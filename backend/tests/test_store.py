from datetime import datetime

import pytest

from cagent.cases import CaseStore
from cagent.memory import MemoryStore

NOW = datetime(2026, 10, 24, 10, 0)


def test_memory_set_get_correct_clear(db_conn):
    m = MemoryStore(db_conn)
    m.set("session", "S1", "promise", {"amount": 3000}, "第2轮")
    m.set("session", "S1", "promise", {"amount": 5000}, "第3轮")
    assert m.get("session", "S1", "promise") == {"amount": 5000}
    assert len(m.history("session", "S1", "promise")) == 2
    m.correct("session", "S1", "promise", {"amount": 4000}, operator="运营甲", ts=NOW)
    entry = m.all("session", "S1")["promise"]
    assert entry["value"] == {"amount": 4000} and entry["source"] == "人工纠正：运营甲"
    assert m.clear("session", "S1", "promise", operator="运营甲", ts=NOW) == 1
    assert m.get("session", "S1", "promise", "无") == "无"
    ops = db_conn.execute("SELECT scene FROM audit_log WHERE checkpoint='记忆操作' ORDER BY rowid").fetchall()
    assert [r["scene"] for r in ops] == ["纠正", "清除"]


def test_memory_expires_and_caps_active_keys(db_conn):
    m = MemoryStore(db_conn)
    m.set("customer", "C0001", "old", {"v": 1}, "测试", ts=datetime(2025, 1, 1))
    assert m.get("customer", "C0001", "old", "无") == "无"
    for i in range(32):
        m.set("customer", "C0002", f"k{i}", i, "测试")
    assert len(m.all("customer", "C0002")) == 30
    assert m.get("customer", "C0002", "k0", "无") == "无"
    assert m.get("customer", "C0002", "k31") == 31


def test_case_session_messages_and_context(db_conn):
    cs = CaseStore(db_conn)
    case_id = cs.create_case("C0001", "consumer_finance", NOW)
    sid = cs.open_session(case_id, "C0001", "运营模式", "consumer_finance", "v1", "外呼", NOW)
    for i in range(8):
        cs.add_message(sid, "客户" if i % 2 == 0 else "AI坐席", f"第{i}句", {"intent": "要求分期"} if i % 2 == 0 else {}, NOW)
    msgs = cs.messages(sid)
    assert [m["turn"] for m in msgs] == list(range(1, 9))
    ctx = cs.context(sid, keep_last=6)
    assert len(ctx["recent"]) == 6 and "要求分期" in ctx["summary"]
    cs.update_session(sid, mode="催收对话模式")
    assert cs.get_session(sid)["mode"] == "催收对话模式"
    with pytest.raises(ValueError):
        cs.update_session(sid, customer_id="C0002")


def test_out_of_order_messages_follow_time(db_conn):
    cs = CaseStore(db_conn)
    case_id = cs.create_case("C0001", "consumer_finance", NOW)
    sid = cs.open_session(case_id, "C0001", "催收对话模式", "consumer_finance", "v1", "外呼", NOW)
    cs.add_message(sid, "客户", "后写入", {}, datetime(2026, 10, 24, 11, 0))
    cs.add_message(sid, "AI坐席", "时间更早", {}, datetime(2026, 10, 24, 10, 0))
    msgs = cs.messages(sid)
    assert [m["text"] for m in msgs] == ["时间更早", "后写入"]
    assert msgs[0]["meta"]["乱序已重排"] is True


def test_case_status_artifacts_todos_archive(db_conn):
    cs = CaseStore(db_conn)
    case_id = cs.create_case("C0002", "consumer_finance", NOW)
    with pytest.raises(ValueError):
        cs.set_case_status(case_id, "不存在的状态", NOW)
    cs.set_case_status(case_id, "已完成", NOW)
    aid = cs.add_artifact(case_id, "S9", "催收报告", {"score": 60}, NOW)
    assert cs.artifacts(case_id=case_id)[0]["content"] == {"score": 60}
    assert cs.latest_artifact("C0002", "催收报告")["artifact_id"] == aid
    t1 = cs.add_todo(case_id, "S9", "承诺跟进", "跟进", "2026-10-30", NOW, todo_id="TD-S9-1")
    t2 = cs.add_todo(case_id, "S9", "承诺跟进", "跟进", "2026-10-30", NOW, todo_id="TD-S9-1")
    assert t1 == t2 and len(cs.todos(case_id=case_id)) == 1
    cs.archive_case(case_id, NOW)
    assert cs.get_case(case_id)["status"] == "已归档"
    assert cs.artifacts(case_id=case_id)[0]["archived"] == 1
    assert cs.search_cases(customer_id="C0002", status="已归档")[0]["case_id"] == case_id
