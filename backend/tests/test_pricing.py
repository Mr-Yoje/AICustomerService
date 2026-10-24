from datetime import datetime

from cagent.llm import FakeLLM, GuardedLLM, bind_session
from cagent.pricing import call_cost, is_peak, summarize


def test_peak_follows_weekday_utc_windows():
    assert is_peak(datetime(2026, 10, 26, 9, 30))  # 周一北京 09:30 = UTC 01:30
    assert is_peak(datetime(2026, 10, 26, 15, 0))  # 周一北京 15:00 = UTC 07:00
    assert not is_peak(datetime(2026, 10, 26, 13, 0))  # 周一北京 13:00 = UTC 05:00，两段高峰之间
    assert not is_peak(datetime(2026, 10, 24, 9, 30))  # 周六


def test_input_and_output_priced_separately():
    off = datetime(2026, 10, 24, 9, 30)
    peak = datetime(2026, 10, 26, 9, 30)
    assert call_cost(3000, 500, 2000, 1000, off) == (2000 * 0.02 + 1000 * 1 + 500 * 4) / 1_000_000
    assert call_cost(3000, 500, 2000, 1000, peak) == (2000 * 0.04 + 1000 * 2 + 500 * 8) / 1_000_000
    assert call_cost(None, None, None, None, off) is None


def test_summary_groups_sessions_and_skips_unmetered():
    rows = [
        {"ts": "2026-10-24T09:00:00", "session_id": "S-1", "customer_id": "C0001", "started_at": "2026-10-24T08:00:00",
         "prompt_tokens": 1000, "completion_tokens": 0, "cache_hit_tokens": 0, "cache_miss_tokens": 1000},
        {"ts": "2026-10-24T09:05:00", "session_id": None, "customer_id": None, "started_at": None,
         "prompt_tokens": None, "completion_tokens": None, "cache_hit_tokens": None, "cache_miss_tokens": None},
    ]
    report = summarize(rows)
    assert report["summary"]["calls"] == 2 and report["summary"]["unmetered"] == 1
    assert report["summary"]["cost_cny"] == 1000 * 1 / 1_000_000
    assert report["sessions"][0]["session_id"] == "S-1"
    assert report["sessions"][1]["session_id"] is None and report["sessions"][1]["unmetered"] == 1


def test_guarded_llm_logs_usage_and_session(db_conn):
    client = FakeLLM(lambda _s, _u: {"ok": True})
    client.last_usage = {"prompt_tokens": 12, "completion_tokens": 3, "cache_hit_tokens": 8, "cache_miss_tokens": 4}
    llm = GuardedLLM(client, db_conn)
    with bind_session("S-cost"):
        llm.json("unit", "测试", {"text": "x"})
    row = db_conn.execute(
        "SELECT session_id, prompt_tokens, completion_tokens, cache_hit_tokens, cache_miss_tokens FROM llm_calls"
    ).fetchone()
    assert row["session_id"] == "S-cost"
    assert (row["prompt_tokens"], row["completion_tokens"], row["cache_hit_tokens"], row["cache_miss_tokens"]) == (12, 3, 8, 4)
