from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel

from cagent.agents.main import MainTurn
from cagent.llm import bind_session
from cagent.suggestions import suggest

from .service import ChatIn

router = APIRouter(prefix="/api", tags=["对话"])


class FinishIn(BaseModel):
    conclusion: str
    now: datetime | None = None


class EndIn(BaseModel):
    reason: str = "运营结束"
    now: datetime | None = None


class Ok(BaseModel):
    ok: bool = True


class SuggestIn(BaseModel):
    reply: str
    role: str = "operator"
    customer_id: str | None = None
    session_id: str | None = None


class SuggestOut(BaseModel):
    items: list[str]


@router.post("/suggestions")
def suggestions(body: SuggestIn, request: Request) -> SuggestOut:
    svc = request.app.state.service
    with svc.lock:
        with bind_session(body.session_id):
            return SuggestOut(items=suggest(svc.agent.llm, body.reply, body.role, body.customer_id))


@router.post("/chat", response_class=EventSourceResponse, responses={
    200: {"model": MainTurn, "description": "SSE：trace / delta / replace / done / error；done 的 data 结构为 MainTurn（不含 trace）"}})
def chat(body: ChatIn, request: Request) -> Iterator[ServerSentEvent]:
    for kind, data in request.app.state.service.chat_events(body):
        yield ServerSentEvent(event=kind, data=data)


def _open_session(request: Request, session_id: str) -> dict:
    s = request.app.state.service.agent.cases.get_session(session_id)
    if s is None:
        raise HTTPException(404, f"会话不存在: {session_id}")
    if s["ended_at"]:
        raise HTTPException(409, f"会话已结束: {session_id}")
    return s


@router.post("/sessions/{session_id}/end")
def end_session(session_id: str, body: EndIn, request: Request) -> Ok:
    svc = request.app.state.service
    with svc.lock:
        _open_session(request, session_id)
        svc.agent.end_collection(session_id, body.now or datetime.now(), body.reason)
    return Ok()


@router.post("/sessions/{session_id}/handoff/finish")
def finish_handoff(session_id: str, body: FinishIn, request: Request) -> Ok:
    svc = request.app.state.service
    with svc.lock:
        s = _open_session(request, session_id)
        if s["mode"] != "人工接管":
            raise HTTPException(409, "会话不在人工接管模式")
        svc.agent.finish_handoff(session_id, body.conclusion, body.now or datetime.now())
    return Ok()
