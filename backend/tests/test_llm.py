import json

import pytest

from cagent.desensitize import display_mask, export_mask, mask, present, redact
from cagent.llm import DeepSeekClient, FakeLLM, GuardedLLM, LLMError


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
    amounts = redact({"overdue_amount": 32600.5, "principal": 10000, "total_amount": 8000,
                      "overdue_days": 180, "score": 53})
    assert amounts["overdue_amount"] == "**" and amounts["principal"] == "**" and amounts["total_amount"] == "**"
    assert amounts["overdue_days"] == 180 and amounts["score"] == 53
    shown = present({"monthly_income": 8000, "reply": "欠款32600元"})
    assert shown["monthly_income"] == "**" and "32600" in shown["reply"]
    payload = '{"amount": 3000, "overdue_days": 12}'
    masked_amount, mm = mask(payload)
    assert "3000" not in masked_amount and "12" in masked_amount
    assert mm.unmask(masked_amount) == payload


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


def test_llm_retry_waits_before_next_attempt(monkeypatch):
    slept = []
    monkeypatch.setattr("cagent.llm.time.sleep", lambda seconds: slept.append(seconds))
    client = DeepSeekClient.__new__(DeepSeekClient)
    client.retries = 1
    client.model = "fake"
    client.temperature = 0
    client.max_tokens = 8
    client.extra_body = {}
    client.last_usage = {}

    class Completions:
        def create(self, **_kwargs):
            raise TimeoutError("down")

    client.client = type("C", (), {"chat": type("Chat", (), {"completions": Completions()})()})()
    with pytest.raises(LLMError):
        client.complete_json("s", "u")
    assert slept == [0.2]


def test_guarded_llm_stops_at_token_budget(db_conn, monkeypatch):
    monkeypatch.setattr("cagent.llm.load_limits", lambda: {
        "max_turns": 12, "max_steps": 8, "token_budget": 1,
        "max_request_bytes": 32768, "allowed_base_urls": ["https://api.deepseek.com"],
    })
    from cagent.llm import pop_llm_session, push_llm_session

    db_conn.execute(
        "INSERT INTO llm_calls(ts, purpose, masked_input, masked_output, status, latency_ms, session_id, model, "
        "prompt_tokens, completion_tokens, cache_hit_tokens, cache_miss_tokens) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("2026-10-25 10:00:00", "unit", "x", "y", "ok", 1, "S-budget", "fake", 5, 1, 0, 0))
    db_conn.commit()
    token = push_llm_session("S-budget")
    try:
        with pytest.raises(LLMError, match="token 预算"):
            GuardedLLM(FakeLLM(lambda _s, _u: {}), db_conn).json("unit", "测试", {"text": "x"})
    finally:
        pop_llm_session(token)


def test_system_prompt_carries_purpose(db_conn):
    seen = []
    llm = GuardedLLM(FakeLLM(lambda s, u: seen.append(s) or {}), db_conn)
    llm.json("speaker", "判断说话人", {})
    assert seen[0].splitlines()[0] == "任务代号：speaker"
