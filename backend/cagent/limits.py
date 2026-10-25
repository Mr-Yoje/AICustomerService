from __future__ import annotations

from pathlib import Path

import yaml

from . import settings

_DEFAULTS = {
    "max_turns": 12,
    "max_steps": 8,
    "token_budget": 12000,
    "max_request_bytes": 32768,
    "allowed_base_urls": ["https://api.deepseek.com"],
}


def load_limits(config_dir: Path | None = None) -> dict:
    """读取轮次、步数、token 预算、请求体上限和外呼地址白名单。"""
    path = Path(config_dir or settings.CONFIG_DIR) / "limits.yaml"
    data = dict(_DEFAULTS)
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        data.update({k: loaded[k] for k in _DEFAULTS if k in loaded})
    data["max_turns"] = max(1, int(data["max_turns"]))
    data["max_steps"] = max(0, int(data["max_steps"]))
    data["token_budget"] = max(1, int(data["token_budget"]))
    data["max_request_bytes"] = max(1024, int(data["max_request_bytes"]))
    data["allowed_base_urls"] = [str(u).rstrip("/") for u in data["allowed_base_urls"]]
    return data
