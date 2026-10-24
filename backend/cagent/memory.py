from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from .audit import write_event


TTL_DAYS = 180
MAX_ACTIVE = 30


class MemoryStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def _expire(self) -> None:
        cutoff = (datetime.now() - timedelta(days=TTL_DAYS)).isoformat(timespec="seconds")
        self.conn.execute("UPDATE memory SET status='superseded' WHERE status='active' AND updated_at < ?", (cutoff,))
        self.conn.commit()

    def set(self, scope: str, owner_id: str, key: str, value, source: str, ts: datetime | None = None) -> None:
        self._expire()
        stamp = (ts or datetime.now()).isoformat(timespec="seconds")
        self.conn.execute("UPDATE memory SET status='superseded' WHERE scope=? AND owner_id=? AND key=? AND status='active'",
                          (scope, owner_id, key))
        self.conn.execute("INSERT INTO memory(scope, owner_id, key, value, source, status, updated_at) "
                          "VALUES (?,?,?,?,?,'active',?)",
                          (scope, owner_id, key, json.dumps(value, ensure_ascii=False), source, stamp))
        ids = [r[0] for r in self.conn.execute(
            "SELECT id FROM memory WHERE scope=? AND owner_id=? AND status='active' ORDER BY id", (scope, owner_id))]
        if len(ids) > MAX_ACTIVE:
            drop = ids[:-MAX_ACTIVE]
            marks = ",".join("?" * len(drop))
            self.conn.execute(f"UPDATE memory SET status='superseded' WHERE id IN ({marks})", drop)
        self.conn.commit()

    def get(self, scope: str, owner_id: str, key: str, default=None):
        self._expire()
        row = self.conn.execute("SELECT value FROM memory WHERE scope=? AND owner_id=? AND key=? AND status='active' "
                                "ORDER BY id DESC LIMIT 1", (scope, owner_id, key)).fetchone()
        return json.loads(row["value"]) if row else default

    def all(self, scope: str, owner_id: str) -> dict[str, dict]:
        self._expire()
        rows = self.conn.execute("SELECT key, value, source, updated_at FROM memory WHERE scope=? AND owner_id=? "
                                 "AND status='active' ORDER BY id", (scope, owner_id))
        return {r["key"]: {"value": json.loads(r["value"]), "source": r["source"], "updated_at": r["updated_at"]}
                for r in rows}

    def history(self, scope: str, owner_id: str, key: str) -> list[dict]:
        rows = self.conn.execute("SELECT value, source, status, updated_at FROM memory WHERE scope=? AND owner_id=? "
                                 "AND key=? ORDER BY id", (scope, owner_id, key))
        return [{**dict(r), "value": json.loads(r["value"])} for r in rows]

    def correct(self, scope: str, owner_id: str, key: str, value, operator: str, ts: datetime | None = None) -> None:
        ts = ts or datetime.now()
        self.set(scope, owner_id, key, value, f"人工纠正：{operator}", ts)
        write_event(self.conn, checkpoint="记忆操作", customer_id=owner_id if scope == "customer" else None, scene="纠正",
                    content=f"{scope}/{owner_id}/{key} 改为 {json.dumps(value, ensure_ascii=False)}（{operator}）", ts=ts,
                    session_id=owner_id if scope == "session" else None)

    def clear(self, scope: str, owner_id: str, key: str | None = None, operator: str = "",
              ts: datetime | None = None) -> int:
        ts = ts or datetime.now()
        sql = "UPDATE memory SET status='cleared' WHERE scope=? AND owner_id=? AND status='active'"
        params: list = [scope, owner_id]
        if key:
            sql += " AND key=?"
            params.append(key)
        n = self.conn.execute(sql, params).rowcount
        self.conn.commit()
        write_event(self.conn, checkpoint="记忆操作", customer_id=owner_id if scope == "customer" else None, scene="清除",
                    content=f"{scope}/{owner_id}/{key or '全部'}（{operator}）", ts=ts,
                    session_id=owner_id if scope == "session" else None)
        return n
