from __future__ import annotations

import copy
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from cagent.agents.collection import CollectionAgent
from cagent.catalog import create_row, list_effective, rule_errors, save_overlay, template_errors
from cagent.config_center import SINGLETON_KINDS, ConfigError, validate_registry
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


class RuleIn(BaseModel):
    rule_type: str
    rule_content: str
    violation_level: str
    action: str


class RuleCreate(RuleIn):
    rule_id: str


class TemplateIn(BaseModel):
    template_type: str
    max_periods: int
    rate: float
    fee: float
    condition: str


class TemplateCreate(TemplateIn):
    template_id: str


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
        if kind == "registry":
            _reject(validate_registry(body.content, _registry_known(svc.agent)))
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


def _rule_rows(svc) -> list[RuleRow]:
    checkpoints = svc.agent.cc.get("checkpoints")[0]["checkpoints"]
    rows = list_effective(svc.agent.conn, "dim_compliance_rule", "rule_id")
    return [RuleRow(**r, checkpoints=[cp["name"] for cp in checkpoints.values()
                                      if r["rule_id"] in cp.get("rules", [])]) for r in rows]


def _reject(errors: list[str]) -> None:
    if errors:
        raise HTTPException(422, {"errors": errors})


def _rule_fields(body: RuleIn) -> dict:
    data = {k: v.strip() for k, v in body.model_dump().items() if k != "rule_id"}
    _reject(rule_errors(data))
    return data


@router.get("/rules")
def list_rules(request: Request) -> list[RuleRow]:
    svc = _svc(request)
    with svc.lock:
        return _rule_rows(svc)


@router.put("/rules/{rule_id}", responses={422: {"description": "校验失败，detail.errors 为错误清单"}})
def save_rule(rule_id: str, body: RuleIn, request: Request) -> RuleRow:
    svc = _svc(request)
    fields = _rule_fields(body)
    with svc.lock:
        try:
            save_overlay(svc.agent.conn, "dim_compliance_rule", "rule_id", rule_id, fields)
        except KeyError:
            raise HTTPException(404, f"规则不存在: {rule_id}") from None
        svc.agent.reload()
        return next(r for r in _rule_rows(svc) if r.rule_id == rule_id)


@router.post("/rules", responses={422: {"description": "校验失败，detail.errors 为错误清单"}})
def create_rule(body: RuleCreate, request: Request) -> RuleRow:
    svc = _svc(request)
    fields = _rule_fields(body)
    rule_id = body.rule_id.strip()
    _reject(rule_errors(fields, rule_id=rule_id))
    with svc.lock:
        try:
            create_row(svc.agent.conn, "dim_compliance_rule", "rule_id", {"rule_id": rule_id, **fields})
        except FileExistsError:
            raise HTTPException(409, f"规则已存在: {rule_id}") from None
        svc.agent.reload()
        return next(r for r in _rule_rows(svc) if r.rule_id == rule_id)


@router.get("/templates")
def list_templates(request: Request) -> list[TemplateRow]:
    svc = _svc(request)
    with svc.lock:
        return [TemplateRow(**t) for t in load_templates(svc.agent.conn)]


def _template_fields(body: TemplateIn) -> dict:
    data = body.model_dump()
    data["template_type"] = data["template_type"].strip()
    data["condition"] = data["condition"].strip()
    _reject(template_errors(data))
    return data


@router.put("/templates/{template_id}", responses={422: {"description": "校验失败，detail.errors 为错误清单"}})
def save_template(template_id: str, body: TemplateIn, request: Request) -> TemplateRow:
    svc = _svc(request)
    fields = _template_fields(body)
    with svc.lock:
        try:
            save_overlay(svc.agent.conn, "dim_installment_template", "template_id", template_id, fields)
        except KeyError:
            raise HTTPException(404, f"分期模板不存在: {template_id}") from None
        return TemplateRow(**next(t for t in load_templates(svc.agent.conn) if t["template_id"] == template_id))


@router.post("/templates", responses={422: {"description": "校验失败，detail.errors 为错误清单"}})
def create_template(body: TemplateCreate, request: Request) -> TemplateRow:
    svc = _svc(request)
    fields = _template_fields(body)
    template_id = body.template_id.strip()
    _reject(template_errors(fields, template_id=template_id))
    with svc.lock:
        try:
            create_row(svc.agent.conn, "dim_installment_template", "template_id",
                       {"template_id": template_id, **fields})
        except FileExistsError:
            raise HTTPException(409, f"分期模板已存在: {template_id}") from None
        return TemplateRow(**next(t for t in load_templates(svc.agent.conn) if t["template_id"] == template_id))
    svc = _svc(request)
    agent = svc.agent
def _registry_known(agent) -> dict[str, tuple[str, str]]:
    known: dict[str, tuple[str, str]] = {}
    for group in _registry_groups(agent):
        owner = "collection" if group.owner.startswith("collection:") else group.owner
        for item in group.items:
            known.setdefault(item["name"], (owner, item["kind"]))
    return known


def _registry_groups(agent) -> list[RegistryGroup]:
    cfg = agent.registry_items()
    groups = [
        RegistryGroup(owner="main", items=agent.registry.describe()),
        RegistryGroup(owner="ops", items=agent.ops.registry.describe()),
    ]
    for pid in agent.cc.pack_ids():
        pack = agent.cc.get("business_pack", pid)[0]
        groups.append(RegistryGroup(
            owner=f"collection:{pid}",
            items=CollectionAgent.describe_registry(pack, agent.conn, agent.policy, cfg),
        ))
    return groups


@router.get("/registry")
def registry(request: Request) -> list[RegistryGroup]:
    svc = _svc(request)
    with svc.lock:
        return _registry_groups(svc.agent)