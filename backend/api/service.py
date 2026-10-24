from __future__ import annotations

import contextvars
import json
import logging
import queue
import threading
from datetime import datetime
from typing import Iterator

from pydantic import BaseModel

from cagent.agents.main import MODE_COLLECTION, TurnUsage, MainAgent
from cagent.llm import bind_session, usage_since
from cagent.narrate import narrate
from cagent.trace import TraceEvent, jsonable

log = logging.getLogger("cagent")
ROLES = {"operator": "operator", "customer": "customer", "agent": "agent", "human_agent": "人工坐席"}
_CHUNK = 4
_END = object()

Event = tuple[str, dict]


class ChatIn(BaseModel):
    text: str = ""
    session_id: str | None = None
    role: str | None = None
    customer_id: str | None = None
    now: datetime | None = None


def _ai_messages(conn, session_id: str | None) -> int:
    if not session_id:
        return 0
    return conn.execute("SELECT COUNT(*) FROM messages WHERE session_id=? AND role='AI坐席'", (session_id,)).fetchone()[0]


def _stamp_usage(conn, session_id: str, usage: TurnUsage) -> None:
    row = conn.execute(
        "SELECT id, meta FROM messages WHERE session_id=? AND role='AI坐席' ORDER BY turn DESC LIMIT 1",
        (session_id,)).fetchone()
    if row is None:
        return
    meta = json.loads(row["meta"] or "{}")
    meta["usage"] = usage.model_dump()
    conn.execute("UPDATE messages SET meta=? WHERE id=?", (json.dumps(meta, ensure_ascii=False), row["id"]))
    conn.commit()


def _chunks(text: str) -> Iterator[Event]:
    for i in range(0, len(text), _CHUNK):
        yield "delta", {"text": text[i:i + _CHUNK]}


class Service:
    def __init__(self, agent: MainAgent):
        self.agent = agent
        self.lock = threading.RLock()

    @classmethod
    def from_settings(cls, **kw) -> "Service":
        return cls(MainAgent.from_settings(**kw))

    def _run_turn(self, req: ChatIn, emit) -> None:
        with self.lock:
            conn = self.agent.conn
            since = conn.execute("SELECT COALESCE(MAX(id), 0) FROM llm_calls").fetchone()[0]
            before = _ai_messages(conn, req.session_id)
            turn = self.agent.handle(
                req.text, session_id=req.session_id, role=ROLES.get(req.role or "", req.role),
                customer_id=req.customer_id or None, now=req.now,
                sink=lambda e: emit(("trace", jsonable(e.__dict__ if isinstance(e, TraceEvent) else e))),
            )
            # 发起催收时的回复是 AI 坐席开场白，属于发给客户的话术，不能交给大模型改写
            narrated = turn.speaker == "operator" and turn.mode != MODE_COLLECTION
            with bind_session(turn.session_id):
                events = narrate(self.agent.llm, turn.reply, question=req.text) if narrated else _chunks(turn.reply)
                text = ""
                for kind, data in events:
                    if kind == "delta":
                        text += data["text"]
                    elif kind == "replace":
                        text = data["text"]
                    emit((kind, data))
            usage = usage_since(conn, since)
            turn.usage = TurnUsage(**usage) if usage["calls"] else None
            grew = _ai_messages(conn, turn.session_id) > (before if turn.session_id == req.session_id else 0)
            if turn.usage and turn.session_id and grew:
                _stamp_usage(conn, turn.session_id, turn.usage)
            done = turn.model_dump(mode="json", exclude={"trace"})
            done["reply"] = text
            emit(("done", done))

    def chat_events(self, req: ChatIn) -> Iterator[Event]:
        q: queue.Queue = queue.Queue()

        def worker() -> None:
            try:
                self._run_turn(req, q.put)
            except Exception as e:  # noqa: BLE001 任何失败都要以 error 事件告知前端
                log.exception("对话处理失败")
                q.put(("error", {"message": f"{type(e).__name__}: {e}"}))
            finally:
                q.put(_END)

        threading.Thread(target=contextvars.copy_context().run, args=(worker,), daemon=True).start()
        while (item := q.get()) is not _END:
            yield item
