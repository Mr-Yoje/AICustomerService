from __future__ import annotations

import csv
import sqlite3
from datetime import date, datetime
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


_PRIVATE_FK = {
    "dim_loan": {"customer_id": "dim_customer"},
    "fact_overdue": {"customer_id": "dim_customer", "loan_id": "dim_loan"},
    "fact_call_history": {"customer_id": "dim_customer"},
    "fact_repayment": {"customer_id": "dim_customer"},
    "fact_user_behavior": {"customer_id": "dim_customer"},
    "fact_emotion_log": {"customer_id": "dim_customer"},
}


def _cell(value, typ: str):
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.hour == value.minute == value.second == value.microsecond == 0:
            return value.date().isoformat()
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return None
    if typ == "INTEGER":
        return int(float(value))
    if typ == "REAL":
        return float(value)
    return str(value).strip()


def append_private(conn: sqlite3.Connection, workbook: Path) -> dict:
    """把非公开业务表追加进当前库。客户编号保持原值；主键冲突的本批记录改为 P- 前缀，并改写本批外键。"""
    import openpyxl

    wb = openpyxl.load_workbook(workbook, read_only=True, data_only=True)
    renames: dict[str, dict[str, str]] = {}
    counts: dict[str, int] = {}
    stamp = db.now_iso()
    try:
        for table, cols in db.BUSINESS_TABLES.items():
            if table not in wb.sheetnames:
                continue
            rows = wb[table].iter_rows(values_only=True)
            header = ["" if cell is None else str(cell) for cell in next(rows)]
            missing = [name for name, _ in cols if name not in header]
            if missing:
                raise ValueError(f"{table} 缺少列: {missing}")
            index = {name: header.index(name) for name, _ in cols}
            pk = cols[0][0]
            existing = {row[0] for row in conn.execute(f"SELECT {db.q(pk)} FROM {db.q(table)}")}
            renamed: dict[str, str] = {}
            records = []
            for raw in rows:
                if raw[index[pk]] is None:
                    continue
                values = {name: _cell(raw[index[name]], typ) for name, typ in cols}
                original = values[pk]
                if original in existing:
                    prefixed = f"P-{original}"
                    if prefixed in existing:
                        raise ValueError(f"{table}.{pk}={prefixed} 仍然冲突")
                    renamed[original] = prefixed
                    values[pk] = prefixed
                    existing.add(prefixed)
                else:
                    existing.add(original)
                for column, ref in _PRIVATE_FK.get(table, {}).items():
                    current = values[column]
                    if current in renames.get(ref, {}):
                        values[column] = renames[ref][current]
                records.append(tuple(values[name] for name, _ in cols) + (db.RAW, None, stamp))
            names = [name for name, _ in cols] + db.META_COLUMNS
            conn.executemany(
                f"INSERT INTO {db.q(table)} ({', '.join(db.q(n) for n in names)}) "
                f"VALUES ({', '.join('?' * len(names))})",
                records,
            )
            renames[table] = renamed
            counts[table] = len(records)
    finally:
        wb.close()
    issues = check_quality(conn)
    conn.execute("DELETE FROM data_quality")
    conn.executemany(
        "INSERT INTO data_quality(table_name, record_id, customer_id, issue) VALUES (?,?,?,?)",
        [(i["table"], i["record_id"], i["customer_id"], i["issue"]) for i in issues],
    )
    conn.commit()
    return {"counts": counts, "prefixed": {table: len(ids) for table, ids in renames.items()}, "issues": len(issues)}


def reset_generated(conn: sqlite3.Connection) -> None:
    for table in db.BUSINESS_TABLES:
        conn.execute(f"DELETE FROM {db.q(table)} WHERE source = ?", (db.GENERATED,))
    for table in db.RUNTIME_TABLES:
        conn.execute(f"DELETE FROM {db.q(table)}")
    conn.commit()
