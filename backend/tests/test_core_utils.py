import time
from datetime import datetime

import pytest
from pydantic import BaseModel

from cagent.clock import in_quiet_hours, next_allowed_time, parse_time_hint
from cagent.registry import CallError, Registry
from cagent.trace import Trace

BASE = datetime(2026, 10, 24, 10, 0)


def test_parse_evening_digits():
    assert parse_time_hint("现在晚上11点了，可以打电话催收吗？", BASE) == datetime(2026, 10, 24, 23, 0)


def test_parse_colon_time():
    assert parse_time_hint("当前 22:31 能外呼吗", BASE) == datetime(2026, 10, 24, 22, 31)


def test_parse_chinese_numeral_half():
    assert parse_time_hint("现在是晚上十一点半", BASE) == datetime(2026, 10, 24, 23, 30)


def test_no_hint_without_now_word():
    assert parse_time_hint("我3点前还款", BASE) is None


@pytest.mark.parametrize("hour,minute,expected", [(23, 0, True), (7, 59, True), (8, 0, False), (21, 59, False), (22, 0, True)])
def test_quiet_hours(hour, minute, expected):
    assert in_quiet_hours(datetime(2026, 10, 24, hour, minute), "22:00", "08:00") is expected


def test_next_allowed_after_evening():
    assert next_allowed_time(datetime(2026, 10, 24, 23, 0), "22:00", "08:00") == datetime(2026, 10, 25, 8, 0)


def test_next_allowed_early_morning():
    assert next_allowed_time(datetime(2026, 10, 24, 3, 0), "22:00", "08:00") == datetime(2026, 10, 24, 8, 0)


class In(BaseModel):
    x: int


class Out(BaseModel):
    y: int


def test_registry_call_ok():
    reg = Registry("test")
    reg.register("double", lambda a: {"y": a.x * 2}, In, Out, kind="tool", description="翻倍")
    tr = Trace()
    assert reg.call("double", tr, x=2).y == 4
    assert tr.events[-1].status == "ok"
    assert tr.events[-1].detail["output"] == {"y": 4}


def test_registry_validation_error_not_retried():
    calls = []
    reg = Registry("test")
    reg.register("double", lambda a: calls.append(1) or {"y": 1}, In, Out, kind="tool", description="d")
    with pytest.raises(CallError) as e:
        reg.call("double", Trace(), x="abc")
    assert e.value.retryable is False
    assert calls == []


def test_registry_timeout_then_retry_succeeds():
    calls = []

    def fn(a):
        calls.append(1)
        if len(calls) == 1:
            time.sleep(0.5)
        return {"y": 1}

    reg = Registry("test")
    reg.register("slow", fn, In, Out, kind="tool", description="d", timeout_s=0.1, retries=1)
    tr = Trace()
    assert reg.call("slow", tr, x=1).y == 1
    assert [e.status for e in tr.events] == ["retry", "ok"]


def test_registry_exhausted_raises():
    calls = []

    def fn(a):
        calls.append(1)
        raise RuntimeError("boom")

    reg = Registry("test")
    reg.register("bad", fn, In, Out, kind="skill", description="d", retries=1)
    with pytest.raises(CallError) as e:
        reg.call("bad", Trace(), x=1)
    assert "boom" in e.value.reason
    assert len(calls) == 2


def test_registry_output_error_is_not_retried():
    calls = []

    def fn(a):
        calls.append(1)
        return {"y": "bad"}

    reg = Registry("test")
    reg.register("bad", fn, In, Out, kind="tool", description="d", retries=2)
    with pytest.raises(CallError) as e:
        reg.call("bad", Trace(), x=1)
    assert e.value.retryable is False and calls == [1]


def test_registry_apply_overrides_and_disable_skips_call():
    reg = Registry("ops")
    reg.register("double", lambda a: {"y": a.x}, In, Out, kind="tool", description="翻倍", timeout_s=5, retries=1)
    reg.apply({"double": {"description": "改过的说明", "timeout_s": 12, "retries": 0, "enabled": False}, "missing": {}})
    info = reg.describe()[0]
    assert info["description"] == "改过的说明" and info["timeout_s"] == 12 and info["retries"] == 0
    assert info["enabled"] is False and reg.names() == []
    with pytest.raises(CallError) as e:
        reg.call("double", Trace(), x=1)
    assert e.value.reason == "已停用" and e.value.retryable is False


def test_registry_rejects_duplicate_and_describes():
    reg = Registry("owner-a")
    reg.register("t", lambda a: {"y": 1}, In, Out, kind="tool", description="d")
    with pytest.raises(ValueError):
        reg.register("t", lambda a: {"y": 1}, In, Out, kind="tool", description="d")
    info = reg.describe()[0]
    assert info["owner"] == "owner-a" and info["kind"] == "tool" and "properties" in info["input"]


def test_trace_sink_receives_events():
    got = []
    tr = Trace(sink=got.append)
    tr.add("route", "说话人判定", speaker="customer")
    assert got[0].detail == {"speaker": "customer"}
    assert tr.to_list()[0]["name"] == "说话人判定"
