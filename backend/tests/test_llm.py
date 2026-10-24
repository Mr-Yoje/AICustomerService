import json

import pytest

from cagent.desensitize import display_mask, export_mask, mask, redact
from cagent.llm import FakeLLM, GuardedLLM, LLMError


def test_mask_and_unmask_roundtrip():
    text = "客户0001，身份证110101199001011234，手机13812345678，欠款12000元"
    masked, mm = mask(text)
    for raw in ("客户0001", "110101199001011234", "13812345678", "12000元"):
        assert raw not in masked
    assert mm.unmask(masked) == text


def test_mask_extra_names_and_same_token():
    masked, mm = mask("张三说张三会还", names=["张三"])
    assert masked.count("<NAME_1>") == 2


def test_display_mask():
    out = display_mask("客户0001 13812345678 110101199001011234")
    assert "客户**01" in out and "138****5678" in out and "110101********1234" in out


def test_display_mask_hides_export_plaintext():
    out = export_mask("C0001 王先生欠款32600元，李女士30岁，销售，信用评分720")
    assert "C0001" not in out and "32600" not in out and "王先生" not in out and "李女士" not in out
    assert "720" not in out and "**元" in out
    hidden = redact({"profile": {"customer_name": "王先生", "monthly_income": 8000, "credit_score": 720,
                                 "age": 30, "occupation": "销售"}, "reply": "欠款32600元"})
    assert hidden["profile"] == {"customer_name": "**", "monthly_income": "**", "credit_score": "**",
                                 "age": "**", "occupation": "**"}
    assert "32600" not in hidden["reply"]


def test_guarded_llm_masks_input_logs_and_unmasks(db_conn):
    def responder(system, user):
        assert "客户0003" not in user
        token = json.loads(user)["text"].split("，")[0]
        return {"echo": token}

    llm = GuardedLLM(FakeLLM(responder), db_conn)
    out = llm.json("unit", "测试", {"text": "客户0003，欠款20000元"})
    assert out == {"echo": "客户0003"}
    row = db_conn.execute("SELECT purpose, masked_input, status FROM llm_calls").fetchone()
    assert row["purpose"] == "unit" and row["status"] == "ok" and "客户0003" not in row["masked_input"]


def test_guarded_llm_failure_logged_and_raised(db_conn):
    llm = GuardedLLM(FakeLLM(fail=True), db_conn)
    with pytest.raises(LLMError):
        llm.json("unit", "测试", {"text": "x"})
    assert db_conn.execute("SELECT status FROM llm_calls").fetchone()["status"] == "error"


def test_guarded_llm_unavailable(db_conn):
    llm = GuardedLLM(None, db_conn)
    assert llm.available is False
    with pytest.raises(LLMError):
        llm.json("unit", "测试", {})


def test_system_prompt_carries_purpose(db_conn):
    seen = []
    llm = GuardedLLM(FakeLLM(lambda s, u: seen.append(s) or {}), db_conn)
    llm.json("speaker", "判断说话人", {})
    assert seen[0].splitlines()[0] == "任务代号：speaker"
