from cagent.llm import FakeLLM, GuardedLLM
from cagent.suggestions import suggest


def test_llm_items_are_used_when_three(db_conn):
    llm = GuardedLLM(FakeLLM(lambda _s, _u: {"items": ["再查联系记录", "生成评估报告", "发起催收", "多余的不要"]}), db_conn)
    assert suggest(llm, "本月没有催收记录", "operator", "C0001") == ["再查联系记录", "生成评估报告", "发起催收"]


def test_fallback_follows_reply_when_model_empty(db_conn):
    llm = GuardedLLM(FakeLLM(lambda _s, _u: {}), db_conn)
    items = suggest(llm, "当前不能外呼，处于禁止时段", "operator", "C0004")
    assert len(items) == 3 and any("打电话" in s for s in items) and any("C0004" in s for s in items)


def test_customer_fallback_picks_up_installment(db_conn):
    llm = GuardedLLM(None, db_conn)
    items = suggest(llm, "可以申请12期，月供3118元", "customer", "C0003")
    assert items[0] == "月供能不能再低一点？" and len(items) == 3
