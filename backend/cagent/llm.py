from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Callable, Iterable, Iterator, Protocol

from .desensitize import MaskMap, display_mask, mask
from .limits import load_limits

# 流式片段末尾可能是未闭合的占位符或被截断的号码，需等后续片段到达后再还原与脱敏
_HOLD = re.compile(r"(<[^>]*|(?:客户?)?[\dXx]*)$")


class LLMError(Exception):
    pass


_session_id: ContextVar[str | None] = ContextVar("llm_session_id", default=None)
log = logging.getLogger("cagent")


def push_llm_session(session_id: str | None):
    return _session_id.set(session_id)


def pop_llm_session(token) -> None:
    _session_id.reset(token)


def set_llm_session(session_id: str | None) -> None:
    _session_id.set(session_id)


@contextmanager
def bind_session(session_id: str | None):
    token = push_llm_session(session_id)
    try:
        yield
    finally:
        pop_llm_session(token)


def usage_since(conn: sqlite3.Connection, after_id: int) -> dict:
    """汇总一次回复期间新写入的大模型调用。没有调用时 calls 为 0。"""
    rows = conn.execute(
        "SELECT prompt_tokens, completion_tokens, cache_hit_tokens FROM llm_calls WHERE id > ?",
        (after_id,)).fetchall()
    return {
        "prompt_tokens": sum(r["prompt_tokens"] or 0 for r in rows),
        "completion_tokens": sum(r["completion_tokens"] or 0 for r in rows),
        "cache_hit_tokens": sum(r["cache_hit_tokens"] or 0 for r in rows),
        "calls": len(rows),
    }


def _usage_fields(usage) -> dict:
    if usage is None:
        return {}
    get = usage.get if isinstance(usage, dict) else lambda k, d=None: getattr(usage, k, d)
    prompt = get("prompt_tokens") or 0
    hit = get("prompt_cache_hit_tokens")
    miss = get("prompt_cache_miss_tokens")
    if hit is None and miss is None:
        details = get("prompt_tokens_details")
        cached = 0
        if isinstance(details, dict):
            cached = details.get("cached_tokens") or 0
        elif details is not None:
            cached = getattr(details, "cached_tokens", 0) or 0
        hit, miss = cached, max(prompt - cached, 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": get("completion_tokens") or 0,
        "cache_hit_tokens": hit or 0,
        "cache_miss_tokens": miss or 0,
    }


class LLMClient(Protocol):
    def complete_json(self, system: str, user: str, *, max_tokens: int = 800) -> dict: ...

    def stream_text(self, system: str, user: str, *, max_tokens: int = 800) -> Iterator[str]: ...


def _allowed_base_url(base: str | None) -> str:
    url = (base or "").rstrip("/")
    allowed = set(load_limits()["allowed_base_urls"])
    if url not in allowed:
        raise LLMError("大模型地址不在允许名单")
    return url


class DeepSeekClient:
    def __init__(self, cfg: dict):
        from openai import OpenAI

        key = os.getenv("DEEPSEEK_API_KEY")
        if not key:
            raise LLMError("未设置 DEEPSEEK_API_KEY")
        self.model = os.getenv("DEEPSEEK_MODEL", cfg.get("model", "deepseek-flash"))
        base_url = _allowed_base_url(os.getenv("DEEPSEEK_BASE_URL", cfg.get("base_url")))
        self.client = OpenAI(api_key=key, base_url=base_url, timeout=cfg.get("timeout_s", 15), max_retries=0)
        self.temperature = cfg.get("temperature", 0)
        self.retries = cfg.get("max_retries", 1)
        self.max_tokens = cfg.get("max_tokens", 800)
        self.extra_body = cfg.get("extra_body") or {}
        self.last_usage: dict = {}

    def _messages(self, system: str, user: str) -> list[dict]:
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def complete_json(self, system: str, user: str, *, max_tokens: int = 800) -> dict:
        self.last_usage = {}
        last: Exception | None = None
        for attempt in range(1, self.retries + 2):
            if attempt > 1:
                time.sleep(min(0.2 * (2 ** (attempt - 2)), 1.0))
            try:
                resp = self.client.chat.completions.create(
                    model=self.model, messages=self._messages(system, user), response_format={"type": "json_object"},
                    temperature=self.temperature, max_tokens=max_tokens or self.max_tokens, extra_body=self.extra_body,
                )
                self.last_usage = _usage_fields(getattr(resp, "usage", None))
                content = (resp.choices[0].message.content or "").strip()
                if not content:
                    raise LLMError("模型返回空内容")
                return json.loads(content)
            except Exception as e:  # noqa: BLE001 网络、解析、空响应统一重试
                last = e
        raise LLMError(f"大模型调用失败: {last}")

    def stream_text(self, system: str, user: str, *, max_tokens: int = 800) -> Iterator[str]:
        try:
            self.last_usage = {}
            stream = self.client.chat.completions.create(
                model=self.model, messages=self._messages(system, user), temperature=self.temperature,
                max_tokens=max_tokens or self.max_tokens, extra_body=self.extra_body, stream=True,
                stream_options={"include_usage": True},
            )
            for chunk in stream:
                usage = _usage_fields(getattr(chunk, "usage", None))
                if usage:
                    self.last_usage = usage
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if delta:
                    yield delta
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"大模型流式调用失败: {e}") from e


class FakeLLM:
    def __init__(self, responder: Callable[[str, str], dict] | None = None, fail: bool = False):
        self.responder = responder
        self.fail = fail
        self.calls: list[tuple[str, str]] = []
        self.last_usage: dict = {}
        self.model = "fake"

    def complete_json(self, system: str, user: str, *, max_tokens: int = 800) -> dict:
        self.calls.append((system, user))
        if self.fail:
            raise LLMError("模拟失败")
        return self.responder(system, user) if self.responder else {}

    def stream_text(self, system: str, user: str, *, max_tokens: int = 800) -> Iterator[str]:
        out = self.complete_json(system, user, max_tokens=max_tokens)
        yield from str(out.get("text", ""))


def build_llm(cfg: dict) -> DeepSeekClient | None:
    try:
        return DeepSeekClient(cfg)
    except LLMError:
        return None


def _unmask(value, mm: MaskMap):
    if isinstance(value, str):
        return mm.unmask(value)
    if isinstance(value, dict):
        return {k: _unmask(v, mm) for k, v in value.items()}
    if isinstance(value, list):
        return [_unmask(v, mm) for v in value]
    return value


class GuardedLLM:
    def __init__(self, client: LLMClient | None, conn: sqlite3.Connection):
        self.client = client
        self.conn = conn
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self.client is not None

    def _log(self, purpose: str, masked_input: str, masked_output: str, status: str, start: float) -> None:
        usage = getattr(self.client, "last_usage", None) or {}
        with self._lock:
            self.conn.execute(
                "INSERT INTO llm_calls(ts, purpose, masked_input, masked_output, status, latency_ms, session_id, model, "
                "prompt_tokens, completion_tokens, cache_hit_tokens, cache_miss_tokens) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), purpose, masked_input[:4000], masked_output[:4000],
                 status, int((time.perf_counter() - start) * 1000), _session_id.get(),
                 getattr(self.client, "model", None), usage.get("prompt_tokens"), usage.get("completion_tokens"),
                 usage.get("cache_hit_tokens"), usage.get("cache_miss_tokens")),
            )
            self.conn.commit()
        log.info("大模型 %s %s %dms", purpose, status, int((time.perf_counter() - start) * 1000))

    def _names(self, extra: Iterable[str]) -> list[str]:
        found: list[str] = []
        try:
            found = [r[0] for r in self.conn.execute("SELECT customer_name FROM dim_customer") if r[0]]
        except Exception:  # noqa: BLE001 库尚未建表时仍按调用方给出的名单掩码
            found = []
        return [n for n in (*extra, *found) if n]

    def _over_budget(self) -> bool:
        sid = _session_id.get()
        if not sid:
            return False
        used = self.conn.execute(
            "SELECT COALESCE(SUM(COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0)),0) "
            "FROM llm_calls WHERE session_id=?", (sid,)).fetchone()[0]
        return used >= load_limits()["token_budget"]

    def json(self, purpose: str, system: str, payload: dict, *, names: Iterable[str] = ()) -> dict:
        if self.client is None:
            raise LLMError("大模型不可用")
        if self._over_budget():
            raise LLMError("已达到本会话 token 预算")
        masked_user, mm = mask(json.dumps(payload, ensure_ascii=False), names=self._names(names))
        full_system = f"任务代号：{purpose}\n{system}"
        start = time.perf_counter()
        try:
            out = self.client.complete_json(full_system, masked_user)
        except LLMError as e:
            self._log(purpose, masked_user, str(e), "error", start)
            raise
        self._log(purpose, masked_user, json.dumps(out, ensure_ascii=False), "ok", start)
        return _unmask(out, mm)

    def stream(self, purpose: str, system: str, payload: dict, *, names: Iterable[str] = ()) -> Iterator[str]:
        if self.client is None:
            raise LLMError("大模型不可用")
        if self._over_budget():
            raise LLMError("已达到本会话 token 预算")
        masked_user, mm = mask(json.dumps(payload, ensure_ascii=False), names=self._names(names))
        full_system = f"任务代号：{purpose}\n{system}"
        start = time.perf_counter()
        raw, pending = [], ""
        try:
            for piece in self.client.stream_text(full_system, masked_user):
                raw.append(piece)
                pending += piece
                cut = _HOLD.search(pending).start()
                if cut:
                    yield display_mask(mm.unmask(pending[:cut]))
                    pending = pending[cut:]
        except LLMError as e:
            self._log(purpose, masked_user, str(e), "error", start)
            raise
        if pending:
            yield display_mask(mm.unmask(pending))
        self._log(purpose, masked_user, "".join(raw), "ok", start)
