from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from .trace import Trace

_POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="cagent-call")
log = logging.getLogger("cagent")


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
    enabled: bool = True


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
        return [s.name for s in self._specs.values() if s.enabled and (kind is None or s.kind == kind)]

    def apply(self, items: dict[str, dict] | None) -> None:
        for name, item in (items or {}).items():
            spec = self._specs.get(name)
            if spec is None or not isinstance(item, dict):
                continue
            if isinstance(item.get("description"), str) and item["description"].strip():
                spec.description = item["description"].strip()
            timeout = item.get("timeout_s")
            if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
                spec.timeout_s = float(timeout)
            retries = item.get("retries")
            if isinstance(retries, int) and not isinstance(retries, bool) and retries >= 0:
                spec.retries = retries
            if isinstance(item.get("enabled"), bool):
                spec.enabled = item["enabled"]

    def describe(self) -> list[dict]:
        return [{
            "owner": self.owner, "name": s.name, "kind": s.kind, "description": s.description,
            "input": s.input_model.model_json_schema(), "output": s.output_model.model_json_schema(),
            "timeout_s": s.timeout_s, "retries": s.retries, "enabled": s.enabled,
        } for s in self._specs.values()]

    def call(self, name: str, trace: Trace, **kwargs) -> BaseModel:
        spec = self._specs.get(name)
        if spec is None or not spec.enabled:
            reason = "未注册" if spec is None else "已停用"
            trace.add("call", name, "error", owner=self.owner, error=reason)
            raise CallError(name, reason, retryable=False)
        try:
            args = spec.input_model(**kwargs)
        except ValidationError as e:
            trace.add("call", name, "error", owner=self.owner, error=f"参数校验失败: {e.errors()[:3]}")
            raise CallError(name, "参数校验失败", retryable=False) from e
        last = ""
        retryable = True
        for attempt in range(1, spec.retries + 2):
            if attempt > 1:
                time.sleep(min(0.2 * (2 ** (attempt - 2)), 1.0))
            start = time.perf_counter()
            future = _POOL.submit(spec.fn, args)
            try:
                raw = future.result(timeout=spec.timeout_s)
                out = raw if isinstance(raw, spec.output_model) else spec.output_model.model_validate(raw)
                ms = int((time.perf_counter() - start) * 1000)
                trace.add("call", name, "ok", ms=ms, owner=self.owner,
                          kind=spec.kind, attempt=attempt, input=args, output=out)
                log.info("工具 %s 完成 第%s次 %dms", name, attempt, ms)
                return out
            except FutureTimeout:
                last, retryable = f"超时（>{spec.timeout_s}s）", True
            except ValidationError as e:
                last, retryable = f"输出校验失败: {e.errors()[:2]}", False
            except Exception as e:  # noqa: BLE001 工具内部任意异常都需要被诊断并重试
                last, retryable = f"{type(e).__name__}: {e}", True
            more = retryable and attempt <= spec.retries
            ms = int((time.perf_counter() - start) * 1000)
            trace.add("call", name, "retry" if more else "error", ms=ms,
                      owner=self.owner, kind=spec.kind, attempt=attempt, error=last)
            log.warning("工具 %s %s 第%s次 %dms %s", name, "重试" if more else "失败", attempt, ms, last)
            if not more:
                break
        raise CallError(name, last, retryable=retryable)
