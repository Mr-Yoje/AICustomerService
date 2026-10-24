from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from . import db

FK_CHECKS = [
    ("fact_overdue", "overdue_id", "customer_id", "dim_customer", "customer_id"),
    ("fact_overdue", "overdue_id", "loan_id", "dim_loan", "loan_id"),
    ("fact_overdue", "overdue_id", "recommended_template_id", "dim_installment_template", "template_id"),
    ("fact_call_history", "call_id", "customer_id", "dim_customer", "customer_id"),
    ("fact_call_history", "call_id", "violated_rule_id", "dim_compliance_rule", "rule_id"),
    ("fact_repayment", "repay_id", "customer_id", "dim_customer", "customer_id"),
    ("fact_user_behavior", "behavior_id", "customer_id", "dim_customer", "customer_id"),
    ("fact_emotion_log", "emotion_id", "customer_id", "dim_customer", "customer_id"),
    ("dim_loan", "loan_id", "customer_id", "dim_customer", "customer_id"),
]


def _cast(value: str | None, typ: str):
    if value is None or value.strip() == "":
        return None
    if typ == "INTEGER":
        return int(float(value))
    if typ == "REAL":
        return float(value)
    return value.strip()


def import_all(conn: sqlite3.Connection, data_dir: Path) -> dict:
    db.init_runtime(conn)
    counts: dict[str, int] = {}
    stamp = db.now_iso()
    for table, cols in db.BUSINESS_TABLES.items():
        db.create_business_table(conn, table)
        path = Path(data_dir) / f"{table}.csv"
        with path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            missing = [c for c, _ in cols if c not in (reader.fieldnames or [])]
            if missing:
                raise ValueError(f"{path.name} 缺少列: {missing}")
            records = [tuple(_cast(r[c], t) for c, t in cols) + (db.RAW, None, stamp) for r in reader]
        names = [c for c, _ in cols] + db.META_COLUMNS
        conn.executemany(
            f"INSERT INTO {db.q(table)} ({', '.join(db.q(n) for n in names)}) VALUES ({', '.join('?' * len(names))})",
            records,
        )
        counts[table] = len(records)
    issues = check_quality(conn)
    conn.execute("DELETE FROM data_quality")
    conn.executemany(
        "INSERT INTO data_quality(table_name, record_id, customer_id, issue) VALUES (?,?,?,?)",
        [(i["table"], i["record_id"], i["customer_id"], i["issue"]) for i in issues],
    )
    conn.commit()
    return {"counts": counts, "issues": len(issues)}


def check_quality(conn: sqlite3.Connection) -> list[dict]:
    issues: list[dict] = []
    for table, pk, fk, ref, ref_key in FK_CHECKS:
        sql = (f'SELECT t."{pk}" AS rid, t."{fk}" AS fk, t."customer_id" AS cid FROM "{table}" t '
               f'WHERE t."{fk}" IS NOT NULL AND t."{fk}" NOT IN (SELECT "{ref_key}" FROM "{ref}")')
        for r in conn.execute(sql):
            issues.append({"table": table, "record_id": r["rid"], "customer_id": r["cid"],
                           "issue": f"{fk}={r['fk']} 在 {ref} 中不存在"})
    sql = ("SELECT o.overdue_id, o.customer_id, o.loan_id, ("
           "SELECT l.customer_id FROM dim_loan l WHERE l.loan_id = o.loan_id ORDER BY l.rowid LIMIT 1) AS owner "
           "FROM fact_overdue o WHERE o.loan_id IS NOT NULL "
           "AND EXISTS (SELECT 1 FROM dim_loan l WHERE l.loan_id = o.loan_id) "
           "AND NOT EXISTS (SELECT 1 FROM dim_loan l WHERE l.loan_id = o.loan_id AND l.customer_id = o.customer_id)")
    for r in conn.execute(sql):
        issues.append({"table": "fact_overdue", "record_id": r["overdue_id"], "customer_id": r["customer_id"],
                       "issue": f"逾期记录的贷款 {r['loan_id']} 在贷款表中属于 {r['owner']}，与逾期客户 {r['customer_id']} 不一致"})
    return issues


def sync_raw(dest: sqlite3.Connection, source: sqlite3.Connection) -> dict:
    """用源库的「原始导入」业务行覆盖目标库，不复制系统生成行和运行时表。"""
    db.init_runtime(dest)
    counts: dict[str, int] = {}
    for table, cols in db.BUSINESS_TABLES.items():
        db.create_business_table(dest, table)
        names = [c for c, _ in cols] + db.META_COLUMNS
        listed = ", ".join(db.q(n) for n in names)
        rows = source.execute(
            f"SELECT {listed} FROM {db.q(table)} WHERE source = ?", (db.RAW,)
        ).fetchall()
        dest.executemany(
            f"INSERT INTO {db.q(table)} ({listed}) VALUES ({', '.join('?' * len(names))})",
            [tuple(r) for r in rows],
        )
        counts[table] = len(rows)
    issues = check_quality(dest)
    dest.execute("DELETE FROM data_quality")
    dest.executemany(
        "INSERT INTO data_quality(table_name, record_id, customer_id, issue) VALUES (?,?,?,?)",
        [(i["table"], i["record_id"], i["customer_id"], i["issue"]) for i in issues],
    )
    for table in db.RUNTIME_TABLES:
        dest.execute(f"DELETE FROM {db.q(table)}")
    dest.commit()
    return {"counts": counts, "issues": len(issues)}


def reset_generated(conn: sqlite3.Connection) -> None:
    for table in db.BUSINESS_TABLES:
        conn.execute(f"DELETE FROM {db.q(table)} WHERE source = ?", (db.GENERATED,))
    for table in db.RUNTIME_TABLES:
        conn.execute(f"DELETE FROM {db.q(table)}")
    conn.commit()
