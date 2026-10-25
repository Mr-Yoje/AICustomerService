from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime

CASE_STATUSES = ["待发起", "前置检查", "对话中", "人工接管中", "后置处理中", "已完成", "已归档"]
SESSION_FIELDS = {"mode", "ended_at", "end_reason"}


def _iso(ts: datetime) -> str:
    return ts.isoformat(timespec="seconds")


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class CaseStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create_case(self, customer_id: str, pack_id: str, now: datetime) -> str:
        case_id = _uid("CASE")
        self.conn.execute("INSERT INTO cases VALUES (?,?,?,?,?,?)",
                          (case_id, customer_id, pack_id, "待发起", _iso(now), _iso(now)))
        self.conn.commit()
        return case_id

    def set_case_status(self, case_id: str, status: str, now: datetime) -> None:
        if status not in CASE_STATUSES:
            raise ValueError(f"非法案件状态: {status}")
        self.conn.execute("UPDATE cases SET status=?, updated_at=? WHERE case_id=?", (status, _iso(now), case_id))
        self.conn.commit()

    def get_case(self, case_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        return dict(row) if row else None

    def search_cases(self, customer_id: str | None = None, status: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM cases WHERE 1=1", []
        if customer_id:
            sql += " AND customer_id=?"
            params.append(customer_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY created_at DESC", params)]

    def archive_case(self, case_id: str, now: datetime) -> None:
        self.set_case_status(case_id, "已归档", now)
        self.conn.execute("UPDATE artifacts SET archived=1 WHERE case_id=?", (case_id,))
        self.conn.commit()

    def open_session(self, case_id: str, customer_id: str, mode: str, pack_id: str, config_version: str,
                     channel: str, now: datetime) -> str:
        sid = _uid("S")
        self.conn.execute("INSERT INTO sessions(session_id, case_id, customer_id, mode, pack_id, config_version, channel, "
                          "started_at) VALUES (?,?,?,?,?,?,?,?)",
                          (sid, case_id, customer_id, mode, pack_id, config_version, channel, _iso(now)))
        self.conn.commit()
        return sid

    def get_session(self, session_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def update_session(self, session_id: str, **fields) -> None:
        bad = set(fields) - SESSION_FIELDS
        if bad:
            raise ValueError(f"不允许修改的会话字段: {sorted(bad)}")
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            self.conn.execute(f"UPDATE sessions SET {sets} WHERE session_id=?", [*fields.values(), session_id])
            self.conn.commit()

    def add_message(self, session_id: str, role: str, text: str, meta: dict, now: datetime) -> int:
        turn = self.conn.execute("SELECT COALESCE(MAX(turn), 0) + 1 FROM messages WHERE session_id=?",
                                 (session_id,)).fetchone()[0]
        self.conn.execute("INSERT INTO messages(session_id, turn, role, text, meta, created_at) VALUES (?,?,?,?,?,?)",
                          (session_id, turn, role, text, json.dumps(meta, ensure_ascii=False), _iso(now)))
        self.conn.commit()
        return turn

    def messages(self, session_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT turn, role, text, meta, created_at FROM messages WHERE session_id=? ORDER BY turn",
                                 (session_id,))
        parsed = [{**dict(r), "meta": json.loads(r["meta"] or "{}")} for r in rows]
        ordered = sorted(parsed, key=lambda m: (m["created_at"] or "", m["turn"]))
        if [m["turn"] for m in ordered] != [m["turn"] for m in parsed]:
            for m in ordered:
                m["meta"]["乱序已重排"] = True
        return ordered

    def context(self, session_id: str, keep_last: int = 6) -> dict:
        msgs = self.messages(session_id)
        recent, older = msgs[-keep_last:], msgs[:-keep_last]
        parts = []
        for m in older:
            intent = m["meta"].get("intent")
            parts.append(f"第{m['turn']}句 {m['role']}{'（' + intent + '）' if intent else ''}：{m['text'][:20]}")
        summary = "；".join(parts)
        return {"recent": [{"role": m["role"], "text": m["text"][:200]} for m in recent],
                "summary": summary[-400:]}

    def add_artifact(self, case_id: str | None, session_id: str | None, kind: str, content, now: datetime) -> str:
        aid = _uid("ART")
        self.conn.execute("INSERT INTO artifacts(artifact_id, case_id, session_id, kind, content, created_at) "
                          "VALUES (?,?,?,?,?,?)",
                          (aid, case_id, session_id, kind, json.dumps(content, ensure_ascii=False), _iso(now)))
        self.conn.commit()
        return aid

    def artifacts(self, case_id: str | None = None, session_id: str | None = None, kind: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM artifacts WHERE 1=1", []
        for col, val in (("case_id", case_id), ("session_id", session_id), ("kind", kind)):
            if val:
                sql += f" AND {col}=?"
                params.append(val)
        rows = self.conn.execute(sql + " ORDER BY created_at, rowid", params)
        return [{**dict(r), "content": json.loads(r["content"])} for r in rows]

    def latest_artifact(self, customer_id: str, kind: str) -> dict | None:
        row = self.conn.execute("SELECT a.* FROM artifacts a JOIN cases c ON c.case_id = a.case_id "
                                "WHERE c.customer_id=? AND a.kind=? ORDER BY a.created_at DESC, a.rowid DESC LIMIT 1",
                                (customer_id, kind)).fetchone()
        return {**dict(row), "content": json.loads(row["content"])} if row else None

    def add_todo(self, case_id: str | None, session_id: str | None, kind: str, content: str, due: str | None,
                 now: datetime, todo_id: str | None = None) -> str:
        todo_id = todo_id or _uid("TD")
        self.conn.execute("INSERT OR IGNORE INTO todos VALUES (?,?,?,?,?,?,?,?)",
                          (todo_id, case_id, session_id, kind, content, due, "待处理", _iso(now)))
        self.conn.commit()
        return todo_id

    def todos(self, case_id: str | None = None, session_id: str | None = None, status: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM todos WHERE 1=1", []
        for col, val in (("case_id", case_id), ("session_id", session_id), ("status", status)):
            if val:
                sql += f" AND {col}=?"
                params.append(val)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY created_at", params)]
