from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

RAW = "原始导入"
GENERATED = "系统生成"

BUSINESS_TABLES: dict[str, list[tuple[str, str]]] = {
    "dim_customer": [("customer_id", "TEXT"), ("customer_name", "TEXT"), ("age", "INTEGER"),
                     ("occupation", "TEXT"), ("monthly_income", "REAL"), ("credit_score", "INTEGER"),
                     ("region", "TEXT"), ("customer_level", "TEXT")],
    "dim_loan": [("loan_id", "TEXT"), ("customer_id", "TEXT"), ("loan_type", "TEXT"), ("principal", "REAL"),
                 ("annual_rate", "REAL"), ("periods", "INTEGER"), ("issue_date", "TEXT"), ("due_date", "TEXT")],
    "dim_compliance_rule": [("rule_id", "TEXT"), ("rule_type", "TEXT"), ("rule_content", "TEXT"),
                            ("violation_level", "TEXT"), ("action", "TEXT")],
    "dim_installment_template": [("template_id", "TEXT"), ("template_type", "TEXT"), ("max_periods", "INTEGER"),
                                 ("rate", "REAL"), ("fee", "REAL"), ("condition", "TEXT")],
    "fact_overdue": [("overdue_id", "TEXT"), ("customer_id", "TEXT"), ("loan_id", "TEXT"),
                     ("overdue_amount", "REAL"), ("overdue_days", "INTEGER"), ("overdue_periods", "INTEGER"),
                     ("last_repay_date", "TEXT"), ("recommended_template_id", "TEXT")],
    "fact_call_history": [("call_id", "TEXT"), ("customer_id", "TEXT"), ("call_time", "TEXT"),
                          ("call_method", "TEXT"), ("talk_summary", "TEXT"), ("call_result", "TEXT"),
                          ("agent_id", "TEXT"), ("violated_rule_id", "TEXT")],
    "fact_repayment": [("repay_id", "TEXT"), ("customer_id", "TEXT"), ("repay_amount", "REAL"),
                       ("repay_time", "TEXT"), ("repay_method", "TEXT"), ("is_on_time", "TEXT")],
    "fact_user_behavior": [("behavior_id", "TEXT"), ("customer_id", "TEXT"), ("behavior_type", "TEXT"),
                           ("behavior_time", "TEXT"), ("behavior_detail", "TEXT")],
    "fact_emotion_log": [("emotion_id", "TEXT"), ("customer_id", "TEXT"), ("emotion_type", "TEXT"),
                         ("emotion_intensity", "REAL"), ("trigger_talk", "TEXT"), ("record_time", "TEXT")],
}

META_COLUMNS = ["source", "session_id", "written_at"]

RUNTIME_DDL = """
CREATE TABLE IF NOT EXISTS cases(case_id TEXT PRIMARY KEY, customer_id TEXT, pack_id TEXT, status TEXT,
  created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS sessions(session_id TEXT PRIMARY KEY, case_id TEXT, customer_id TEXT, mode TEXT,
  pack_id TEXT, config_version TEXT, channel TEXT, started_at TEXT, ended_at TEXT, end_reason TEXT);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, turn INTEGER,
  role TEXT, text TEXT, meta TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS memory(id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT, owner_id TEXT, key TEXT,
  value TEXT, source TEXT, status TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS artifacts(artifact_id TEXT PRIMARY KEY, case_id TEXT, session_id TEXT, kind TEXT,
  content TEXT, created_at TEXT, archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS audit_log(log_id TEXT PRIMARY KEY, ts TEXT, customer_id TEXT, checkpoint TEXT,
  scene TEXT, content TEXT, rule_id TEXT, rule_type TEXT, level TEXT, action TEXT, handoff INTEGER,
  config_version TEXT, session_id TEXT);
CREATE TABLE IF NOT EXISTS llm_calls(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, purpose TEXT,
  masked_input TEXT, masked_output TEXT, status TEXT, latency_ms INTEGER, session_id TEXT, model TEXT,
  prompt_tokens INTEGER, completion_tokens INTEGER, cache_hit_tokens INTEGER, cache_miss_tokens INTEGER);
CREATE TABLE IF NOT EXISTS todos(todo_id TEXT PRIMARY KEY, case_id TEXT, session_id TEXT, kind TEXT,
  content TEXT, due TEXT, status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS post_tasks(session_id TEXT, name TEXT, stage INTEGER, status TEXT, attempts INTEGER,
  error TEXT, updated_at TEXT, PRIMARY KEY(session_id, name));
CREATE TABLE IF NOT EXISTS profile_snapshots(customer_id TEXT, version INTEGER, content TEXT, session_id TEXT,
  created_at TEXT, PRIMARY KEY(customer_id, version));
CREATE TABLE IF NOT EXISTS config_versions(kind TEXT, key TEXT, version TEXT, content TEXT, created_at TEXT,
  PRIMARY KEY(kind, key, version));
CREATE TABLE IF NOT EXISTS data_quality(issue_id INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT,
  record_id TEXT, customer_id TEXT, issue TEXT);
"""

RUNTIME_TABLES = ["cases", "sessions", "messages", "memory", "artifacts", "audit_log", "llm_calls", "todos",
                  "post_tasks", "profile_snapshots"]


def q(name: str) -> str:
    return f'"{name}"'


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def connect(path: Path | str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_runtime(conn: sqlite3.Connection) -> None:
    conn.executescript(RUNTIME_DDL)
    have = {row[1] for row in conn.execute("PRAGMA table_info(llm_calls)")}
    for name, typ in (
        ("session_id", "TEXT"), ("model", "TEXT"), ("prompt_tokens", "INTEGER"),
        ("completion_tokens", "INTEGER"), ("cache_hit_tokens", "INTEGER"), ("cache_miss_tokens", "INTEGER"),
    ):
        if name not in have:
            conn.execute(f"ALTER TABLE llm_calls ADD COLUMN {name} {typ}")
    conn.commit()


def create_business_table(conn: sqlite3.Connection, table: str) -> None:
    cols = ", ".join(f"{q(c)} {t}" for c, t in BUSINESS_TABLES[table])
    conn.execute(f"DROP TABLE IF EXISTS {q(table)}")
    conn.execute(
        f"CREATE TABLE {q(table)} ({cols}, \"source\" TEXT NOT NULL DEFAULT '{GENERATED}', "
        f"\"session_id\" TEXT, \"written_at\" TEXT)"
    )
    for op in ("UPDATE", "DELETE"):
        conn.execute(
            f"CREATE TRIGGER {q(table + '_raw_no_' + op.lower())} BEFORE {op} ON {q(table)} "
            f"WHEN OLD.source = '{RAW}' BEGIN SELECT RAISE(ABORT, '原始数据只读'); END"
        )


def insert_generated(conn: sqlite3.Connection, table: str, row: dict, session_id: str | None) -> None:
    cols = [c for c, _ in BUSINESS_TABLES[table]]
    unknown = set(row) - set(cols)
    if unknown:
        raise ValueError(f"{table} 不存在字段: {sorted(unknown)}")
    names = cols + META_COLUMNS
    values = [row.get(c) for c in cols] + [GENERATED, session_id, now_iso()]
    conn.execute(
        f"INSERT INTO {q(table)} ({', '.join(q(n) for n in names)}) VALUES ({', '.join('?' * len(names))})",
        values,
    )
