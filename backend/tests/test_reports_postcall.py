from datetime import datetime

from cagent import db, postcall, settings
from cagent.cases import CaseStore
from cagent.config_center import ConfigCenter
from cagent.memory import MemoryStore
from cagent.postcall import PostCallRunner
from cagent.reports import (customer_report, render_report_markdown, render_summary_paragraph, render_summary_text,
                            report_reply, session_report, summary_report)

NOW = datetime(2026, 10, 24, 10, 0)


def policy_of(conn):
    return ConfigCenter(conn, settings.CONFIG_DIR).get("policy")[0]


def make_session(db_path):
    conn = db.connect(db_path)
    cases, mem = CaseStore(conn), MemoryStore(conn)
    case_id = cases.create_case("C0003", "auto_loan", NOW)
    sid = cases.open_session(case_id, "C0003", "催收对话模式", "auto_loan", "v", "外呼", NOW)
    cases.add_message(sid, "AI坐席", "您好，我是贷后服务专员", {}, NOW)
    cases.add_message(sid, "客户", "下个月10号还5000元", {"intent": "承诺还款"}, NOW)
    mem.set("session", sid, "intents", ["要求分期", "承诺还款"], "测试")
    mem.set("session", sid, "emotions", [{"turn": 1, "emotion": "焦虑", "intensity": 0.6, "text": "一次性还不上"},
                                         {"turn": 2, "emotion": "配合", "intensity": 0.3, "text": "下个月10号还"}], "测试")
    mem.set("session", sid, "promises", [{"amount": 5000, "date": "下个月10号", "turn": 2}], "测试")
    mem.set("session", sid, "negotiation", [{"turn": 1, "客户诉求": "分12期", "方案": ["T004 标准分期 12期"], "结果": "已提供方案"}], "测试")
    cases.update_session(sid, ended_at=NOW.isoformat(timespec="seconds"), end_reason="正常结束")
    conn.close()
    return sid, case_id


def test_session_and_customer_reports(db_conn):
    cid_report = customer_report(db_conn, "C0003", policy_of(db_conn), NOW)
    assert cid_report["会话编号"] is None and cid_report["逾期概况"]["逾期笔数"] == 1
    md = render_report_markdown(cid_report, ["逾期概况", "还款意愿评估", "合规审计"])
    assert "## 合规审计" in md and "## 数据冲突" in md
    assert "- 因素：\n  - 因素：还款履约；" in md


def test_summary_paragraph_links_to_report(db_conn):
    rep = summary_report(db_conn, NOW, policy_of(db_conn))
    text = render_summary_paragraph(rep)
    assert "催收" in text and "次" in text and "触达率" not in text
    assert report_reply(text, "ART-1") == text + "\n\n[查看完整报告](/reports/ART-1)"


def test_summary_report_falls_back_to_latest_month(db_conn):
    rep = summary_report(db_conn, NOW, policy_of(db_conn))
    assert rep["统计月份"] == "2025-10" and rep["本期指标"]["催收次数"] == 2 and rep["说明"]
    assert "催收次数" in render_summary_text(rep)


def test_postcall_writes_records_idempotently(db_path):
    sid, case_id = make_session(db_path)
    runner = PostCallRunner(db_path, settings.CONFIG_DIR)
    runner.enqueue(sid, sync=True)
    assert {t["status"] for t in runner.status(sid)} == {"完成"}
    for name in postcall.STAGES:
        runner.retry(sid, name=name, sync=True)
    runner.enqueue(sid, sync=True)
    conn = db.connect(db_path)
    call = conn.execute("SELECT * FROM fact_call_history WHERE session_id=?", (sid,)).fetchall()
    assert len(call) == 1 and call[0]["call_id"] == f"CH-{sid}" and call[0]["call_result"] == "承诺还款"
    assert call[0]["source"] == db.GENERATED
    assert conn.execute("SELECT COUNT(*) FROM fact_emotion_log WHERE session_id=?", (sid,)).fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM profile_snapshots WHERE session_id=?", (sid,)).fetchone()[0] == 1
    cases = CaseStore(conn)
    assert len(cases.artifacts(session_id=sid, kind="催收报告")) == 1
    assert [t["kind"] for t in cases.todos(session_id=sid)] == ["承诺跟进"]
    assert cases.get_case(case_id)["status"] == "已完成"
    rep = session_report(conn, sid, policy_of(conn), NOW)
    assert rep["协商记录"]["还款承诺"][0]["amount"] == 5000
    conn.close()


def test_postcall_single_failure_isolated(db_path, monkeypatch):
    sid, case_id = make_session(db_path)

    def boom(ctx):
        raise RuntimeError("模拟失败")

    slept = []
    monkeypatch.setattr(postcall.time, "sleep", lambda seconds: slept.append(seconds))
    monkeypatch.setitem(postcall.TASKS, "write_emotion", boom)
    runner = PostCallRunner(db_path, settings.CONFIG_DIR, max_retries=1)
    runner.enqueue(sid, sync=True)
    status = {t["name"]: t for t in runner.status(sid)}
    assert status["write_emotion"]["status"] == "失败" and status["write_emotion"]["attempts"] == 2
    assert slept == [0.2]
    assert status["write_call_record"]["status"] == "完成" and status["create_todos"]["status"] == "完成"
    conn = db.connect(db_path)
    cases = CaseStore(conn)
    assert "后置任务失败" in [t["kind"] for t in cases.todos(session_id=sid)]
    assert cases.get_case(case_id)["status"] == "后置处理中"
    conn.close()
