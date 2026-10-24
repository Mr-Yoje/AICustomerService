from __future__ import annotations

import copy
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from cagent.agents.collection import CollectionAgent
from cagent.config_center import SINGLETON_KINDS, ConfigError
from cagent.installment import load_templates

router = APIRouter(prefix="/api", tags=["配置"])
KINDS = (*SINGLETON_KINDS, "business_pack")


class ConfigBrief(BaseModel):
    kind: str
    key: str
    version: str
    name: str | None = None


class ConfigDoc(BaseModel):
    kind: str
    key: str
    version: str
    content: dict[str, Any]


class ConfigVersion(BaseModel):
    version: str
    created_at: str
    content: dict[str, Any]


class SaveIn(BaseModel):
    content: dict[str, Any]


class CopyIn(BaseModel):
    new_key: str
    name: str


class RuleRow(BaseModel):
    rule_id: str
    rule_type: str | None
    rule_content: str | None
    violation_level: str | None
    action: str | None
    checkpoints: list[str]


class TemplateRow(BaseModel):
    template_id: str
    template_type: str | None
    max_periods: int | None
    rate: float | None
    fee: float | None
    condition: str | None
    cond: dict[str, Any]


class RegistryGroup(BaseModel):
    owner: str
    items: list[dict[str, Any]]


def _svc(request: Request):
    return request.app.state.service


def _kind(kind: str) -> str:
    if kind not in KINDS:
        raise HTTPException(404, f"未知配置类型: {kind}")
    return kind


def _doc(cc, kind: str, key: str) -> ConfigDoc:
    try:
        content, version = cc.get(kind, key)
    except ConfigError as e:
        raise HTTPException(404, str(e)) from e
    return ConfigDoc(kind=kind, key=key, version=version, content=content)


def _save_pack(svc, key: str, content: dict) -> ConfigDoc:
    errors = ([] if content.get("pack_id") == key else [f"pack_id 必须与业务包编号一致: {key}"]) + svc.agent.validate(content)
    if errors:
        raise HTTPException(422, {"errors": errors})
    version = svc.agent.cc.save("business_pack", key, content)
    return ConfigDoc(kind="business_pack", key=key, version=version, content=content)


@router.get("/config")
def list_config(request: Request) -> list[ConfigBrief]:
    svc = _svc(request)
    cc = svc.agent.cc
    with svc.lock:
        out = [ConfigBrief(kind=k, key="default", version=cc.get(k)[1]) for k in SINGLETON_KINDS]
        for pid in cc.pack_ids():
            pack, version = cc.get("business_pack", pid)
            out.append(ConfigBrief(kind="business_pack", key=pid, version=version, name=pack.get("name")))
    return out


@router.get("/config/{kind}/{key}")
def get_config(kind: str, key: str, request: Request) -> ConfigDoc:
    svc = _svc(request)
    with svc.lock:
        return _doc(svc.agent.cc, _kind(kind), key)


@router.put("/config/{kind}/{key}", responses={422: {"description": "校验失败，detail.errors 为错误清单"}})
def save_config(kind: str, key: str, body: SaveIn, request: Request) -> ConfigDoc:
    svc = _svc(request)
    with svc.lock:
        _doc(svc.agent.cc, _kind(kind), key)
        if kind == "business_pack":
            return _save_pack(svc, key, body.content)
        version = svc.agent.cc.save(kind, key, body.content)
        svc.agent.reload()
        return ConfigDoc(kind=kind, key=key, version=version, content=body.content)


@router.get("/config/{kind}/{key}/versions")
def config_versions(kind: str, key: str, request: Request) -> list[ConfigVersion]:
    svc = _svc(request)
    with svc.lock:
        rows = svc.agent.conn.execute(
            "SELECT version, created_at, content FROM config_versions WHERE kind=? AND key=? "
            "ORDER BY created_at DESC, rowid DESC", (_kind(kind), key)).fetchall()
    return [ConfigVersion(version=r["version"], created_at=r["created_at"], content=json.loads(r["content"]))
            for r in rows]


@router.post("/config/business_pack/{key}/copy", responses={422: {"description": "校验失败"}})
def copy_pack(key: str, body: CopyIn, request: Request) -> ConfigDoc:
    svc = _svc(request)
    with svc.lock:
        if body.new_key in svc.agent.cc.pack_ids():
            raise HTTPException(409, f"业务包已存在: {body.new_key}")
        pack = copy.deepcopy(_doc(svc.agent.cc, "business_pack", key).content)
        pack.update(pack_id=body.new_key, name=body.name)
        pack["match"] = {**pack.get("match", {}), "default": False}
        return _save_pack(svc, body.new_key, pack)


@router.get("/rules")
def list_rules(request: Request) -> list[RuleRow]:
    svc = _svc(request)
    with svc.lock:
        checkpoints = svc.agent.cc.get("checkpoints")[0]["checkpoints"]
        rows = svc.agent.conn.execute("SELECT rule_id, rule_type, rule_content, violation_level, action "
                                      "FROM dim_compliance_rule ORDER BY rule_id").fetchall()
    return [RuleRow(**dict(r), checkpoints=[cp["name"] for cp in checkpoints.values()
                                            if r["rule_id"] in cp.get("rules", [])]) for r in rows]


@router.get("/templates")
def list_templates(request: Request) -> list[TemplateRow]:
    svc = _svc(request)
    with svc.lock:
        return [TemplateRow(**t) for t in load_templates(svc.agent.conn)]


@router.get("/registry")
def registry(request: Request) -> list[RegistryGroup]:
    svc = _svc(request)
    agent = svc.agent
    with svc.lock:
        groups = [
            RegistryGroup(owner="main", items=agent.registry.describe()),
            RegistryGroup(owner="ops", items=agent.ops.registry.describe()),
        ]
        for pid in agent.cc.pack_ids():
            pack = agent.cc.get("business_pack", pid)[0]
            groups.append(RegistryGroup(owner=f"collection:{pid}",
                                        items=CollectionAgent.describe_registry(pack, agent.conn, agent.policy)))
    return groups
