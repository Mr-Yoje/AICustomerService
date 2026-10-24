from datetime import datetime

import pytest

from cagent import settings
from cagent.agents.main import MODE_COLLECTION, MODE_HANDOFF, MODE_OPERATOR, MainAgent
from cagent.llm import FakeLLM
from cagent.agents.ops import SKILL_NAMES, TOOL_NAMES
from cagent.cases import CaseStore
from cagent.config_center import ConfigCenter

NOW = datetime(2026, 10, 24, 10, 0)


@pytest.fixture
def main(db_conn, db_path):
    return MainAgent(db_conn, db_path=db_path, config_dir=settings.CONFIG_DIR, offline=True, sync_post=True)


def test_shipped_packs_valid_against_real_registries(main, db_conn):
    cc = ConfigCenter(db_conn, settings.CONFIG_DIR)
    for pid in cc.pack_ids():
        assert main.validate(cc.get("business_pack", pid)[0]) == []


def test_main_registry_has_no_query_tools(main):
    assert main.registry.owner == "main"
    assert not (TOOL_NAMES | SKILL_NAMES) & set(main.registry.names())
    assert (TOOL_NAMES | SKILL_NAMES) <= set(main.ops.registry.names())


def test_lost_contact_repair_uses_customer_history(main, db_conn):
    from cagent import db
    db.insert_generated(db_conn, "fact_call_history", {
        "call_id": "CH-LOST", "customer_id": "C0001", "call_time": "2026-10-20 09:00",
        "call_method": "电话", "talk_summary": "无人接听", "call_result": "无人接听", "agent_id": "A01",
    }, None)
    db.insert_generated(db_conn, "fact_user_behavior", {
        "behavior_id": "B-LOST", "customer_id": "C0001", "behavior_type": "APP登录",
        "behavior_time": "2025-01-01 08:00", "behavior_detail": "长期未再登录",
    }, None)
    db_conn.commit()
    t = main.handle("该客户连续多次无人接听、APP已长期未登录，请给出失联修复方案。",
                    role="operator", customer_id="C0001", now=NOW)
    assert t.speaker == "operator" and "查询客户画像或欠款" not in t.reply
    assert "C0001" in t.reply and "无人接听" in t.reply and "修复" in t.reply
    assert any(e.get("step") == "call" for e in t.trace)


def test_quiet_hours_question(main):
    t = main.handle("现在晚上11点了，能给C0003打电话吗？", now=NOW)
    assert t.speaker == "operator" and t.system_intent == "合规检测"
    assert "R004" in t.hit_rules and "不可以" in t.reply and "08:00" in t.reply


def test_clear_speaker_beats_selected_role(main):
    t = main.handle("如果你今天不还款，我们就上门催收！", role="operator", customer_id="C0004", now=NOW)
    assert t.speaker == "agent" and t.speaker_method == "关键词兜底"
    assert "R002" in t.hit_rules


def test_selected_role_only_when_speaker_unclear(main):
    t = main.handle("你好", role="operator", customer_id="C0004", now=NOW)
    assert t.speaker == "operator" and t.speaker_method == "显式角色"


def test_second_turn_speaker_sees_previous_round(db_conn, db_path):
    def responder(system, user):
        if system.startswith("任务代号：speaker"):
            if "可以先还一笔吗" in user and "上一轮" not in user:
                return {"speaker": "agent", "confidence": 0.95}
            return {"speaker": "customer", "confidence": 0.95}
        if system.startswith("任务代号：understand"):
            return {"customer_intent": "其他", "emotion": "平静", "emotion_intensity": 0.2, "confidence": 0.9}
        if system.startswith("任务代号：reply"):
            return {"reply": "可以，我们先看其中一笔。"}
        return {"hits": []}

    agent = MainAgent(db_conn, db_path=db_path, config_dir=settings.CONFIG_DIR, llm_client=FakeLLM(responder),
                      sync_post=True)
    first = agent.handle("我手头紧，想商量还款", role="customer", customer_id="C0003", now=NOW)
    assert first.mode == MODE_COLLECTION
    second = agent.handle("可以先还一笔吗", session_id=first.session_id, role="customer", now=NOW)
    assert second.speaker == "customer" and second.mode == MODE_COLLECTION
    assert "先看其中一笔" in second.reply


def test_agent_speech_audit(main, db_conn):
    t = main.handle("你今天不还就上门找你", now=NOW)
    assert t.speaker == "agent" and t.reply.startswith("[合规拦截]")
    assert "R002" in t.hit_rules and t.handoff
    assert any(r["kind"] == "主管复核" for r in CaseStore(db_conn).todos())


def test_customer_flow_handoff_and_postcall(main, db_conn):
    t = main.handle("能不能分12期", role="customer", customer_id="C0003", now=NOW)
    assert t.mode == MODE_COLLECTION and t.system_intent == "分期方案协商" and "12期" in t.reply
    sid = t.session_id
    t2 = main.handle("给我转人工", session_id=sid, now=NOW)
    assert t2.mode == MODE_HANDOFF and t2.handoff and t2.handoff_package_id
    assert CaseStore(db_conn).artifacts(session_id=sid, kind="移交包")
    t3 = main.handle("我们会联系你的家人", session_id=sid, now=NOW)
    assert t3.reply.startswith("[合规拦截]") and "R009" in t3.hit_rules
    main.finish_handoff(sid, "客户同意下月10号还款", NOW)
    call = db_conn.execute("SELECT * FROM fact_call_history WHERE call_id=?", (f"CH-{sid}",)).fetchone()
    assert call is not None and call["agent_id"] == "AI02+人工"
    assert CaseStore(db_conn).get_session(sid)["mode"] == MODE_OPERATOR


def test_customer_refuses_three_times_agent_ends(main, db_conn):
    sid = main.handle("没钱，不还", role="customer", customer_id="C0003", now=NOW).session_id
    assert main.handle("还不了", session_id=sid, now=NOW).mode == MODE_COLLECTION
    t = main.handle("说了不还", session_id=sid, now=NOW)
    assert t.mode == MODE_OPERATOR and not t.handoff and t.end_reason == "客户累计明确拒绝3次"
    assert any("主动结束" in p for p in t.key_points)
    s = CaseStore(db_conn).get_session(sid)
    assert s["ended_at"] and s["end_reason"] == "客户累计明确拒绝3次" and sid not in main.agents
    assert "拒绝跟进" in [r["kind"] for r in CaseStore(db_conn).todos(session_id=sid)]


def test_summary_report(main):
    t = main.handle("生成本月催收报告", now=NOW)
    assert t.system_intent == "催收报告" and "催收次数" in t.reply


def test_outbound_blocked_at_night(main, db_conn):
    t = main.handle("对C0001发起催收", now=datetime(2026, 10, 24, 23, 30))
    assert "R004" in t.hit_rules and "拦截" in t.reply
    kinds = [r["kind"] for r in CaseStore(db_conn).todos(session_id=t.session_id)]
    assert "下次可联系" in kinds
    call = db_conn.execute("SELECT call_method FROM fact_call_history WHERE call_id=?", (f"CH-{t.session_id}",)).fetchone()
    assert call["call_method"] == "外呼拦截"


def test_outbound_daytime_then_operator_ends(main, db_conn):
    t = main.handle("对C0001发起催收", now=NOW)
    assert t.mode == MODE_COLLECTION and "工号AI01" in t.reply
    end = main.handle("结束催收", role="operator", session_id=t.session_id, now=NOW)
    assert "已结束" in end.reply
    call = db_conn.execute("SELECT call_method FROM fact_call_history WHERE call_id=?", (f"CH-{t.session_id}",)).fetchone()
    assert call["call_method"] == "AI对话"


def test_operator_queries_and_bad_input(main):
    assert "O0033" in main.handle("查询C0001的欠款", now=NOW).reply
    assert "还款意愿" in main.handle("看下C0002的客户画像", now=NOW).reply
    empty = main.handle("", now=NOW)
    assert empty.needs_review and empty.speaker == "unknown"
