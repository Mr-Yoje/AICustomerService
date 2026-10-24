from __future__ import annotations

import re
from typing import Iterator

from .llm import GuardedLLM, LLMError

NARRATE_SYSTEM = (
    "你是催收系统的运营助理，根据【事实稿】回答运营人员的问题。\n"
    "只能使用事实稿中的信息，不得增加事实稿以外的任何数字、日期、规则编号或结论；尖括号占位符原样保留。\n"
    "先给结论，再分点说明；语言简洁，不超过事实稿篇幅的 1.5 倍；直接输出正文，不要输出 json。"
)

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_ORDINAL = re.compile(r"(?m)^\s*\d{1,2}[.、)）](?!\d)")
_CHUNK = 4

Event = tuple[str, dict]


def _numbers(text: str) -> set[float]:
    return {float(n) for n in _NUMBER.findall(text)}


def numbers_ok(answer: str, facts: str) -> bool:
    """答案中的数字（不含行首列表序号）必须都出现在事实稿中。"""
    return _numbers(_ORDINAL.sub("", answer)) <= _numbers(facts)


def _chunks(text: str) -> Iterator[Event]:
    for i in range(0, len(text), _CHUNK):
        yield "delta", {"text": text[i:i + _CHUNK]}


def _trace(status: str, source: str, **detail) -> Event:
    return "trace", {"step": "reply", "name": "运营回答生成", "status": status, "ms": 0,
                     "detail": {"source": source, **detail}}


def narrate(llm: GuardedLLM, facts: str, *, question: str) -> Iterator[Event]:
    """产出 ("delta", {"text"}) / ("replace", {"text"}) / ("trace", 轨迹事件)；最终显示文本以 replace 为准。"""
    if not llm.available:
        yield _trace("ok", "事实稿", reason="大模型不可用")
        yield from _chunks(facts)
        return
    parts: list[str] = []
    try:
        for piece in llm.stream("narrate", NARRATE_SYSTEM, {"问题": question, "事实稿": facts}):
            parts.append(piece)
            yield "delta", {"text": piece}
    except LLMError as e:
        yield _trace("warn", "事实稿", reason=str(e))
        if parts:
            yield "replace", {"text": facts}
        else:
            yield from _chunks(facts)
        return
    answer = "".join(parts)
    if not answer.strip() or not numbers_ok(answer, facts):
        yield _trace("warn", "事实稿", reason="数字核对未通过，已改用事实稿" if answer.strip() else "大模型返回空内容")
        yield "replace", {"text": facts}
        return
    yield _trace("ok", "大模型")
