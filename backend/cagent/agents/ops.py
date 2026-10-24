from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from pydantic import BaseModel

from .. import db
from ..audit import write_audit, write_event
from ..cases import CaseStore
from ..compliance import CheckResult, ComplianceEngine, contact_count_on
from ..config_center import ConfigCenter, ConfigError, registry_overrides
from ..desensitize import display_mask
from ..llm import GuardedLLM, LLMError
from ..loans import LoanPriority, prioritize
from ..memory import MemoryStore
from ..registry import CallError, Registry
from ..reports import (collection_eval_report, compliance_audit_report, customer_report, render_audit_text,
                       render_customer_text, render_eval_text, render_report_markdown, render_summary_markdown,
                       render_summary_paragraph, render_summary_text, report_reply, summary_report)
from ..scoring import Willingness, score_customer
from ..trace import Trace
from .collection import Snapshot

TOOL_NAMES = {"get_customer_profile", "get_overdues", "get_contact_count_today", "get_willingness"}
SKILL_NAMES = {"customer_report", "summary_report", "collection_eval_report", "compliance_audit_report"}
_THIRD_PARTY = re.compile(r"家人|父母|亲戚|朋友|同事|领导")
REWRITE_SYSTEM = (
    "你是催收合规质检员，把违规话术改写为合规表述：保留提醒还款的业务意图，后果只能客观说明（如影响征信），"
    "去掉威胁、辱骂、虚假承诺、冒充身份、骚扰亲友、误导等内容；"
    "不得保留第三方姓名、先生或女士称谓、工作单位和欠款金额；不超过 80 字；尖括号占位符原样保留。\n"
    '输出 json，例如 {"reply": "……"}'
)
_PRIVACY_RULES = {"R006", "R009"}
_DISCLOSURE = re.compile(r"[\u4e00-\u9fff]{1,2}(?:先生|女士)|(?<![\dA-Za-z_])\d+(?:,\d{3})*(?:\.\d+)?\s*元")


class LookupResult(BaseModel):
    customer_id: str
    loan_type: str | None = None


class CidIn(BaseModel):
    customer_id: str


class CountIn(BaseModel):
    customer_id: str
    day: str


class CountOut(BaseModel):
    customer_id: str
    day: str
    count: int


class ProfileOut(BaseModel):
    customer_id: str
    profile: dict
    behaviors: list[dict]
    emotions: list[dict]
    repayments: list[dict]
    memory: dict


class AnswerResult(BaseModel):
    reply: str
    hit_rules: list[str] = []
    compliance: list[CheckResult] = []
    willingness_score: int | None = None
    willingness_level: str | None = None
    strategy: str | None = None
    needs_review: bool = False
    review_reasons: list[str] = []


class ReportIn(BaseModel):
    customer_id: str
    now: str


class SummaryIn(BaseModel):
    now: str
    month: str | None = None


class ScopedReportIn(BaseModel):
    now: str
    customer_id: str | None = None
    text: str = ""
    month: str | None = None


class ReportOut(BaseModel):
    report: dict
    text: str


def _hit_ids(results: list[CheckResult]) -> list[str]:
    return list(dict.fromkeys(h.rule_id for r in results for h in r.hits))


class PrepareResult(BaseModel):
    blocked: str | None = None
    gate: CheckResult | None = None
    snapshot: Snapshot


class SpeechReview(BaseModel):
    passed: bool
    reply: str
    hit_rules: list[str] = []
    compliance: list[CheckResult] = []
    handoff: bool = False
    needs_review: bool = False
    review_reasons: list[str] = []


def _violation_text(r: CheckResult) -> str:
    return "\n".join(f"[合规拦截] 命中 {h.rule_id}（{h.rule_type}，{h.level}）：{h.rule_content}；"
                     f"证据「{h.evidence}」；处置：{h.action}" for h in r.hits)


class OpsAgent:
    def __init__(self, conn: sqlite3.Connection, *, cc: ConfigCenter, llm: GuardedLLM,
                 engine: ComplianceEngine, policy: dict, intents: dict, main_cfg: dict,
                 cp_version: str, cases: CaseStore, memory: MemoryStore):
        self.conn, self.cc, self.llm = conn, cc, llm
        self.engine, self.policy, self.intents = engine, policy, intents
        self.main_cfg, self.cp_version = main_cfg, cp_version
        self.cases, self.memory = cases, memory
        self.registry = self._build_registry()

    def _build_registry(self) -> Registry:
        r = Registry("ops")
        r.register("get_customer_profile", self._profile, CidIn, ProfileOut, kind="tool",
                   description="客户画像：基本信息、近期行为、情绪、还款记录与长期记忆")
        r.register("get_overdues", lambda a: prioritize(self.conn, a.customer_id, self.policy), CidIn,
                   LoanPriority, kind="tool", description="客户全部逾期记录及处理优先级")
        r.register("get_contact_count_today", lambda a: CountOut(
            customer_id=a.customer_id, day=a.day,
            count=contact_count_on(self.conn, a.customer_id, datetime.fromisoformat(a.day).date())),
                   CountIn, CountOut, kind="tool", description="客户指定日期已被联系次数（不含外呼拦截与客户进线）")
        r.register("get_willingness", lambda a: score_customer(self.conn, a.customer_id, self.policy), CidIn,
                   Willingness, kind="tool", description="对话前还款意愿评分")
        r.register("customer_report", self._customer_report, ReportIn, ReportOut, kind="skill",
                   description="单客户催收报告")
        r.register("summary_report", lambda a: self._summary(a), SummaryIn, ReportOut, kind="skill",
                   description="月度催收汇总报告")
        r.register("collection_eval_report", self._eval_report, ScopedReportIn, ReportOut, kind="skill",
                   description="本月催收评估报告，按单个客户或全部客户")
        r.register("compliance_audit_report", self._audit_report, ScopedReportIn, ReportOut, kind="skill",
                   description="合规审计报告，按单个客户或全部客户")
        r.apply(self._registry_items())
        return r

    def _registry_items(self) -> dict:
        try:
            return registry_overrides(self.cc.get("registry")[0])
        except ConfigError:
            return {}

    def _profile(self, a: CidIn) -> ProfileOut:
        cols = [c for c, _ in db.BUSINESS_TABLES["dim_customer"]]
        row = self.conn.execute(f"SELECT {', '.join(cols)} FROM dim_customer WHERE customer_id=?",
                                (a.customer_id,)).fetchone()
        if row is None:
            raise ValueError(f"客户不存在: {a.customer_id}")

        def rows(sql: str) -> list[dict]:
            return [dict(x) for x in self.conn.execute(sql, (a.customer_id,))]

        return ProfileOut(
            customer_id=a.customer_id, profile=dict(row),
            behaviors=rows("SELECT behavior_id, behavior_type, behavior_time, behavior_detail, source FROM "
                           "fact_user_behavior WHERE customer_id=? ORDER BY behavior_time DESC LIMIT 10"),
            emotions=rows("SELECT emotion_id, emotion_type, emotion_intensity, record_time, source FROM fact_emotion_log "
                          "WHERE customer_id=? ORDER BY record_time DESC LIMIT 5"),
            repayments=rows("SELECT repay_id, repay_amount, repay_time, is_on_time FROM fact_repayment "
                            "WHERE customer_id=? ORDER BY repay_time DESC LIMIT 10"),
            memory={k: v["value"] for k, v in self.memory.all("customer", a.customer_id).items()},
        )

    def lookup(self, customer_id: str) -> LookupResult:
        if self.conn.execute("SELECT 1 FROM dim_customer WHERE customer_id=?", (customer_id,)).fetchone() is None:
            raise ValueError(f"客户不存在: {customer_id}")
        items = prioritize(self.conn, customer_id, self.policy).items
        return LookupResult(customer_id=customer_id, loan_type=items[0].loan_type if items else None)

    def answer(self, intent: str, text: str, customer_id: str | None, now: datetime, trace: Trace,
               session_id: str | None = None) -> AnswerResult:
        if intent == "查询欠款":
            return self._answer_overdues(customer_id, trace)
        if intent == "查询画像":
            return self._answer_profile(customer_id, trace)
        if intent == "生成报告":
            return self._answer_report(text, customer_id, now, trace)
        if intent == "合规问答":
            return self._answer_compliance(text, customer_id, now, trace, session_id)
        if intent == "失联修复":
            return self._answer_lost_contact(customer_id, trace)
        raise ValueError(f"运营子 Agent 不处理该意图: {intent}")

    def _answer_overdues(self, customer_id: str, trace: Trace) -> AnswerResult:
        self.lookup(customer_id)
        lp = self.registry.call("get_overdues", trace, customer_id=customer_id)
        if not lp.items:
            return AnswerResult(reply=f"{customer_id} 当前无逾期记录。")
        lines = [f"{customer_id} 名下逾期{len(lp.items)}笔，合计{lp.total_amount:.0f}元（{lp.basis}）："]
        lines += [f"{n}. {i.overdue_id} {i.loan_type or '未知类型'} 逾期{i.overdue_days}天 欠款{i.overdue_amount:.0f}元"
                  for n, i in enumerate(lp.items, 1)]
        lines.append(f"{lp.suggestion}。")
        lines += [f"注意：{c}" for c in lp.conflicts]
        return AnswerResult(reply="\n".join(lines), needs_review=bool(lp.conflicts), review_reasons=lp.conflicts)

    def _answer_lost_contact(self, customer_id: str, trace: Trace) -> AnswerResult:
        self.lookup(customer_id)
        self.registry.call("get_customer_profile", trace, customer_id=customer_id)
        missed = self.conn.execute(
            "SELECT COUNT(*) FROM fact_call_history WHERE customer_id=? AND call_result='无人接听'",
            (customer_id,)).fetchone()[0]
        last_login = self.conn.execute(
            "SELECT behavior_time FROM fact_user_behavior WHERE customer_id=? AND behavior_type='APP登录' "
            "ORDER BY behavior_time DESC LIMIT 1", (customer_id,)).fetchone()
        login = f"最近 APP 登录 {last_login[0]}" if last_login else "无 APP 登录记录"
        reply = (f"{customer_id} 失联修复：通话中无人接听 {missed} 次，{login}。"
                 "建议暂停连续拨打同一号码，改约白天联系，并改用短信或 App 消息触达，请客户本人回拨。")
        return AnswerResult(reply=reply)

    def _answer_profile(self, customer_id: str, trace: Trace) -> AnswerResult:
        self.lookup(customer_id)
        p = self.registry.call("get_customer_profile", trace, customer_id=customer_id)
        w = self.registry.call("get_willingness", trace, customer_id=customer_id)
        pr = p.profile
        top = sorted(w.factors, key=lambda f: f.contribution, reverse=True)[:3]
        reply = (f"{customer_id}（{display_mask(pr['customer_name'] or '')}）：{pr['age']}岁，{pr['occupation']}，{pr['region']}，"
                 f"月收入{pr['monthly_income']:.0f}元，信用评分{pr['credit_score']}，客户等级{pr['customer_level']}。"
                 f"还款意愿{w.score}分（{w.level}，置信度{w.confidence}），主要贡献："
                 + "、".join(f"{f.name}{f.score:.0f}分" for f in top) + "。")
        if w.needs_review:
            reply += "需人工复核：" + "；".join(w.review_reasons)
        remembered = self.memory.get("customer", customer_id, "last_willingness")
        if remembered:
            reply += f"上次会话还款意愿{remembered.get('score')}分（{remembered.get('level')}）。"
        contact = self.memory.get("customer", customer_id, "last_contact")
        if contact:
            reply += f"上次联系{contact.get('time')}，结果{contact.get('result')}。"
        promises = self.memory.get("customer", customer_id, "open_promises") or []
        if promises:
            last = promises[-1]
            reply += f"未结承诺：{last.get('date') or '近期'}还款{last.get('amount') or ''}。"
        return AnswerResult(reply=reply, willingness_score=w.score, willingness_level=w.level,
                            needs_review=w.needs_review, review_reasons=w.review_reasons)

    def _default_pack(self) -> dict:
        return self.cc.get("business_pack", self.main_cfg["default_pack"])[0]

    def _customer_report(self, a: ReportIn) -> ReportOut:
        rep = customer_report(self.conn, a.customer_id, self.policy, datetime.fromisoformat(a.now))
        sections = self._default_pack()["report_sections"]
        return ReportOut(report=rep, text=render_customer_text(rep) + "\n\n" + render_report_markdown(rep, sections))

    def _summary(self, a: SummaryIn) -> ReportOut:
        rep = summary_report(self.conn, datetime.fromisoformat(a.now), self.policy, a.month)
        return ReportOut(report=rep, text=render_summary_text(rep))

    def _eval_report(self, a: ScopedReportIn) -> ReportOut:
        rep = collection_eval_report(self.conn, datetime.fromisoformat(a.now), self.policy, a.customer_id, a.text, a.month)
        return ReportOut(report=rep, text=render_eval_text(rep))

    def _audit_report(self, a: ScopedReportIn) -> ReportOut:
        rep = compliance_audit_report(self.conn, datetime.fromisoformat(a.now), a.customer_id, a.text, a.month)
        return ReportOut(report=rep, text=render_audit_text(rep))

    def _write_scoped(self, name: str, kind: str, text: str, customer_id: str | None, now: datetime, trace: Trace) -> AnswerResult:
        out = self.registry.call(name, trace, now=now.isoformat(), customer_id=customer_id, text=text)
        self.cases.add_artifact(None, None, kind, out.report, now)
        return AnswerResult(reply=out.text)

    def _answer_report(self, text: str, customer_id: str | None, now: datetime, trace: Trace) -> AnswerResult:
        if "合规审计" in text or "审计报告" in text:
            return self._write_scoped("compliance_audit_report", "合规审计报告", text, customer_id, now, trace)
        if "评估" in text:
            return self._write_scoped("collection_eval_report", "催收评估报告", text, customer_id, now, trace)
        if customer_id is None or any(k in text for k in self.main_cfg["summary_keywords"]):
            out = self.registry.call("summary_report", trace, now=now.isoformat())
            aid = self.cases.add_artifact(None, None, "汇总报告",
                                          {"data": out.report, "markdown": render_summary_markdown(out.report)}, now)
            return AnswerResult(reply=report_reply(render_summary_paragraph(out.report), aid))
        self.lookup(customer_id)
        out = self.registry.call("customer_report", trace, customer_id=customer_id, now=now.isoformat())
        sections = self._default_pack()["report_sections"]
        aid = self.cases.add_artifact(None, None, "催收报告",
                                      {"data": out.report, "markdown": render_report_markdown(out.report, sections)}, now)
        w = out.report["还款意愿评估"]
        return AnswerResult(reply=report_reply(render_customer_text(out.report), aid),
                            willingness_score=w["评分"], willingness_level=w["等级"],
                            strategy=out.report["建议策略"], needs_review=bool(out.report["数据冲突"]),
                            review_reasons=out.report["数据冲突"])

    def _answer_compliance(self, text: str, customer_id: str | None, now: datetime, trace: Trace,
                           session_id: str | None) -> AnswerResult:
        m = _THIRD_PARTY.search(text)
        result = self.engine.check_operator_action(action="外呼", customer_id=customer_id, now=now,
                                                    contact_target=m.group(0) if m else "本人")
        write_audit(self.conn, result=result, customer_id=customer_id, scene="合规问答", content=text,
                    config_version=self.cp_version, ts=now, session_id=session_id)
        trace.add("guard", "检查点二：运营操作", "ok" if result.passed else "warn", hits=_hit_ids([result]))
        if result.hits:
            reply = "不可以。" + "；".join(f"命中 {h.rule_id}（{h.rule_type}）：{h.rule_content}，处置：{h.action}"
                                       for h in result.hits)
            if result.next_allowed:
                reply += f"。下次可联系时间：{result.next_allowed.replace('T', ' ')}"
        else:
            count = (f"，今日已联系{contact_count_on(self.conn, customer_id, now.date())}次，未超过单日上限"
                     if customer_id else "")
            reply = f"可以。当前时间 {now:%H:%M} 不在禁止外呼时段{count}。请使用规范开场并先核实本人身份。"
        return AnswerResult(reply=reply, hit_rules=_hit_ids([result]), compliance=[result])

    def prepare(self, customer_id: str, channel: str, pack_name: str, gates: list[str], pre_tools: list[dict],
                now: datetime, trace: Trace, session_id: str) -> PrepareResult:
        gate = None
        if channel == "外呼":
            gate = self.engine.check_operator_action(action="外呼", customer_id=customer_id, now=now, only=gates)
            write_audit(self.conn, result=gate, customer_id=customer_id, scene=pack_name,
                        content=f"发起外呼 {customer_id}", config_version=self.cp_version, ts=now, session_id=session_id)
            trace.add("guard", "检查点二：外呼门禁", "ok" if gate.passed else "warn", hits=_hit_ids([gate]),
                      next_allowed=gate.next_allowed)
            if not gate.passed:
                msg = "外呼已拦截：" + "；".join(
                    f"命中 {h.rule_id}（{h.rule_type}）{h.rule_content}，处置：{h.action}" for h in gate.hits)
                if gate.next_allowed:
                    msg += f"。下次可外呼时间：{gate.next_allowed.replace('T', ' ')}，已生成待办"
                return PrepareResult(blocked=msg, gate=gate, snapshot=Snapshot(customer_id=customer_id))
        snap: dict = {"customer_id": customer_id, "degraded": []}
        for item in pre_tools:
            tool = item["tool"]
            args = {"customer_id": customer_id,
                    **({"day": now.date().isoformat()} if tool == "get_contact_count_today" else {})}
            try:
                out = self.registry.call(tool, trace, **args)
            except CallError as e:
                if item["on_failure"] == "block":
                    msg = f"前置工具 {tool} 调用失败（{e.reason}），已停止发起"
                    write_event(self.conn, checkpoint="前置检查", customer_id=customer_id, scene=pack_name,
                                content=msg, ts=now, session_id=session_id)
                    return PrepareResult(blocked=msg, gate=gate, snapshot=Snapshot(**snap))
                snap["degraded"].append(tool)
                trace.add("call", f"{tool} 失败按 {item['on_failure']} 处理", "warn", reason=e.reason)
                continue
            if tool == "get_customer_profile":
                snap["profile"] = out.profile
            elif tool == "get_overdues":
                snap["overdues"] = [i.model_dump() for i in out.items]
            elif tool == "get_contact_count_today":
                snap["contact_count_today"] = out.count
            elif tool == "get_willingness":
                snap["willingness"] = out.model_dump(mode="json")
        return PrepareResult(gate=gate, snapshot=Snapshot(**snap))

    def review_speech(self, text: str, *, customer_id: str | None, scene: str, extra_rules: list,
                      rewrite_fallback: str, opening: str, now: datetime, trace: Trace,
                      session_id: str | None = None) -> SpeechReview:
        result = self.engine.check_agent_speech(text, extra_rules=extra_rules)
        write_audit(self.conn, result=result, customer_id=customer_id, scene=scene, content=text,
                    config_version=self.cp_version, ts=now, session_id=session_id)
        label = "检查点一：坐席话术审核" if scene == "话术审核" else "检查点一：人工坐席话术"
        trace.add("guard", label, "ok" if result.passed else "warn", hits=_hit_ids([result]), degraded=result.degraded)
        if result.hits:
            if any(h.rule_id in _PRIVACY_RULES for h in result.hits):
                reply = _DISCLOSURE.sub("", _violation_text(result))
                reply += "\n建议改写：请只与客户本人沟通账单，不要提及他人的姓名和欠款。"
            else:
                reply = _violation_text(result) + f"\n建议改写：{self._rewrite(text, result, extra_rules, rewrite_fallback, trace)}"
        else:
            reply = "[合规通过] 未命中合规规则。规范开场可参考：" + opening
        return SpeechReview(passed=result.passed, reply=reply, hit_rules=_hit_ids([result]), compliance=[result],
                            handoff=result.handoff, needs_review=result.degraded, review_reasons=result.notes)

    def _rewrite(self, text: str, result: CheckResult, extra_rules: list, rewrite_fallback: str, trace: Trace) -> str:
        if self.llm.available:
            try:
                out = self.llm.json("rewrite", REWRITE_SYSTEM, {
                    "原话术": text, "违规": [f"{h.rule_id} {h.rule_content}（{h.evidence}）" for h in result.hits]})
                candidate = str(out.get("reply") or "").strip()
                if candidate and not _DISCLOSURE.search(candidate) and "转告" not in candidate and self.engine.check_agent_speech(
                        candidate, extra_rules=extra_rules, use_semantic=False).passed:
                    trace.add("reply", "改写建议", source="大模型")
                    return candidate
            except LLMError:
                pass
        trace.add("reply", "改写建议", source="兜底话术")
        return rewrite_fallback
