from datetime import datetime

import pytest

from cagent import settings
from cagent.agents.collection import ACTION_NAMES, CollectionAgent, Snapshot, format_speech
from cagent.agents.understanding import Understander
from cagent.cases import CaseStore
from cagent.compliance import ComplianceEngine
from cagent.config_center import ConfigCenter
from cagent.llm import FakeLLM, GuardedLLM
from cagent.loans import prioritize
from cagent.memory import MemoryStore
from cagent.registry import CallError
from cagent.trace import Trace

NOW = datetime(2026, 10, 24, 10, 0)


def make_agent(conn, pack_id="auto_loan", customer_id="C0003", llm_client=None, verified=True):
    cc = ConfigCenter(conn, settings.CONFIG_DIR)
    pack, pv = cc.get("business_pack", pack_id)
    policy = cc.get("policy")[0]
    intents = cc.get("intents")[0]
    cp, cpv = cc.get("checkpoints")
    llm = GuardedLLM(llm_client, conn)
    cases = CaseStore(conn)
    case_id = cases.create_case(customer_id, pack_id, NOW)
    sid = cases.open_session(case_id, customer_id, "催收对话模式", pack_id, pv, "客户进线", NOW)
    profile = dict(conn.execute("SELECT * FROM dim_customer WHERE customer_id=?", (customer_id,)).fetchone())
    snapshot = Snapshot(customer_id=customer_id, profile=profile,
                        overdues=[i.model_dump() for i in prioritize(conn, customer_id, policy).items])
    return CollectionAgent(conn=conn, session_id=sid, case_id=case_id, customer_id=customer_id, pack=pack,
                           audit_version=f"{pv}/{cpv}", policy=policy, intents_cfg=intents,
                           engine=ComplianceEngine(conn, cp, cpv, llm), llm=llm, understander=Understander(llm, intents),
                           memory=MemoryStore(conn), cases=cases, snapshot=snapshot, verified=verified)


def scripted(drafts):
    it = iter(drafts)

    def responder(system, user):
        purpose = system.splitlines()[0].removeprefix("任务代号：")
        if purpose == "understand":
            return {"customer_intent": "要求分期", "emotion": "焦虑", "emotion_intensity": 0.4, "confidence": 0.9}
        if purpose == "reply":
            return {"reply": next(it)}
        return {"hits": []}

    return FakeLLM(responder)


def test_registry_isolated_and_covers_actions(db_conn):
    agent = make_agent(db_conn)
    assert ACTION_NAMES <= set(agent.registry.names())
    for spec in agent.registry.describe():
        assert "customer_id" not in spec["input"].get("properties", {})
    for pid in ("auto_loan", "consumer_finance"):
        pack = ConfigCenter(db_conn, settings.CONFIG_DIR).get("business_pack", pid)[0]
        assert {a for acts in pack["intent_actions"].values() for a in acts} <= ACTION_NAMES


def test_opening_unverified_starts_at_identity(db_conn):
    agent = make_agent(db_conn, verified=False)
    out = agent.opening(NOW, Trace())
    assert agent.stage == "核身" and "工号AI02" in out.reply and out.compliance[0].passed
    assert format_speech(agent.pack, "不存在的模板") == format_speech(agent.pack, "default")


def test_installment_negotiation_multi_turn(db_conn):
    agent = make_agent(db_conn)
    out = agent.handle("欠款太多了我一次性还不上，能不能分12期？", NOW, Trace())
    assert out.system_intent == "分期方案协商" and out.stage == "协商"
    assert out.plan.plans[0].template_id == "T004" and out.plan.plans[0].periods == 12
    assert "12期" in out.reply and out.compliance[-1].passed
    out2 = agent.handle("那改成24期呢", NOW, Trace())
    assert out2.plan.plans[0].periods == 24 and "24期" in out2.reply
    assert len(agent.memory.get("session", agent.session_id, "negotiation")) == 2
    assert db_conn.execute("SELECT COUNT(*) FROM audit_log WHERE session_id=?", (agent.session_id,)).fetchone()[0] >= 2
    assert len(agent.cases.messages(agent.session_id)) == 4


def test_multi_loan_priority(db_conn):
    out = make_agent(db_conn, pack_id="consumer_finance", customer_id="C0001").handle("我到底有几笔欠款？", NOW, Trace())
    assert out.system_intent == "多贷款管理" and out.loans.items[0].overdue_id == "O0033"
    assert "5笔" in out.reply and "合计" in out.reply and "风险" in out.reply
    assert "优先" in (out.strategy or "")


def test_handoff_soothing_uses_context_and_drops_promises(db_conn):
    seen = {}

    def responder(system, user):
        purpose = system.splitlines()[0].removeprefix("任务代号：")
        if purpose == "understand":
            return {"customer_intent": "投诉", "emotion": "愤怒", "emotion_intensity": 0.95, "confidence": 0.9}
        if purpose == "soothe":
            seen["system"] = system
            seen["user"] = user
            if "第二次" in user:
                return {"reply": "后续会控制联系频次，账单改到您方便的时间。"}
            return {"reply": "听得出您现在很生气，这让人难受。"}
        return {"hits": []}

    text = "我已经打12378投诉你们了，一天打我四次电话，你们就等着挨罚吧！"
    agent = make_agent(db_conn, llm_client=FakeLLM(responder))
    out = agent.handle(text, NOW, Trace())
    assert out.handoff and out.reply.startswith("听得出您现在很生气") and "转接人工" in out.reply
    assert "控制联系频次" not in out.reply
    assert "愤怒" in seen["user"] and "一天打我四次" in seen["user"]
    assert "禁止任何承诺" in seen["system"]
    again = agent.handle("第二次还是很生气，别再打了", NOW, Trace())
    assert again.handoff and "控制联系频次" not in again.reply and "方便的时间" not in again.reply
    assert again.reply.startswith("我理解您现在的感受。")


def test_emotion_and_stop_contact_hands_off(db_conn):
    out = make_agent(db_conn).handle("我没钱！别再打了！再打我报警！", NOW, Trace())
    assert out.system_intent == "情绪感知+人工接管" and out.handoff
    assert out.reply.startswith("我理解您现在的感受。") and "转接人工" in out.reply
    assert "控制联系频次" not in out.reply and "方便的时间" not in out.reply


def test_anxious_installment_is_combo(db_conn):
    out = make_agent(db_conn).handle("我最近失业了真的很焦虑，能不能想办法帮我分期？", NOW, Trace())
    assert out.system_intent == "情绪感知+分期方案协商" and not out.handoff and out.plan
    assert "分期" in out.reply and "联系频次" not in out.reply


def test_plain_installment_stays_single_intent(db_conn):
    out = make_agent(db_conn).handle("能不能分12期？", NOW, Trace())
    assert out.system_intent == "分期方案协商" and not out.handoff


def test_amount_asked_once_then_confirmed_plan_ends(db_conn):
    agent = make_agent(db_conn)
    first = agent.handle("能不能分12期？", NOW, Trace())
    assert first.end_reason is None and "可承受" in first.reply
    second = agent.handle("每月可以还2000，还是分期", NOW, Trace())
    assert second.end_reason is None and "哪一天" in second.reply
    assert "可承受" not in second.reply and "还多少" not in second.reply
    third = agent.handle("就按这个方案，下个月10号还", NOW, Trace())
    assert third.end_reason == "客户已确认还款、还款方案和还款时间"
    assert third.stage == "结束" and "下个月10号" in third.reply
    assert "可承受" not in third.reply and "还多少" not in third.reply


def test_handoff_on_request_and_repeated_refusal(db_conn):
    agent = make_agent(db_conn)
    out = agent.handle("不跟机器人说了，给我转人工", NOW, Trace())
    assert out.handoff and out.system_intent == "人工接管" and "转接人工" in out.reply
    pkg = agent.handoff_package(out.handoff_reason, NOW)
    assert {"客户档案快照", "对话全文", "情绪轨迹", "还款意愿", "协商记录", "命中规则", "建议下一步"} <= set(pkg)
    assert "客户0003" not in str(pkg["客户档案快照"])
    agent = make_agent(db_conn)
    assert agent.handle("没钱，不还", NOW, Trace()).end_reason is None
    assert agent.handle("能不能分期", NOW, Trace()).end_reason is None
    assert agent.handle("还不了", NOW, Trace()).end_reason is None
    third = agent.handle("说了不还", NOW, Trace())
    assert third.end_reason == "客户累计明确拒绝3次" and not third.handoff and third.stage == "结束"
    shorter = make_agent(db_conn)
    shorter.pack["exit"] = {"max_refusals": 2}
    shorter.handle("没钱，不还", NOW, Trace())
    assert shorter.handle("还不了", NOW, Trace()).end_reason == "客户累计明确拒绝2次"
    unset = make_agent(db_conn)
    unset.pack.pop("exit", None)
    unset.handle("没钱，不还", NOW, Trace())
    unset.handle("还不了", NOW, Trace())
    assert unset.handle("说了不还", NOW, Trace()).end_reason is None


def test_garbled_input_asks_to_repeat(db_conn):
    out = make_agent(db_conn).handle("@@##$$%%^^&&**", NOW, Trace())
    assert "再说一遍" in out.reply and out.needs_review and not out.handoff


def test_draft_violation_rewritten(db_conn):
    agent = make_agent(db_conn, llm_client=scripted(["不还也没事，先放着吧", "可以为您办理分期，请问每月大概能还多少？"]))
    out = agent.handle("能不能分期", NOW, Trace())
    assert out.reply == "可以为您办理分期，请问每月大概能还多少？"
    assert [c.passed for c in out.compliance] == [False, True]
    assert out.compliance[0].hits[0].rule_id == "R008" and not out.handoff


def test_opening_reads_customer_promise(db_conn):
    agent = make_agent(db_conn)
    agent.memory.set("customer", "C0003", "open_promises", [{"amount": 3000, "date": "2026-11-01"}], "测试")
    out = agent.opening(NOW, Trace())
    assert "2026-11-01" in out.reply and "3000" in out.reply


def test_first_turn_uses_prepared_willingness(db_conn):
    from cagent.scoring import score_customer

    agent = make_agent(db_conn)
    data = score_customer(db_conn, "C0003", agent.policy).model_dump(mode="json")
    data["score"] = 42
    agent.snapshot.willingness = data
    called = []
    orig = agent.registry.call

    def wrapped(name, trace, **kw):
        called.append(name)
        return orig(name, trace, **kw)

    agent.registry.call = wrapped
    out = agent.handle("你好", NOW, Trace())
    assert out.willingness.score == 42 and "willingness_update" not in called
    assert "42" not in str(agent._facts(None, None, out.willingness))
    assert "不要评价客户" in agent._reply_system("询问还款安排")


def test_turn_cap_ends_without_another_reply_cycle(db_conn, monkeypatch):
    monkeypatch.setattr("cagent.agents.collection.load_limits", lambda: {
        "max_turns": 1, "max_steps": 8, "token_budget": 12000,
        "max_request_bytes": 32768, "allowed_base_urls": ["https://api.deepseek.com"],
    })
    agent = make_agent(db_conn)
    agent.cases.add_message(agent.session_id, "客户", "先还一笔", {}, NOW)
    out = agent.handle("再商量一下", NOW, Trace())
    assert out.end_reason == "达到轮次上限"
    assert "轮次上限" in out.reply


def test_step_cap_skips_extra_actions(db_conn, monkeypatch):
    monkeypatch.setattr("cagent.agents.collection.load_limits", lambda: {
        "max_turns": 12, "max_steps": 0, "token_budget": 12000,
        "max_request_bytes": 32768, "allowed_base_urls": ["https://api.deepseek.com"],
    })
    agent = make_agent(db_conn)
    trace = Trace()
    agent.handle("能不能分12期", NOW, trace)
    assert "installment_plan" not in [e.name for e in trace.events]


def test_reply_timeout_is_told_to_customer(db_conn):
    agent = make_agent(db_conn)
    orig = agent.registry.call

    def wrapped(name, trace, **kw):
        if name == "generate_reply":
            raise CallError(name, "超时（>40s）")
        return orig(name, trace, **kw)

    agent.registry.call = wrapped
    out = agent.handle("你好", NOW, Trace())
    assert out.reply.startswith("话术生成超时")


def test_severe_or_pack_rule_forces_handoff(db_conn):
    out = make_agent(db_conn, llm_client=scripted(["再不还就安排拖车"])).handle("能不能分期", NOW, Trace())
    assert out.handoff and out.compliance[0].hits[0].rule_id == "BR-AUTO-01"
    assert "转接人工" in out.reply


def _intent_script(turns):
    seq = iter(turns)

    def responder(system, user):
        purpose = system.splitlines()[0].removeprefix("任务代号：")
        if purpose == "understand":
            item = next(seq)
            return {"customer_intent": item["intent"], "emotion": "平静", "emotion_intensity": 0.2,
                    "confidence": 0.9, "current_node": item.get("current"), "next_node": item.get("next")}
        if purpose == "reply":
            return {"reply": "好的，我按您刚才说的继续。"}
        return {"hits": []}

    return FakeLLM(responder)


def _calls(llm, purpose):
    return [(system, user) for system, user in llm.calls if system.startswith(f"任务代号：{purpose}")]


def _goal(agent, stage_id):
    return next(s["goal"] for s in agent.pack["stages"] if s["id"] == stage_id)


def test_followup_nodes_come_from_context(db_conn):
    import json

    llm = _intent_script([
        {"intent": "其他"},
        {"intent": "要求分期", "current": "告知逾期", "next": "了解原因"},
        {"intent": "其他", "current": "协商", "next": "结束"},
    ])
    agent = make_agent(db_conn, pack_id="consumer_finance", customer_id="C0001", llm_client=llm)
    first = agent.handle("你好", NOW, Trace())
    assert first.stage == "告知逾期"
    system, user = _calls(llm, "reply")[-1]
    assert "按系统意图组织话术" in system and "先直接回答客户最后一句" not in system
    assert "当前节点" not in json.loads(user)

    second = agent.handle("能不能分期", NOW, Trace())
    assert second.stage == "了解原因" and second.plan
    understood = json.loads(_calls(llm, "understand")[-1][1])
    assert {n["id"] for n in understood["上下文"]["nodes"]} == {s["id"] for s in agent.pack["stages"]}
    assert "了解原因" not in _calls(llm, "understand")[-1][0]
    system, user = _calls(llm, "reply")[-1]
    payload = json.loads(user)
    assert "先直接回答客户最后一句" in system and "按系统意图组织话术" not in system
    assert _goal(agent, "了解原因") not in system
    assert payload["当前节点"] == "告知逾期" and payload["当前目标"] == _goal(agent, "告知逾期")
    assert payload["下一节点"] == "了解原因" and payload["下一节点目标"] == _goal(agent, "了解原因")
    assert payload["事实"]["逾期明细"] and payload["事实"]["分期方案"]

    third = agent.handle("说吧", NOW, Trace())
    assert third.stage == "结束"
    payload = json.loads(_calls(llm, "reply")[-1][1])
    assert payload["当前节点"] == "协商" and payload["下一节点"] == "结束"
    assert payload["下一节点目标"] == _goal(agent, "结束")
    assert payload["事实"]["分期方案"]


def test_unverified_followup_does_not_leave_identity(db_conn):
    import json

    llm = _intent_script([{"intent": "其他", "current": "核身", "next": "告知逾期"}])
    agent = make_agent(db_conn, verified=False, llm_client=llm)
    agent.opening(NOW, Trace())
    out = agent.handle("你好", NOW, Trace())
    assert out.stage == "核身"
    system, user = _calls(llm, "reply")[-1]
    payload = json.loads(user)
    assert "先直接回答客户最后一句" in system and _goal(agent, "核身") not in system
    assert payload["当前节点"] == "核身" and payload["当前目标"] == _goal(agent, "核身")
    assert "下一节点" not in payload
