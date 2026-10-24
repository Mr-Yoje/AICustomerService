"""deepseek-flash 计价。单价来自 https://api-docs.deepseek.com/zh-cn/quick_start/pricing （每百万 token，人民币）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

MODEL = "deepseek-flash"
CURRENCY = "CNY"
UNIT = "1M tokens"
# 高峰：北京时间周一至周五 09:00–12:00、14:00–18:00。周末全天闲时。
# 文档还豁免中国法定节假日；这里没有接入节假日日历，节假日仍按星期判断。
PEAK_NOTE = "高峰为工作日北京时间 09:00–12:00 与 14:00–18:00，价格为闲时的两倍。周末全天按闲时。文档中的中国法定节假日豁免未接入日历。"
OFF_PEAK = {"cache_hit": 0.02, "cache_miss": 1.0, "output": 4.0}
PEAK = {"cache_hit": 0.04, "cache_miss": 2.0, "output": 8.0}
_SH = timezone(timedelta(hours=8))


def is_peak(ts: datetime) -> bool:
    """无时区的时间按北京时间理解，再换算到 UTC 判断高峰。"""
    local = ts.replace(tzinfo=_SH) if ts.tzinfo is None else ts.astimezone(_SH)
    utc = local.astimezone(timezone.utc)
    if utc.weekday() >= 5:
        return False
    minutes = utc.hour * 60 + utc.minute
    return (1 * 60 <= minutes < 4 * 60) or (6 * 60 <= minutes < 10 * 60)


def call_cost(prompt_tokens, completion_tokens, cache_hit_tokens, cache_miss_tokens, ts: datetime) -> float | None:
    if prompt_tokens is None and completion_tokens is None:
        return None
    rate = PEAK if is_peak(ts) else OFF_PEAK
    hit = cache_hit_tokens or 0
    miss = cache_miss_tokens if cache_miss_tokens is not None else (prompt_tokens or 0)
    out = completion_tokens or 0
    return (hit * rate["cache_hit"] + miss * rate["cache_miss"] + out * rate["output"]) / 1_000_000


def _bucket() -> dict:
    return {"calls": 0, "unmetered": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "cache_hit_tokens": 0, "cache_miss_tokens": 0, "cost_cny": 0.0}


def _add(bucket: dict, row: dict, cost: float | None) -> None:
    bucket["calls"] += 1
    if cost is None:
        bucket["unmetered"] += 1
        return
    bucket["prompt_tokens"] += row["prompt_tokens"] or 0
    bucket["completion_tokens"] += row["completion_tokens"] or 0
    bucket["cache_hit_tokens"] += row["cache_hit_tokens"] or 0
    miss = row["cache_miss_tokens"] if row["cache_miss_tokens"] is not None else (row["prompt_tokens"] or 0)
    bucket["cache_miss_tokens"] += miss
    bucket["cost_cny"] = round(bucket["cost_cny"] + cost, 8)


def summarize(rows: list[dict]) -> dict:
    summary = _bucket()
    groups: dict[str | None, dict] = {}
    for row in rows:
        ts = datetime.fromisoformat(row["ts"])
        cost = call_cost(row["prompt_tokens"], row["completion_tokens"], row["cache_hit_tokens"],
                         row["cache_miss_tokens"], ts)
        _add(summary, row, cost)
        sid = row.get("session_id") or None
        group = groups.get(sid)
        if group is None:
            group = {**_bucket(), "session_id": sid, "customer_id": row.get("customer_id"),
                     "started_at": row.get("started_at")}
            groups[sid] = group
        _add(group, row, cost)
    sessions = sorted(groups.values(), key=lambda g: (g["session_id"] is None, -g["cost_cny"], g["session_id"] or ""))
    return {
        "model": MODEL, "currency": CURRENCY, "unit": UNIT, "off_peak": OFF_PEAK, "peak": PEAK,
        "peak_note": PEAK_NOTE, "summary": summary, "sessions": sessions,
    }
