from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from cagent.agents.main import MODE_COLLECTION, MODE_HANDOFF
from cagent.audit import export_audit_csv
from cagent.pricing import summarize
from cagent.registry import CallError
from cagent.trace import Trace

from .models import (Artifact, AuditRow, CaseDetail, CaseRow, CostReport, CustomerBrief, CustomerDetail, MemoryItem,
                     MemoryValue, Ok, PostTask, RetryIn, SessionDetail, SessionListItem, masked)

router = APIRouter(prefix="/api", tags=["数据"])


def _svc(request: Request):
    return request.app.state.service


@router.get("/customers")
def list_customers(request: Request, q: str = "") -> list[CustomerBrief]:
    svc = _svc(request)
    like = f"%{q}%"
    with svc.lock:
        rows = svc.agent.conn.execute(
            "SELECT c.customer_id, c.customer_name, c.age, c.occupation, c.region, c.customer_level, c.credit_score, "
            "c.monthly_income, COUNT(o.overdue_id) AS overdue_count, COALESCE(SUM(o.overdue_amount), 0) AS overdue_amount "
            "FROM dim_customer c LEFT JOIN fact_overdue o ON o.customer_id = c.customer_id "
            "WHERE c.customer_id LIKE ? OR c.occupation LIKE ? OR c.region LIKE ? "
            "GROUP BY c.customer_id ORDER BY c.customer_id", (like, like, like)).fetchall()
    return [CustomerBrief(**masked(dict(r))) for r in rows]


@router.get("/customers/{customer_id}")
def get_customer(customer_id: str, request: Request) -> CustomerDetail:
    svc = _svc(request)
    agent = svc.agent
    with svc.lock:
        if agent.conn.execute("SELECT 1 FROM dim_customer WHERE customer_id=?", (customer_id,)).fetchone() is None:
            raise HTTPException(404, f"客户不存在: {customer_id}")
        trace = Trace()
        profile = agent.ops.registry.call("get_customer_profile", trace, customer_id=customer_id)
        overdues = agent.ops.registry.call("get_overdues", trace, customer_id=customer_id)
        try:
            willingness = agent.ops.registry.call("get_willingness", trace, customer_id=customer_id)
        except CallError:
            willingness = None
        row = agent.conn.execute("SELECT session_id FROM sessions WHERE customer_id=? AND ended_at IS NULL "
                                 "ORDER BY started_at DESC LIMIT 1", (customer_id,)).fetchone()
    return CustomerDetail(**masked({
        **profile.model_dump(),
        "overdues": overdues.model_dump(),
        "willingness": willingness.model_dump(mode="json") if willingness else None,
        "open_session_id": row["session_id"] if row else None,
    }))


def _resumable(agent, row) -> bool:
    if row["ended_at"]:
        return False
    if row["mode"] == MODE_HANDOFF:
        return True
    return row["mode"] == MODE_COLLECTION and row["session_id"] in agent.agents


@router.get("/costs")
def cost_report(request: Request, start: str = "", end: str = "") -> CostReport:
    svc = _svc(request)
    with svc.lock:
        rows = svc.agent.conn.execute(
            "SELECT c.ts, c.session_id, c.prompt_tokens, c.completion_tokens, c.cache_hit_tokens, c.cache_miss_tokens, "
            "s.customer_id, s.started_at FROM llm_calls c LEFT JOIN sessions s ON s.session_id = c.session_id "
            "WHERE (? = '' OR c.ts >= ?) AND (? = '' OR c.ts <= ?) ORDER BY c.ts",
            (start, start, end, end)).fetchall()
    return CostReport(**summarize([dict(r) for r in rows]))


@router.get("/sessions")
def list_sessions(request: Request) -> list[SessionListItem]:
    svc = _svc(request)
    agent = svc.agent
    with svc.lock:
        rows = agent.conn.execute("SELECT * FROM sessions ORDER BY started_at DESC").fetchall()
        return [SessionListItem(**dict(r), resumable=_resumable(agent, r)) for r in rows]


@router.get("/sessions/{session_id}")
def get_session(session_id: str, request: Request) -> SessionDetail:
    svc = _svc(request)
    agent = svc.agent
    with svc.lock:
        s = agent.cases.get_session(session_id)
        if s is None:
            raise HTTPException(404, f"会话不存在: {session_id}")
        handoff = agent.cases.artifacts(session_id=session_id, kind="移交包")
        detail = {"session": s, "messages": agent.cases.messages(session_id),
                  "memory": agent.memory.all("session", session_id),
                  "handoff": handoff[-1] if handoff else None, "postcall": agent.post.status(session_id)}
    return SessionDetail(**masked(detail))


@router.post("/sessions/{session_id}/postcall/retry")
def retry_postcall(session_id: str, body: RetryIn, request: Request) -> list[PostTask]:
    svc = _svc(request)
    with svc.lock:
        if svc.agent.cases.get_session(session_id) is None:
            raise HTTPException(404, f"会话不存在: {session_id}")
    svc.agent.post.retry(session_id, body.name, sync=svc.agent.sync_post)
    return [PostTask(**t) for t in svc.agent.post.status(session_id)]


@router.get("/cases")
def list_cases(request: Request, customer_id: str = "", status: str = "") -> list[CaseRow]:
    svc = _svc(request)
    with svc.lock:
        return [CaseRow(**c) for c in svc.agent.cases.search_cases(customer_id or None, status or None)]


@router.get("/cases/{case_id}")
def get_case(case_id: str, request: Request) -> CaseDetail:
    svc = _svc(request)
    cases = svc.agent.cases
    with svc.lock:
        case = cases.get_case(case_id)
        if case is None:
            raise HTTPException(404, f"案件不存在: {case_id}")
        sessions = [dict(r) for r in svc.agent.conn.execute(
            "SELECT * FROM sessions WHERE case_id=? ORDER BY started_at", (case_id,))]
        detail = {"case": case, "sessions": sessions, "artifacts": cases.artifacts(case_id=case_id),
                  "todos": cases.todos(case_id=case_id)}
    return CaseDetail(**masked(detail))


@router.post("/cases/{case_id}/archive")
def archive_case(case_id: str, request: Request) -> CaseRow:
    svc = _svc(request)
    with svc.lock:
        if svc.agent.cases.get_case(case_id) is None:
            raise HTTPException(404, f"案件不存在: {case_id}")
        svc.agent.cases.archive_case(case_id, datetime.now())
        return CaseRow(**svc.agent.cases.get_case(case_id))


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: str, request: Request) -> Artifact:
    svc = _svc(request)
    with svc.lock:
        rows = [a for a in svc.agent.cases.artifacts() if a["artifact_id"] == artifact_id]
    if not rows:
        raise HTTPException(404, f"产出物不存在: {artifact_id}")
    return Artifact(**masked(rows[0]))


@router.get("/audit")
def list_audit(request: Request, rule_id: str = "", checkpoint: str = "", customer_id: str = "",
               limit: int = 500) -> list[AuditRow]:
    svc = _svc(request)
    sql, params = "SELECT * FROM audit_log WHERE 1=1", []
    for col, val in (("rule_id", rule_id), ("checkpoint", checkpoint), ("customer_id", customer_id)):
        if val:
            sql += f" AND {col}=?"
            params.append(val)
    with svc.lock:
        rows = svc.agent.conn.execute(sql + " ORDER BY ts DESC, log_id DESC LIMIT ?", [*params, limit]).fetchall()
    return [AuditRow(**dict(r)) for r in rows]


@router.get("/audit/export", response_class=Response, responses={200: {"content": {"text/csv": {}}}})
def export_audit(request: Request) -> Response:
    svc = _svc(request)
    with tempfile.TemporaryDirectory() as d, svc.lock:
        path = Path(d) / "audit_log.csv"
        export_audit_csv(svc.agent.conn, path)
        data = path.read_bytes()
    return Response(data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=audit_log.csv"})


@router.get("/memory/{scope}/{owner_id}")
def get_memory(scope: str, owner_id: str, request: Request) -> dict[str, MemoryItem]:
    svc = _svc(request)
    with svc.lock:
        return {k: MemoryItem(**masked(v)) for k, v in svc.agent.memory.all(scope, owner_id).items()}


@router.put("/memory/{scope}/{owner_id}/{key}")
def correct_memory(scope: str, owner_id: str, key: str, body: MemoryValue, request: Request) -> Ok:
    svc = _svc(request)
    with svc.lock:
        svc.agent.memory.correct(scope, owner_id, key, body.value, body.operator)
    return Ok()


@router.delete("/memory/{scope}/{owner_id}/{key}")
def clear_memory(scope: str, owner_id: str, key: str, request: Request, operator: str = "运营") -> Ok:
    svc = _svc(request)
    with svc.lock:
        if not svc.agent.memory.clear(scope, owner_id, key, operator):
            raise HTTPException(404, f"记忆不存在: {scope}/{owner_id}/{key}")
    return Ok()
