from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from .trace import Trace

_POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="cagent-call")


class CallError(Exception):
    def __init__(self, name: str, reason: str, retryable: bool = True):
        super().__init__(f"{name}: {reason}")
        self.name = name
        self.reason = reason
        self.retryable = retryable


@dataclass
class Spec:
    name: str
    kind: str
    fn: Callable[[Any], Any]
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    description: str
    timeout_s: float
    retries: int


class Registry:
    def __init__(self, owner: str):
        self.owner = owner
        self._specs: dict[str, Spec] = {}

    def register(self, name, fn, input_model, output_model, *, kind, description, timeout_s=5.0, retries=1):
        if kind not in ("tool", "skill"):
            raise ValueError(f"kind 只能是 tool 或 skill: {kind}")
        if name in self._specs:
            raise ValueError(f"重复注册: {name}")
        self._specs[name] = Spec(name, kind, fn, input_model, output_model, description, timeout_s, retries)

    def has(self, name: str) -> bool:
        return name in self._specs

    def names(self, kind: str | None = None) -> list[str]:
        return [s.name for s in self._specs.values() if kind is None or s.kind == kind]

    def describe(self) -> list[dict]:
        return [{
            "owner": self.owner, "name": s.name, "kind": s.kind, "description": s.description,
            "input": s.input_model.model_json_schema(), "output": s.output_model.model_json_schema(),
            "timeout_s": s.timeout_s, "retries": s.retries,
        } for s in self._specs.values()]

    def call(self, name: str, trace: Trace, **kwargs) -> BaseModel:
        spec = self._specs.get(name)
        if spec is None:
            trace.add("call", name, "error", owner=self.owner, error="未注册")
            raise CallError(name, "未注册", retryable=False)
        try:
            args = spec.input_model(**kwargs)
        except ValidationError as e:
            trace.add("call", name, "error", owner=self.owner, error=f"参数校验失败: {e.errors()[:3]}")
            raise CallError(name, "参数校验失败", retryable=False) from e
        last = ""
        for attempt in range(1, spec.retries + 2):
            start = time.perf_counter()
            future = _POOL.submit(spec.fn, args)
            try:
                raw = future.result(timeout=spec.timeout_s)
                out = raw if isinstance(raw, spec.output_model) else spec.output_model.model_validate(raw)
                trace.add("call", name, "ok", ms=int((time.perf_counter() - start) * 1000), owner=self.owner,
                          kind=spec.kind, attempt=attempt, input=args, output=out)
                return out
            except FutureTimeout:
                last = f"超时（>{spec.timeout_s}s）"
            except ValidationError as e:
                last = f"输出校验失败: {e.errors()[:2]}"
            except Exception as e:  # noqa: BLE001 工具内部任意异常都需要被诊断并重试
                last = f"{type(e).__name__}: {e}"
            status = "retry" if attempt <= spec.retries else "error"
            trace.add("call", name, status, ms=int((time.perf_counter() - start) * 1000), owner=self.owner,
                      kind=spec.kind, attempt=attempt, error=last)
        raise CallError(name, last)
