from __future__ import annotations

import re
import uuid
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from pydantic import BaseModel

_current: ContextVar[str | None] = ContextVar("trace_id", default=None)
_TRACE_ID = re.compile(r"^TR-[0-9a-fA-F]{10}$")


def current_trace_id() -> str | None:
    return _current.get()


def push_trace_id(trace_id: str):
    return _current.set(trace_id)


def pop_trace_id(token) -> None:
    _current.reset(token)


def new_trace_id() -> str:
    return "TR-" + uuid.uuid4().hex[:10]


def accept_trace_id(raw: str | None) -> str:
    """沿用调用方给出的合法编号，否则新生成一个。"""
    text = (raw or "").strip()
    return text if _TRACE_ID.match(text) else new_trace_id()


def jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


@dataclass
class TraceEvent:
    step: str
    name: str
    status: str
    detail: dict[str, Any] = field(default_factory=dict)
    ms: int = 0


class Trace:
    def __init__(self, sink: Callable[[TraceEvent], None] | None = None, trace_id: str | None = None):
        self.trace_id = trace_id or "TR-" + uuid.uuid4().hex[:10]
        self.events: list[TraceEvent] = []
        self._sink = sink

    def add(self, step: str, name: str, status: str = "ok", ms: int = 0, **detail: Any) -> TraceEvent:
        event = TraceEvent(step, name, status, jsonable(detail), ms)
        self.events.append(event)
        if self._sink:
            self._sink(event)
        return event

    def to_list(self) -> list[dict]:
        return [asdict(e) for e in self.events]
