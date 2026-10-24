import sqlite3

import pytest

from cagent import db, importer


def count(conn, table, where="1=1"):
    return conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE {where}').fetchone()[0]


def test_import_counts(db_conn):
    assert count(db_conn, "dim_customer") == 10
    assert count(db_conn, "dim_loan") == 30
    assert count(db_conn, "fact_overdue") == 40
    assert count(db_conn, "fact_call_history") == 50
    assert count(db_conn, "dim_compliance_rule") == 10


def test_imported_rows_marked_raw(db_conn):
    assert count(db_conn, "fact_overdue", f"source='{db.RAW}'") == 40


def test_quality_reports_loan_owner_mismatch(db_conn):
    assert count(db_conn, "data_quality", "issue LIKE '%不一致%'") == 36


def test_raw_rows_are_read_only(db_conn):
    with pytest.raises(sqlite3.DatabaseError):
        db_conn.execute("UPDATE dim_customer SET age=1 WHERE customer_id='C0001'")
    with pytest.raises(sqlite3.DatabaseError):
        db_conn.execute("DELETE FROM fact_overdue WHERE overdue_id='O0001'")


def test_generated_rows_can_be_reset(db_conn):
    db.insert_generated(db_conn, "fact_call_history", {
        "call_id": "CH-T1", "customer_id": "C0001", "call_time": "2026-10-24 10:00",
        "call_method": "AI对话", "talk_summary": "测试", "call_result": "已沟通", "agent_id": "AI01",
    }, session_id="S-T1")
    db_conn.commit()
    assert count(db_conn, "fact_call_history") == 51
    importer.reset_generated(db_conn)
    assert count(db_conn, "fact_call_history") == 50


def test_insert_generated_rejects_unknown_column(db_conn):
    with pytest.raises(ValueError):
        db.insert_generated(db_conn, "fact_call_history", {"bad": 1}, session_id=None)
