import sqlite3

import pytest

from cagent import db, importer, settings


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


def _insert_raw_customer(conn, customer_id):
    cols = [c for c, _ in db.BUSINESS_TABLES["dim_customer"]]
    conn.execute(
        f"INSERT INTO dim_customer ({', '.join(db.q(c) for c in cols)}, source, session_id, written_at) "
        f"VALUES ({', '.join('?' * (len(cols) + 3))})",
        (customer_id, "测", 30, "工", 1, 1, "区", "级", db.RAW, None, "2026-10-24T20:00:00"),
    )


def test_append_private_prefixes_colliding_keys(tmp_path):
    conn = db.connect(tmp_path / "eval.db")
    importer.import_all(conn, settings.DATA_DIR)
    public_owner = conn.execute("SELECT customer_id FROM dim_loan WHERE loan_id='L0001'").fetchone()[0]
    workbook = settings.DATA_DIR / "非公开加试" / "03_非公开题所需业务数据_现场提供.xlsx"

    added = importer.append_private(conn, workbook)

    assert added["counts"]["dim_customer"] == 20
    assert added["prefixed"]["dim_customer"] == 0
    assert added["prefixed"]["dim_loan"] > 0
    assert conn.execute("SELECT customer_id FROM dim_loan WHERE loan_id='L0001'").fetchone()[0] == public_owner
    private_loan = conn.execute("SELECT customer_id FROM dim_loan WHERE loan_id='P-L0001'").fetchone()
    assert private_loan is not None and private_loan[0] != public_owner
    overdue = conn.execute(
        "SELECT loan_id, customer_id FROM fact_overdue WHERE overdue_id='P-O0001'"
    ).fetchone()
    assert overdue["loan_id"] == "P-L0012" and overdue["customer_id"] == "C0017"
    assert count(conn, "dim_customer", "customer_id='C0011'") == 1
    assert count(conn, "data_quality", "customer_id >= 'C0011' AND issue LIKE '%不存在%'") == 0
    conn.close()


def test_sync_raw_copies_original_rows_and_leaves_runtime(tmp_path):
    source, dest = db.connect(tmp_path / "collection.db"), db.connect(tmp_path / "eval.db")
    importer.import_all(source, settings.DATA_DIR)
    _insert_raw_customer(source, "C0099")
    db.insert_generated(source, "dim_customer", {"customer_id": "C0098", "customer_name": "生成"}, session_id="S-live")
    source.execute(
        "INSERT INTO sessions(session_id, customer_id, mode, started_at) VALUES ('S-live', 'C0001', '催收对话模式', '2026-10-24T20:00:00')"
    )
    source.execute(
        "INSERT INTO artifacts(artifact_id, session_id, kind, content, created_at) VALUES ('ART-live', 'S-live', '催收报告', '{}', '2026-10-24T20:00:00')"
    )
    source.commit()
    importer.import_all(dest, settings.DATA_DIR)
    _insert_raw_customer(dest, "C0088")
    dest.execute(
        "INSERT INTO sessions(session_id, customer_id, mode, started_at) VALUES ('S-old', 'C0001', '催收对话模式', '2026-10-24T19:00:00')"
    )
    dest.commit()

    importer.sync_raw(dest, source)

    dest_ids = {r[0] for r in dest.execute("SELECT customer_id FROM dim_customer")}
    assert "C0099" in dest_ids and "C0098" not in dest_ids and "C0088" not in dest_ids
    assert count(dest, "dim_customer", f"source='{db.GENERATED}'") == 0
    assert count(dest, "sessions") == 0 and count(dest, "artifacts") == 0
    assert count(dest, "data_quality", "issue LIKE '%不一致%'") == 36
    assert count(source, "sessions") == 1 and count(source, "artifacts") == 1
    assert count(source, "dim_customer", "customer_id='C0098'") == 1
    source.close()
    dest.close()
