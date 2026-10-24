import pytest

from cagent import settings
from cagent.agents.understanding import Understander, regex_entities
from cagent.config_center import ConfigCenter
from cagent.llm import FakeLLM, GuardedLLM


@pytest.fixture
def cfg(db_conn):
    return ConfigCenter(db_conn, settings.CONFIG_DIR).get("intents")[0]


@pytest.fixture
def offline(db_conn, cfg):
    return Understander(GuardedLLM(None, db_conn), cfg)


def test_preprocess(offline):
    assert offline.preprocess("   ") == ("", "空输入")
    assert offline.preprocess("@@##$$%%^^&&**")[1] == "乱码"
    text, issue = offline.preprocess("好" * 600)
    assert issue == "超长已截断" and len(text) == 500
    assert offline.preprocess("能不能分期？") == ("能不能分期？", None)


def test_speaker_fallback_markers(offline):
    assert offline.speaker("你好，我是XX金融催收专员，需要核实还款计划").speaker == "agent"
    op = offline.speaker("请生成本月催收报告")
    assert op.speaker == "operator" and op.method == "关键词兜底"
    cu = offline.speaker("我最近手头紧")
    assert cu.speaker == "customer" and cu.needs_clarify
    assert offline.speaker("查询C0001的欠款").speaker == "operator"


def test_speaker_llm_and_low_confidence(db_conn, cfg):
    def responder(system, user):
        assert "任务代号：speaker" in system
        return {"speaker": "operator", "confidence": 0.9} if "报告" in user else {"speaker": "customer", "confidence": 0.3}

    u = Understander(GuardedLLM(FakeLLM(responder), db_conn), cfg)
    assert u.speaker("给我一份报告").method == "大模型"
    low = u.speaker("你今天不还就等着")
    assert low.speaker == "agent" and low.method == "关键词兜底"


def test_regex_entities():
    e = regex_entities("一次性还不上，能不能分十二期？每月能还1500元，下个月发工资")
    assert e == {"periods": 12, "monthly_affordable": 1500.0, "amount": 1500.0, "date": "下个月"}
    assert regex_entities("这是第3期的账单")["periods"] is None


def test_customer_fallback(offline):
    u = offline.customer("欠款太多了我一次性还不上，能不能分12期？")
    assert (u.customer_intent, u.periods, u.method) == ("要求分期", 12, "关键词兜底")
    assert offline.customer("你们天天打电话烦不烦！再打我就投诉").emotion == "愤怒"
    assert offline.customer("我会还，但是我不还").conflict
    assert offline.customer("那改成24期呢").customer_intent == "要求分期"


def test_customer_llm_output_normalized(db_conn, cfg):
    fake = FakeLLM(lambda s, u: {"customer_intent": "乱写", "emotion": "开心", "emotion_intensity": 3,
                                 "periods": 6, "confidence": 0.9})
    u = Understander(GuardedLLM(fake, db_conn), cfg).customer("想分12期")
    assert (u.customer_intent, u.emotion, u.emotion_intensity, u.periods) == ("要求分期", "平静", 1.0, 12)


def test_operator_intents(offline):
    q = offline.operator("现在晚上11点了，能给C0003打电话吗？")
    assert (q.intent, q.customer_id) == ("合规问答", "C0003")
    assert offline.operator("请生成本月催收报告").intent == "生成报告"
    assert offline.operator("对C0001发起催收").intent == "发起催收"
    assert offline.operator("你好").intent == "其他"
