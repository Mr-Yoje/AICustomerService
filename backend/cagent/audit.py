from __future__ import annotations

import csv
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from .compliance import CheckResult
from .desensitize import display_mask, export_mask

CHECKPOINT_NAMES = {"agent_speech": "坐席话术", "operator_action": "运营操作", "customer_input": "客户输入"}

_INSERT = ("INSERT INTO audit_log(log_id, ts, customer_id, checkpoint, scene, content, rule_id, rule_type, level, "
           "action, handoff, config_version, session_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)")


def _id() -> str:
    return "AUD-" + uuid.uuid4().hex[:10]


def write_audit(conn: sqlite3.Connection, *, result: CheckResult, customer_id: str | None, scene: str, content: str,
                config_version: str, ts: datetime, session_id: str | None = None) -> list[str]:
    ids = []
    for hit in result.hits or [None]:
        log_id = _id()
        conn.execute(_INSERT, (
            log_id, ts.isoformat(timespec="seconds"), customer_id, CHECKPOINT_NAMES.get(result.checkpoint, result.checkpoint),
            scene, display_mask(content)[:300], hit.rule_id if hit else None, hit.rule_type if hit else None,
            hit.level if hit else None, hit.action if hit else None, int(result.handoff), config_version, session_id,
        ))
        ids.append(log_id)
    conn.commit()
    return ids


def write_event(conn: sqlite3.Connection, *, checkpoint: str, customer_id: str | None, scene: str, content: str,
                ts: datetime, session_id: str | None = None, config_version: str = "-") -> str:
    log_id = _id()
    conn.execute(_INSERT, (log_id, ts.isoformat(timespec="seconds"), customer_id, checkpoint, scene,
                           display_mask(content)[:300], None, None, None, None, 0, config_version, session_id))
    conn.commit()
    return log_id


def export_audit_csv(conn: sqlite3.Connection, path: Path) -> int:
    rows = conn.execute("SELECT * FROM audit_log ORDER BY ts, log_id").fetchall()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["日志编号", "时间戳", "客户编号", "检查点", "场景类型", "话术内容摘要", "命中规则编号", "规则类型",
                    "违规等级", "处置动作", "是否转人工", "配置版本", "会话编号"])
        for r in rows:
            w.writerow([r["log_id"], r["ts"], r["customer_id"], r["checkpoint"], r["scene"], export_mask(r["content"] or ""),
                        r["rule_id"] or "", r["rule_type"] or "", r["level"] or "", r["action"] or "",
                        "是" if r["handoff"] else "否", r["config_version"], r["session_id"] or ""])
    return len(rows)
