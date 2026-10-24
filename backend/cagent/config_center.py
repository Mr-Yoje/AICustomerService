from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import yaml

SINGLETON_KINDS = ("llm", "checkpoints", "policy", "intents", "main_agent")
REQUIRED_PACK_KEYS = ("pack_id", "name", "match", "pre_tools", "gates", "stages", "stage_advance", "intent_actions",
                      "skill_params", "handoff", "speech", "post_tasks")


class ConfigError(Exception):
    pass


class ConfigCenter:
    def __init__(self, conn: sqlite3.Connection, config_dir: Path):
        self.conn = conn
        self.dir = Path(config_dir)
        self._sync_files()

    @staticmethod
    def version_of(content: dict) -> str:
        raw = json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:10]

    def _files(self) -> list[tuple[str, str, Path]]:
        items = [(k, "default", self.dir / f"{k}.yaml") for k in SINGLETON_KINDS]
        items += [("business_pack", p.stem, p) for p in sorted((self.dir / "business_packs").glob("*.yaml"))]
        return [i for i in items if i[2].exists()]

    def _sync_files(self) -> None:
        for kind, key, path in self._files():
            content = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            self.conn.execute(
                "INSERT OR IGNORE INTO config_versions(kind, key, version, content, created_at) VALUES (?,?,?,?,?)",
                (kind, key, self.version_of(content), json.dumps(content, ensure_ascii=False),
                 datetime.now().isoformat(timespec="microseconds")),
            )
        self.conn.commit()

    def get(self, kind: str, key: str = "default") -> tuple[dict, str]:
        row = self.conn.execute(
            "SELECT content, version FROM config_versions WHERE kind=? AND key=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (kind, key),
        ).fetchone()
        if row is None:
            raise ConfigError(f"配置不存在: {kind}/{key}")
        return json.loads(row["content"]), row["version"]

    def save(self, kind: str, key: str, content: dict) -> str:
        version = self.version_of(content)
        self.conn.execute(
            "INSERT INTO config_versions(kind, key, version, content, created_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(kind, key, version) DO UPDATE SET created_at=excluded.created_at",
            (kind, key, version, json.dumps(content, ensure_ascii=False),
             datetime.now().isoformat(timespec="microseconds")),
        )
        self.conn.commit()
        return version

    def pack_ids(self) -> list[str]:
        rows = self.conn.execute("SELECT DISTINCT key FROM config_versions WHERE kind='business_pack'")
        return sorted(r["key"] for r in rows)


def validate_pack(pack: dict, *, main_tools: set[str], collection_actions: set[str], rule_ids: set[str],
                  post_tasks: set[str]) -> list[str]:
    errors = [f"缺少字段 {k}" for k in REQUIRED_PACK_KEYS if k not in pack]
    if errors:
        return errors
    for item in pack["pre_tools"]:
        if item.get("tool") not in main_tools:
            errors.append(f"前置工具不存在: {item.get('tool')}")
        if item.get("on_failure") not in ("block", "degrade", "warn"):
            errors.append(f"前置工具 {item.get('tool')} 的失败策略非法: {item.get('on_failure')}")
    for rid in pack["gates"]:
        if rid not in rule_ids:
            errors.append(f"门禁引用的规则不存在: {rid}")
    stage_ids = [s["id"] for s in pack["stages"]]
    if len(stage_ids) != len(set(stage_ids)):
        errors.append("阶段编号重复")
    for s in pack["stages"]:
        if s.get("next") is not None and s["next"] not in stage_ids:
            errors.append(f"阶段 {s['id']} 的下一阶段不存在: {s['next']}")
    for intent, target in pack["stage_advance"].items():
        if target not in stage_ids:
            errors.append(f"意图 {intent} 跳转的阶段不存在: {target}")
    for intent, actions in pack["intent_actions"].items():
        for a in actions:
            if a not in collection_actions:
                errors.append(f"意图 {intent} 的动作不存在: {a}")
    for er in pack.get("extra_rules", []):
        if er.get("rule_id") in rule_ids:
            errors.append(f"业务专属规则编号与规则库冲突: {er.get('rule_id')}")
        if not er.get("patterns"):
            errors.append(f"业务专属规则缺少 patterns: {er.get('rule_id')}")
    for name in pack["post_tasks"]:
        if name not in post_tasks:
            errors.append(f"后置任务不存在: {name}")
    for key in ("opening", "clarify", "handoff", "default"):
        if key not in pack["speech"].get("templates", {}):
            errors.append(f"话术模板缺少 {key}")
    return errors
