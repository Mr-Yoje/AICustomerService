"""合规规则与分期模板的生效行。

原始导入行不修改。编辑时另写一条来源为「系统生成」的覆盖行；同一编号再次保存只更新这条覆盖行。
读取时同一编号只取覆盖行，没有覆盖行才用原始导入行。
"""
from __future__ import annotations

import re
import sqlite3

from .db import BUSINESS_TABLES, GENERATED, insert_generated, now_iso, q

LEVELS = ("轻微", "中等", "严重")
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")


def list_effective(conn: sqlite3.Connection, table: str, id_col: str) -> list[dict]:
    cols = [c for c, _ in BUSINESS_TABLES[table]]
    listed = ", ".join(q(c) for c in cols)
    sql = (
        f"SELECT {listed} FROM ("
        f"SELECT {listed}, ROW_NUMBER() OVER ("
        f"PARTITION BY {q(id_col)} "
        f"ORDER BY CASE \"source\" WHEN '{GENERATED}' THEN 0 ELSE 1 END, \"written_at\" DESC, rowid DESC"
        f") AS _rn FROM {q(table)}) WHERE _rn = 1 ORDER BY {q(id_col)}"
    )
    return [dict(r) for r in conn.execute(sql)]


def rule_errors(body: dict, *, rule_id: str | None = None) -> list[str]:
    errors = _id_errors(rule_id) if rule_id is not None else []
    if not _text(body.get("rule_type")):
        errors.append("类型不能为空")
    if not _text(body.get("rule_content")):
        errors.append("规则内容不能为空")
    if body.get("violation_level") not in LEVELS:
        errors.append("级别只能是轻微、中等或严重")
    if not _text(body.get("action")):
        errors.append("处置不能为空")
    return errors


def template_errors(body: dict, *, template_id: str | None = None) -> list[str]:
    errors = _id_errors(template_id) if template_id is not None else []
    if not _text(body.get("template_type")):
        errors.append("类型不能为空")
    periods = body.get("max_periods")
    if isinstance(periods, bool) or not isinstance(periods, int) or periods < 1:
        errors.append("最长期数必须是正整数")
    for key, label in (("rate", "年化利率"), ("fee", "手续费率")):
        value = body.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            errors.append(f"{label}必须是不小于 0 的数字")
    if body.get("condition") is None:
        errors.append("适用条件不能为空值")
    return errors


def save_overlay(conn: sqlite3.Connection, table: str, id_col: str, id_value: str, fields: dict) -> dict:
    cols = [c for c, _ in BUSINESS_TABLES[table]]
    generated = conn.execute(
        f"SELECT rowid FROM {q(table)} WHERE {q(id_col)}=? AND source=? ORDER BY written_at DESC, rowid DESC LIMIT 1",
        (id_value, GENERATED)).fetchone()
    if generated:
        sets = ", ".join(f"{q(k)}=?" for k in fields)
        conn.execute(f"UPDATE {q(table)} SET {sets}, \"written_at\"=? WHERE rowid=?",
                     [*fields.values(), now_iso(), generated["rowid"]])
    else:
        current = conn.execute(f"SELECT * FROM {q(table)} WHERE {q(id_col)}=? ORDER BY rowid LIMIT 1", (id_value,)).fetchone()
        if current is None:
            raise KeyError(id_value)
        row = {c: current[c] for c in cols}
        row.update(fields)
        row[id_col] = id_value
        insert_generated(conn, table, row, None)
    conn.commit()
    return next(r for r in list_effective(conn, table, id_col) if r[id_col] == id_value)


def create_row(conn: sqlite3.Connection, table: str, id_col: str, row: dict) -> dict:
    id_value = row[id_col]
    exists = conn.execute(f"SELECT 1 FROM {q(table)} WHERE {q(id_col)}=?", (id_value,)).fetchone()
    if exists:
        raise FileExistsError(id_value)
    insert_generated(conn, table, row, None)
    conn.commit()
    return next(r for r in list_effective(conn, table, id_col) if r[id_col] == id_value)


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _id_errors(value: str | None) -> list[str]:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        return ["编号需以字母开头，只含字母、数字、下划线或连字符"]
    return []
