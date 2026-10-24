from __future__ import annotations

import re
from datetime import datetime, time, timedelta

_CN = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

_PATTERN = re.compile(
    r"(?:现在|当前|此刻|目前)[^，。！？,.!?\d零一二两三四五六七八九十]{0,4}?"
    r"(凌晨|早上|上午|中午|下午|傍晚|晚上|夜里|半夜)?\s*"
    r"([0-9]{1,2}|[零一二两三四五六七八九十]{1,3})\s*[点:：时]\s*(半|[0-9]{1,2})?"
)


def cn_to_int(s: str) -> int:
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + _CN[s[1]]
    if s.endswith("十"):
        return _CN[s[0]] * 10
    if "十" in s:
        a, b = s.split("十")
        return _CN[a] * 10 + _CN[b]
    return _CN[s]


def parse_time_hint(text: str, base: datetime) -> datetime | None:
    m = _PATTERN.search(text or "")
    if not m:
        return None
    period, hour_s, minute_s = m.groups()
    hour = cn_to_int(hour_s)
    minute = 30 if minute_s == "半" else int(minute_s) if minute_s else 0
    if period in ("下午", "傍晚", "晚上", "夜里") and hour < 12:
        hour += 12
    if period == "中午" and hour < 11:
        hour += 12
    if period == "半夜" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _t(s: str) -> time:
    return time.fromisoformat(s)


def in_quiet_hours(now: datetime, start: str, end: str) -> bool:
    s, e, t = _t(start), _t(end), now.time()
    if s > e:
        return t >= s or t < e
    return s <= t < e


def next_allowed_time(now: datetime, start: str, end: str) -> datetime:
    if not in_quiet_hours(now, start, end):
        return now
    e = _t(end)
    candidate = now.replace(hour=e.hour, minute=e.minute, second=0, microsecond=0)
    if now.time() >= _t(start):
        candidate += timedelta(days=1)
    return candidate
