from __future__ import annotations

import contextvars
import logging
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import db
from .cases import CaseStore
from .config_center import ConfigCenter
from .loans import prioritize
from .memory import MemoryStore
from .reports import render_report_markdown, session_memory, session_report
from .scoring import Willingness, score_customer

log = logging.getLogger("cagent")
STAGES = {"write_call_record": 1, "write_behavior": 1, "write_emotion": 1,
          "refresh_profile": 2, "generate_report": 2, "create_todos": 3}
BEHAVIOR_OF_INTENT = {"承诺还款": "承诺还款", "要求宽限": "申请延期", "要求分期": "协商分期", "要求减免": "申请减免",
                      "拒绝还款": "拒绝沟通", "投诉": "投诉"}


@dataclass
class PostCtx:
    conn: sqlite3.Connection
    session: dict
    case_id: str
    customer_id: str
    pack: dict
    policy: dict
    mem: dict
    messages: list[dict]
    now: datetime

    @property
    def sid(self) -> str:
        return self.session["session_id"]


def build_ctx(conn: sqlite3.Connection, session_id: str, pack: dict, policy: dict) -> PostCtx:
    cases = CaseStore(conn)
    s = cases.get_session(session_id)
    if s is None:
        raise ValueError(f"会话不存在: {session_id}")
    return PostCtx(conn=conn, session=s, case_id=s["case_id"], customer_id=s["customer_id"], pack=pack, policy=policy,
                   mem=session_memory(conn, session_id), messages=cases.messages(session_id),
                   now=datetime.fromisoformat(s["ended_at"] or s["started_at"]))


def _replace(ctx: PostCtx, table: str, rows: list[dict]) -> None:
    ctx.conn.execute(f"DELETE FROM {db.q(table)} WHERE session_id=? AND source=?", (ctx.sid, db.GENERATED))
    for row in rows:
        db.insert_generated(ctx.conn, table, row, ctx.sid)
    ctx.conn.commit()


def _call_result(mem: dict) -> str:
    if mem.get("gate"):
        return "未外呼"
    if mem.get("promises"):
        return "承诺还款"
    if any(n.get("方案") for n in mem.get("negotiation", [])):
        return "协商分期"
    if "拒绝还款" in mem.get("intents", []):
        return "拒绝沟通"
    return "已沟通"


def write_call_record(ctx: PostCtx) -> None:
    method = "外呼拦截" if ctx.mem.get("gate") else "客户进线" if ctx.session["channel"] == "客户进线" else "AI对话"
    human = any(m["role"] == "人工坐席" for m in ctx.messages)
    hits = [r["rule_id"] for r in ctx.conn.execute(
        "SELECT DISTINCT rule_id FROM audit_log WHERE session_id=? AND checkpoint='坐席话术' AND rule_id IS NOT NULL",
        (ctx.sid,))]
    summary = (f"{ctx.pack['name']}；客户诉求：{'、'.join(ctx.mem.get('intents', [])) or '无'}；"
               f"结束原因：{ctx.session['end_reason'] or '正常结束'}")
    _replace(ctx, "fact_call_history", [{
        "call_id": f"CH-{ctx.sid}", "customer_id": ctx.customer_id, "call_time": ctx.now.strftime("%Y-%m-%d %H:%M"),
        "call_method": method, "talk_summary": summary[:120], "call_result": _call_result(ctx.mem),
        "agent_id": ctx.pack["speech"]["agent_id"] + ("+人工" if human else ""),
        "violated_rule_id": ",".join(hits) or None,
    }])


def write_behavior(ctx: PostCtx) -> None:
    kinds = [(i, BEHAVIOR_OF_INTENT[i]) for i in ctx.mem.get("intents", []) if i in BEHAVIOR_OF_INTENT]
    _replace(ctx, "fact_user_behavior", [{
        "behavior_id": f"B-{ctx.sid}-{n}", "customer_id": ctx.customer_id, "behavior_type": kind,
        "behavior_time": ctx.now.strftime("%Y-%m-%d %H:%M"), "behavior_detail": f"本次通话中客户{intent}",
    } for n, (intent, kind) in enumerate(kinds, 1)])


def write_emotion(ctx: PostCtx) -> None:
    emotions = ctx.mem.get("emotions", [])
    rows = []
    if emotions:
        picks = {"峰值": max(emotions, key=lambda e: e.get("intensity") or 0), "结束": emotions[-1]}
        for tag, e in picks.items():
            if tag == "结束" and e is picks["峰值"]:
                continue
            rows.append({"emotion_id": f"E-{ctx.sid}-{tag}", "customer_id": ctx.customer_id,
                         "emotion_type": e["emotion"], "emotion_intensity": e.get("intensity"),
                         "trigger_talk": e.get("text", ""), "record_time": ctx.now.strftime("%Y-%m-%d %H:%M")})
    _replace(ctx, "fact_emotion_log", rows)


def refresh_profile(ctx: PostCtx) -> None:
    will = score_customer(ctx.conn, ctx.customer_id, ctx.policy, ctx.mem.get("intents", []))
    ctx.conn.execute("DELETE FROM profile_snapshots WHERE customer_id=? AND session_id=?", (ctx.customer_id, ctx.sid))
    version = ctx.conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM profile_snapshots WHERE customer_id=?",
                               (ctx.customer_id,)).fetchone()[0]
    content = {"willingness": will.model_dump(mode="json"),
               "loans": prioritize(ctx.conn, ctx.customer_id, ctx.policy).model_dump(mode="json")}
    ctx.conn.execute("INSERT INTO profile_snapshots VALUES (?,?,?,?,?)",
                     (ctx.customer_id, version, db.json_dumps(content), ctx.sid, db.now_iso()))
    ctx.conn.commit()
    mem = MemoryStore(ctx.conn)
    mem.set("customer", ctx.customer_id, "last_willingness",
            {"score": will.score, "level": will.level, "session_id": ctx.sid}, f"会话{ctx.sid}")
    mem.set("customer", ctx.customer_id, "last_contact",
            {"time": ctx.now.isoformat(timespec="minutes"), "result": _call_result(ctx.mem)}, f"会话{ctx.sid}")
    if ctx.mem.get("promises"):
        mem.set("customer", ctx.customer_id, "open_promises", ctx.mem["promises"], f"会话{ctx.sid}")


def generate_report(ctx: PostCtx) -> None:
    rep = session_report(ctx.conn, ctx.sid, ctx.policy, ctx.now)
    ctx.conn.execute("DELETE FROM artifacts WHERE session_id=? AND kind='催收报告'", (ctx.sid,))
    ctx.conn.commit()
    CaseStore(ctx.conn).add_artifact(ctx.case_id, ctx.sid, "催收报告",
                                     {"data": rep, "markdown": render_report_markdown(rep, ctx.pack["report_sections"])},
                                     ctx.now)


def create_todos(ctx: PostCtx) -> None:
    cs, sid = CaseStore(ctx.conn), ctx.sid
    for n, p in enumerate(ctx.mem.get("promises", []), 1):
        amount = f"{p['amount']:.0f}元" if p.get("amount") else "欠款"
        cs.add_todo(ctx.case_id, sid, "承诺跟进", f"客户承诺{p.get('date') or '近期'}还款{amount}，到期核实",
                    p.get("date"), ctx.now, todo_id=f"TD-{sid}-promise-{n}")
    for w in ctx.mem.get("waivers", []):
        cs.add_todo(ctx.case_id, sid, "减免审批", f"减免申请 {w['request_id']} 待审批：{w['scope']}", None, ctx.now,
                    todo_id=f"TD-{sid}-{w['request_id']}")
    if grace := ctx.mem.get("grace_request"):
        cs.add_todo(ctx.case_id, sid, "宽限跟进", f"客户申请宽限至{grace.get('date') or '待确认'}", grace.get("date"),
                    ctx.now, todo_id=f"TD-{sid}-grace")
    if (gate := ctx.mem.get("gate")) and gate.get("next_allowed"):
        cs.add_todo(ctx.case_id, sid, "下次可联系", f"外呼被 {'、'.join(gate['rule_ids'])} 拦截，下次可联系时间到后再发起",
                    gate["next_allowed"], ctx.now, todo_id=f"TD-{sid}-next-contact")


TASKS: dict[str, Callable[[PostCtx], None]] = {
    "write_call_record": write_call_record, "write_behavior": write_behavior, "write_emotion": write_emotion,
    "refresh_profile": refresh_profile, "generate_report": generate_report, "create_todos": create_todos,
}


class PostCallRunner:
    def __init__(self, db_path: Path | str, config_dir: Path | str, max_retries: int = 2):
        self.db_path, self.config_dir, self.max_retries = db_path, Path(config_dir), max_retries

    def _load(self, conn: sqlite3.Connection, session_id: str) -> tuple[dict, dict, dict]:
        s = CaseStore(conn).get_session(session_id)
        if s is None:
            raise ValueError(f"会话不存在: {session_id}")
        cc = ConfigCenter(conn, self.config_dir)
        return s, cc.get("business_pack", s["pack_id"])[0], cc.get("policy")[0]

    def enqueue(self, session_id: str, sync: bool = False) -> None:
        conn = db.connect(self.db_path)
        try:
            s, pack, _ = self._load(conn, session_id)
            for name in pack["post_tasks"]:
                conn.execute("INSERT OR IGNORE INTO post_tasks VALUES (?,?,?,?,?,?,?)",
                             (session_id, name, STAGES[name], "等待", 0, None, db.now_iso()))
            conn.commit()
            CaseStore(conn).set_case_status(s["case_id"], "后置处理中", datetime.now())
        finally:
            conn.close()
        if sync:
            self._run(session_id)
        else:
            threading.Thread(target=self._run, args=(session_id,), daemon=True).start()

            ctx = contextvars.copy_context()
            threading.Thread(target=ctx.run, args=(self._run, session_id), daemon=True).start()

    def _run(self, session_id: str) -> None:
        conn = db.connect(self.db_path)
        try:
            s, pack, policy = self._load(conn, session_id)
            pending = [dict(r) for r in conn.execute(
                "SELECT name, stage FROM post_tasks WHERE session_id=? AND status<>'完成' ORDER BY stage, name", (session_id,))]
        finally:
            conn.close()
        for stage in sorted({p["stage"] for p in pending}):
            names = [p["name"] for p in pending if p["stage"] == stage]
            with ThreadPoolExecutor(max_workers=len(names)) as ex:
                list(ex.map(lambda n: self._run_task(session_id, n, pack, policy), names))
        conn = db.connect(self.db_path)
        try:
            if all(t["status"] == "完成" for t in self._status(conn, session_id)):
                CaseStore(conn).set_case_status(s["case_id"], "已完成", datetime.now())
        finally:
            conn.close()

    @staticmethod
    def _mark(conn: sqlite3.Connection, session_id: str, name: str, status: str, inc: int, error: str | None) -> None:
        conn.execute("UPDATE post_tasks SET status=?, attempts=attempts+?, error=?, updated_at=? WHERE session_id=? AND name=?",
                     (status, inc, error, db.now_iso(), session_id, name))
        conn.commit()

    def _run_task(self, session_id: str, name: str, pack: dict, policy: dict) -> bool:
        error = ""
        for _ in range(self.max_retries + 1):
            conn = db.connect(self.db_path)
            try:
                self._mark(conn, session_id, name, "执行中", 1, None)
                TASKS[name](build_ctx(conn, session_id, pack, policy))
                self._mark(conn, session_id, name, "完成", 0, None)
                log.info("后置任务 %s 完成 会话%s", name, session_id)
                return True
            except Exception as e:  # noqa: BLE001 单个任务的任意失败都要隔离并记录
                conn.rollback()
                error = f"{type(e).__name__}: {e}"
                log.warning("后置任务 %s 失败 会话%s %s", name, session_id, error)
                self._mark(conn, session_id, name, "失败", 0, error)
            finally:
                conn.close()
        conn = db.connect(self.db_path)
        try:
            s = CaseStore(conn).get_session(session_id)
            CaseStore(conn).add_todo(s["case_id"], session_id, "后置任务失败", f"{name} 重试后仍失败：{error}", None,
                                     datetime.now(), todo_id=f"TD-{session_id}-post-{name}")
        finally:
            conn.close()
        return False

    @staticmethod
    def _status(conn: sqlite3.Connection, session_id: str) -> list[dict]:
        return [dict(r) for r in conn.execute("SELECT * FROM post_tasks WHERE session_id=? ORDER BY stage, name",
                                              (session_id,))]

    def status(self, session_id: str) -> list[dict]:
        conn = db.connect(self.db_path)
        try:
            return self._status(conn, session_id)
        finally:
            conn.close()

    def retry(self, session_id: str, name: str | None = None, sync: bool = False) -> None:
        conn = db.connect(self.db_path)
        try:
            sql = "UPDATE post_tasks SET status='等待', error=NULL WHERE session_id=?"
            params: list = [session_id]
            if name:
                sql += " AND name=?"
                params.append(name)
            else:
                sql += " AND status='失败'"
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()
        if sync:
            self._run(session_id)
        else:
            ctx = contextvars.copy_context()
            threading.Thread(target=ctx.run, args=(self._run, session_id), daemon=True).start()