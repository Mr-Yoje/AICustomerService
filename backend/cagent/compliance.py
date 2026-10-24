from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime

from pydantic import BaseModel

from .catalog import list_effective
from .clock import in_quiet_hours, next_allowed_time
from .llm import GuardedLLM, LLMError

NOT_CONTACT_METHODS = ("外呼拦截", "客户进线")
CONTACT_ACTIONS = ("外呼", "发起催收")

SEMANTIC_SYSTEM = (
    "你是金融催收合规审核员。只依据给定的规则清单判断坐席话术是否违规，不得引用清单以外的任何法规、政策或条款。\n"
    "否定表述（例如“不会上门”“无法承诺减免”）不构成违规。\n"
    '输出 json，例如 {"hits": [{"rule_id": "R002", "evidence": "话术中的原文片段", "confidence": 0.9}]}，'
    '没有违规时输出 {"hits": []}。'
)


class RuleHit(BaseModel):
    rule_id: str
    rule_type: str
    rule_content: str
    level: str
    action: str
    method: str
    evidence: str
    confidence: float = 1.0


class CheckResult(BaseModel):
    checkpoint: str
    passed: bool
    hits: list[RuleHit] = []
    block: bool = False
    handoff: bool = False
    degraded: bool = False
    signals: list[str] = []
    next_allowed: str | None = None
    notes: list[str] = []


def contact_count_on(conn: sqlite3.Connection, customer_id: str, day: date) -> int:
    sql = ("SELECT COUNT(*) FROM fact_call_history WHERE customer_id=? AND substr(call_time,1,10)=? "
           "AND COALESCE(call_method,'') NOT IN (?,?)")
    return conn.execute(sql, (customer_id, day.isoformat(), *NOT_CONTACT_METHODS)).fetchone()[0]


class ComplianceEngine:
    def __init__(self, conn: sqlite3.Connection, cfg: dict, version: str, llm: GuardedLLM | None = None):
        self.conn = conn
        self.cfg = cfg
        self.version = version
        self.llm = llm
        self.negations = cfg.get("negations", [])
        self.rules = {r["rule_id"]: r for r in list_effective(conn, "dim_compliance_rule", "rule_id")}

    def _hit(self, rule: dict, checkpoint: str, method: str, evidence: str, confidence: float = 0.8) -> RuleHit:
        overrides = self.cfg["checkpoints"][checkpoint].get("action_overrides") or {}
        return RuleHit(rule_id=rule["rule_id"], rule_type=rule["rule_type"], rule_content=rule["rule_content"],
                       level=rule["violation_level"], action=overrides.get(rule["rule_id"], rule["action"]),
                       method=method, evidence=evidence, confidence=confidence)

    def _match(self, text: str, det: dict) -> str | None:
        patterns = list(det.get("patterns", [])) + [re.escape(k) for k in det.get("keywords", [])]
        for p in patterns:
            for m in re.finditer(p, text):
                prefix = text[max(0, m.start() - 6):m.start()]
                if any(n in prefix for n in self.negations):
                    continue
                return m.group(0)
        return None

    def _semantic(self, text: str, rules: dict[str, dict], checkpoint: str) -> list[RuleHit]:
        payload = {"话术": text, "规则清单": [
            {"rule_id": rid, "规则类型": r["rule_type"], "规则内容": r["rule_content"]} for rid, r in rules.items()]}
        out = self.llm.json("compliance_semantic", SEMANTIC_SYSTEM, payload)
        min_conf = self.cfg.get("semantic_min_confidence", 0.7)
        hits = []
        for h in out.get("hits") or []:
            rid = h.get("rule_id")
            try:
                conf = float(h.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0.0
            if rid in rules and conf >= min_conf:
                hits.append(self._hit(rules[rid], checkpoint, "语义", str(h.get("evidence", ""))[:80], conf))
        return hits

    def check_agent_speech(self, text: str, *, extra_rules=(), pressure_so_far: int = 0,
                           use_semantic: bool = True) -> CheckResult:
        cp = "agent_speech"
        enabled = self.cfg["checkpoints"][cp]["rules"]
        candidates = [(rid, self.rules[rid], self.cfg["detectors"].get(rid, {})) for rid in enabled if rid in self.rules]
        candidates += [(er["rule_id"], er, er) for er in extra_rules]
        hits: list[RuleHit] = []
        for rid, rule, det in candidates:
            if "pressure_patterns" in det:
                current = sum(len(re.findall(p, text)) for p in det["pressure_patterns"])
                count = pressure_so_far + current
                if current and count > det.get("max_per_turn", 2):
                    hits.append(self._hit(rule, cp, "计数", f"施压表述累计{count}次"))
                continue
            evidence = self._match(text, det)
            if evidence:
                hits.append(self._hit(rule, cp, "关键词", evidence))
        degraded = False
        if use_semantic:
            hit_ids = {h.rule_id for h in hits}
            sem = {rid: rule for rid, rule, det in candidates if rid not in hit_ids and det.get("semantic")}
            if sem:
                if self.llm is not None and self.llm.available:
                    try:
                        hits += self._semantic(text, sem, cp)
                    except LLMError:
                        degraded = True
                else:
                    degraded = True
        handoff = any(h.level == "严重" or h.action == "人工接管" for h in hits)
        notes = ["语义判定不可用，仅按关键词规则判定，需人工复核"] if degraded else []
        return CheckResult(checkpoint=cp, passed=not hits, hits=hits, block=bool(hits), handoff=handoff,
                           degraded=degraded, notes=notes)

    def check_operator_action(self, *, action: str, customer_id: str | None, now: datetime,
                              only: list[str] | None = None, contact_target: str = "本人",
                              masked: bool = True) -> CheckResult:
        cp = "operator_action"
        hits: list[RuleHit] = []
        next_allowed = None
        for rid in self.cfg["checkpoints"][cp]["rules"]:
            rule = self.rules.get(rid)
            if rule is None or (only is not None and rid not in only):
                continue
            det = self.cfg["detectors"].get(rid, {})
            if "window" in det and action in CONTACT_ACTIONS:
                s, e = det["window"]["start"], det["window"]["end"]
                if in_quiet_hours(now, s, e):
                    hits.append(self._hit(rule, cp, "时段", f"{s}-次日{e}"))
                    next_allowed = next_allowed_time(now, s, e).isoformat(timespec="minutes")
            elif "max_per_day" in det and action in CONTACT_ACTIONS and customer_id:
                n = contact_count_on(self.conn, customer_id, now.date())
                if n >= det["max_per_day"]:
                    hits.append(self._hit(rule, cp, "频次", f"{customer_id} 在 {now:%Y-%m-%d} 已联系{n}次，单日上限{det['max_per_day']}次"))
            elif det.get("operator_check") == "contact_target" and action in CONTACT_ACTIONS and contact_target != "本人":
                hits.append(self._hit(rule, cp, "联系对象", f"联系对象为{contact_target}"))
            elif det.get("operator_check") == "export_masked" and action == "导出" and not masked:
                hits.append(self._hit(rule, cp, "导出脱敏", "导出文件未脱敏"))
        return CheckResult(checkpoint=cp, passed=not hits, hits=hits, block=bool(hits), next_allowed=next_allowed)

    def check_customer_input(self, text: str) -> CheckResult:
        signals = self.cfg["checkpoints"]["customer_input"]["signals"]
        found = [name for name, words in signals.items() if any(w in text for w in words)]
        return CheckResult(checkpoint="customer_input", passed=True, signals=found)
