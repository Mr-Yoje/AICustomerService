from cagent.llm import FakeLLM, GuardedLLM
from cagent.narrate import narrate, numbers_ok

FACTS = "C0007 名下逾期1笔，合计2000元：1. O0029 信用卡分期 逾期365天 欠款2000元。建议优先处理 O0029。"


def _texts(events):
    return "".join(d["text"] for k, d in events if k == "delta")


def test_stream_restores_split_placeholders(db_conn):
    llm = GuardedLLM(FakeLLM(lambda s, u: {"text": "共<AMOUNT_1>，逾期365天"}), db_conn)
    chunks = list(llm.stream("narrate", "系统", {"事实稿": FACTS}))
    assert len(chunks) > 1
    assert "".join(chunks) == "共2000元，逾期365天"
    row = db_conn.execute("SELECT purpose, masked_input, status FROM llm_calls").fetchone()
    assert row["purpose"] == "narrate" and "2000元" not in row["masked_input"] and row["status"] == "ok"


def test_stream_applies_display_mask(db_conn):
    llm = GuardedLLM(FakeLLM(lambda s, u: {"text": "电话13812345678"}), db_conn)
    assert "".join(llm.stream("narrate", "系统", {})) == "电话138****5678"


def test_numbers_ok():
    assert numbers_ok("C0007 逾期365天，欠款2000元，优先处理 O0029", FACTS)
    assert not numbers_ok("欠款3000元", FACTS)
    assert numbers_ok("没有数字", FACTS)


def test_numbers_ok_ignores_list_ordinals():
    assert numbers_ok("结论：\n2. 逾期365天\n3、欠款2000元", FACTS)
    assert not numbers_ok("结论：\n2. 欠款3000元", FACTS)
    assert not numbers_ok("合计 2.5 万", FACTS)


def test_narrate_streams_llm_answer(db_conn):
    llm = GuardedLLM(FakeLLM(lambda s, u: {"text": "客户名下1笔逾期，共<AMOUNT_1>。"}), db_conn)
    events = list(narrate(llm, FACTS, question="查一下C0007的欠款"))
    assert _texts(events) == "客户名下1笔逾期，共2000元。"
    assert not any(k == "replace" for k, _ in events)


def test_narrate_replaces_when_numbers_invented(db_conn):
    llm = GuardedLLM(FakeLLM(lambda s, u: {"text": "名下3笔逾期"}), db_conn)
    events = list(narrate(llm, FACTS, question="查欠款"))
    assert events[-1] == ("replace", {"text": FACTS})
    assert any(k == "trace" and d["status"] == "warn" for k, d in events)


def test_narrate_falls_back_to_facts_without_llm(db_conn):
    events = list(narrate(GuardedLLM(None, db_conn), FACTS, question="查欠款"))
    assert _texts(events) == FACTS
    assert not any(k == "replace" for k, _ in events)


def test_narrate_falls_back_when_llm_fails(db_conn):
    events = list(narrate(GuardedLLM(FakeLLM(fail=True), db_conn), FACTS, question="查欠款"))
    assert _texts(events) == FACTS
