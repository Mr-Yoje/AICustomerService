from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from pydantic import BaseModel


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
