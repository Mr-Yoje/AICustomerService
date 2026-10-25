from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from pydantic import BaseModel

from ..audit import write_audit
from ..cases import CaseStore
from ..compliance import CheckResult, ComplianceEngine
from ..desensitize import display_mask
from ..installment import PlanResult, build_plans, load_templates
from ..llm import GuardedLLM
from ..loans import LoanPriority, prioritize
from ..memory import MemoryStore
from ..registry import CallError, Registry
from ..scoring import Willingness, choose_strategy, max_overdue_days, score_customer
from ..trace import Trace
from .understanding import Understander, Understanding

ACTION_NAMES = {"installment_plan", "record_grace_request", "submit_waiver_request", "loan_priority",
                "record_promise", "reduce_contact", "request_handoff"}
_LUMP_SUM = re.compile(r"一次性(还清|结清|还完)")
_REFUSE = re.compile(r"不还|还不了|没钱|不会还")
_CONFIRM = re.compile(r"同意|就按|就这个|确认|我会还|马上还|今天还|明天还|这就还|我还")
_PLAN_ACCEPT = re.compile(r"按.{0,8}期|期.{0,6}还")
_ASK_AMOUNT = re.compile(r"可承受|还多少|每月[^。]{0,8}多少|一个月能还")


def _risk_grade(days: int) -> str:
    if days >= 180:
        return "高"
    if days >= 90:
        return "中"
    return "低"
_END_AGREED = "客户已确认还款、还款方案和还款时间"


class _Safe(dict):
    def __missing__(self, key):
        return ""


def speech_vars(pack: dict) -> dict:
    sp = pack["speech"]
    base = _Safe(agent_id=sp["agent_id"], wait_seconds=sp["wait_seconds"])
    return {**base, "identity": sp["identity"].format_map(base), "verify": sp["verify"], "purpose": sp["purpose"]}


COMBO_HANDOFF = "情绪感知+人工接管"
COMBO_PLAN = "情绪感知+分期方案协商"
_HANDOFF_CUES = ("转人工", "报警", "别再打", "再打")
_EMOTION_WORDS = ("焦虑", "压力", "失业", "害怕", "难")


def _combo_intent(u: Understanding, text: str) -> str | None:
    """投诉或强烈抵触，并且要求停联、报警或转人工时，情绪感知与转人工一起做。
    焦虑或抵触，并且话里同时提出分期时，先安抚再给分期。单纯要求转人工或单纯分期保持原意图。"""
    strong = u.customer_intent == "投诉" or u.emotion == "愤怒" or (u.emotion == "抵触" and u.emotion_intensity >= 0.6)
    if strong and any(w in text for w in _HANDOFF_CUES):
        return COMBO_HANDOFF
    wants_plan = u.customer_intent == "要求分期" or "分期" in text
    emotional = u.emotion in ("焦虑", "抵触") and (u.emotion_intensity >= 0.6 or any(w in text for w in _EMOTION_WORDS))
    if emotional and wants_plan:
        return COMBO_PLAN
    return None


def format_speech(pack: dict, key: str, **extra) -> str:
    templates = pack["speech"]["templates"]
    return (templates.get(key) or templates["default"]).format_map(_Safe({**speech_vars(pack), **extra}))


def refusal_limit(pack: dict) -> int | None:
    """业务包 exit.max_refusals。未配置或不是正整数时不因拒绝主动结束。"""
    raw = (pack.get("exit") or {}).get("max_refusals")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    n = int(raw)
    return n if n >= 1 else None


def _plan_fact_block(plan: PlanResult) -> dict:
    block = {
        "欠款总额": f"{plan.principal:.0f}元",
        "分期方案": [{"模板": p.template_id, "类型": p.template_type, "期数": p.periods,
                   "月供": f"{p.monthly_payment:.2f}元", "年化利率": f"{p.annual_rate * 100:.2f}%",
                   "手续费": f"{p.fee:.2f}元", "总成本": f"{p.total_cost:.2f}元", "说明": p.note}
                  for p in plan.plans],
        "方案口径": plan.method,
    }
    if plan.message:
        block["方案提示"] = plan.message
    return block


class Snapshot(BaseModel):
    customer_id: str
    profile: dict = {}
    overdues: list[dict] = []
    contact_count_today: int = 0
    willingness: dict | None = None
    degraded: list[str] = []


class TurnOutput(BaseModel):
    reply: str
    customer_intent: str
    system_intent: str
    emotion: str
    emotion_intensity: float
    stage: str
    actions: list[str] = []
    compliance: list[CheckResult] = []
    willingness: Willingness | None = None
    strategy: str | None = None
    plan: PlanResult | None = None
    loans: LoanPriority | None = None
    handoff: bool = False
    handoff_reason: str | None = None
    end_reason: str | None = None
    needs_review: bool = False
    review_reasons: list[str] = []
    confidence: float | None = None


class Empty(BaseModel):
    pass


class UnderstandIn(BaseModel):
    text: str
    context: dict = {}


class WillIn(BaseModel):
    intents: list[str] = []


class PlanIn(BaseModel):
    monthly_affordable: float | None = None
    requested_periods: int | None = None
    lump_sum: bool = False


class PromiseIn(BaseModel):
    amount: float | None = None
    date: str | None = None


class GraceIn(BaseModel):
    date: str | None = None
    reason: str = ""


class WaiverIn(BaseModel):
    scope: str = ""


class HandoffIn(BaseModel):
    reason: str


class ReplyIn(BaseModel):
    payload: dict
    rewrite_hint: str = ""


class ReplyOut(BaseModel):
    reply: str
    source: str = "大模型"


class AckOut(BaseModel):
    ok: bool = True
    detail: dict = {}


class TemplatesOut(BaseModel):
    templates: list[dict]


class OverduesOut(BaseModel):
    items: list[dict]


class CollectionAgent:
    def __init__(self, *, conn: sqlite3.Connection, session_id: str, case_id: str, customer_id: str, pack: dict,
                 audit_version: str, policy: dict, intents_cfg: dict, engine: ComplianceEngine, llm: GuardedLLM,
                 understander: Understander, memory: MemoryStore, cases: CaseStore, snapshot: Snapshot,
                 verified: bool, registry_cfg: dict | None = None):
        self.conn, self.session_id, self.case_id, self.customer_id = conn, session_id, case_id, customer_id
        self.pack, self.audit_version, self.policy, self.intents = pack, audit_version, policy, intents_cfg
        self.engine, self.llm, self.understander = engine, llm, understander
        self.memory, self.cases, self.snapshot = memory, cases, snapshot
        self.registry_cfg = registry_cfg
        self.allowed_templates = (pack.get("skill_params", {}).get("installment_plan") or {}).get("templates")
        first = pack["stages"][0]
        self.stage = first["next"] if verified and first.get("next") else first["id"]
        self._set("verified", verified, "会话初始化")
        self.registry = self._build_registry()

    def _mem(self, key: str, default=None):
        return self.memory.get("session", self.session_id, key, default)

    def _set(self, key: str, value, source: str) -> None:
        self.memory.set("session", self.session_id, key, value, source)

    def _append(self, key: str, item, source: str) -> None:
        self._set(key, [*self._mem(key, []), item][-20:], source)

    def _turn(self) -> int:
        return len(self.cases.messages(self.session_id)) // 2 + 1

    @classmethod
    def describe_registry(cls, pack: dict, conn, policy: dict, registry_cfg: dict | None = None) -> list[dict]:
        """不绑定客户和会话，仅用于展示某业务包子 Agent 的工具与 Skill 契约。"""
        stub = cls.__new__(cls)
        stub.pack, stub.conn, stub.customer_id, stub.policy = pack, conn, None, policy
        stub.registry_cfg = registry_cfg
        return stub._build_registry().describe()

    def _build_registry(self) -> Registry:
        r = Registry(f"collection:{self.pack['pack_id']}")
        conn, cid, policy = self.conn, self.customer_id, self.policy
        r.register("get_my_overdues", lambda a: OverduesOut(items=[i.model_dump() for i in prioritize(conn, cid, policy).items]),
                   Empty, OverduesOut, kind="tool", description="查询当前客户的全部逾期记录")
        r.register("get_my_templates", lambda a: TemplatesOut(templates=[
            t for t in load_templates(conn) if self.allowed_templates is None or t["template_id"] in self.allowed_templates]),
                   Empty, TemplatesOut, kind="tool", description="查询本业务可用的分期模板")
        r.register("record_promise", self._record_promise, PromiseIn, AckOut, kind="tool",
                   description="记录客户的还款承诺", retries=0)
        r.register("record_grace_request", self._record_grace, GraceIn, AckOut, kind="tool",
                   description="记录客户的宽限请求", retries=0)
        r.register("submit_waiver_request", self._submit_waiver, WaiverIn, AckOut, kind="tool",
                   description="提交减免申请（只登记，不承诺结果）", retries=0)
        r.register("reduce_contact", self._reduce_contact, Empty, AckOut, kind="tool",
                   description="记录客户降低联系频次的诉求", retries=0)
        r.register("request_handoff", lambda a: AckOut(detail={"reason": a.reason}), HandoffIn, AckOut, kind="tool",
                   description="向主控申请转人工")
        r.register("intent_emotion", lambda a: self.understander.customer(a.text, a.context), UnderstandIn, Understanding,
                   kind="skill", description="识别客户意图、情绪与关键信息", timeout_s=40, retries=0)
        r.register("willingness_update", lambda a: score_customer(conn, cid, policy, a.intents), WillIn, Willingness,
                   kind="skill", description="结合本次对话表态更新还款意愿评分")
        r.register("installment_plan", lambda a: build_plans(
            conn, cid, policy, allowed_templates=self.allowed_templates, monthly_affordable=a.monthly_affordable,
            requested_periods=a.requested_periods, lump_sum_requested=a.lump_sum),
                   PlanIn, PlanResult, kind="skill", description="按适用条件生成或调整分期方案")
        r.register("loan_priority", lambda a: prioritize(conn, cid, policy), Empty, LoanPriority, kind="skill",
                   description="当前客户多笔逾期的处理优先级")
        r.register("generate_reply", self._generate_reply, ReplyIn, ReplyOut, kind="skill",
                   description="按阶段目标与事实生成坐席话术", timeout_s=40, retries=0)
        r.register("dialog_summary", lambda a: ReplyOut(reply=self.cases.context(self.session_id)["summary"], source="规则"),
                   Empty, ReplyOut, kind="skill", description="对话小结")
        r.apply(getattr(self, "registry_cfg", None))
        return r

    def _record_promise(self, a: PromiseIn) -> AckOut:
        item = {"amount": a.amount, "date": a.date, "turn": self._turn()}
        self._append("promises", item, "record_promise")
        return AckOut(detail=item)

    def _record_grace(self, a: GraceIn) -> AckOut:
        item = {"date": a.date, "reason": a.reason, "turn": self._turn()}
        self._set("grace_request", item, "record_grace_request")
        return AckOut(detail=item)

    def _submit_waiver(self, a: WaiverIn) -> AckOut:
        item = {"request_id": f"WV-{self.session_id}-{len(self._mem('waivers', [])) + 1}", "scope": a.scope,
                "status": "已提交待审批"}
        self._append("waivers", item, "submit_waiver_request")
        return AckOut(detail=item)

    def _reduce_contact(self, a: Empty) -> AckOut:
        self._set("contact_preference", "降低联系频次", "reduce_contact")
        complaints = self.memory.get("customer", self.customer_id, "complaints", [])
        complaints.append({"session_id": self.session_id, "content": "要求降低联系频次"})
        self.memory.set("customer", self.customer_id, "complaints", complaints, "reduce_contact")
        return AckOut(detail={"contact_preference": "降低联系频次"})

    def _prior_note(self) -> str:
        promises = self.memory.get("customer", self.customer_id, "open_promises") or []
        if not promises:
            return ""
        last = promises[-1]
        amount = last.get("amount")
        amount_text = f"{float(amount):.0f}元" if isinstance(amount, (int, float)) else ""
        return f"上次您承诺{last.get('date') or '近期'}还款{amount_text}，我先与您核对。"

    def _prior_facts(self) -> dict:
        facts = {}
        remembered = self.memory.get("customer", self.customer_id, "last_willingness")
        if remembered:
            facts["上次还款意愿"] = f"{remembered.get('score')}分（{remembered.get('level')}）"
        contact = self.memory.get("customer", self.customer_id, "last_contact")
        if contact:
            facts["上次联系"] = f"{contact.get('time')}，结果{contact.get('result')}"
        promises = self.memory.get("customer", self.customer_id, "open_promises") or []
        if promises:
            last = promises[-1]
            facts["未结承诺"] = f"{last.get('date') or '近期'}还款{last.get('amount') or ''}"
        return facts

    def opening(self, now: datetime, trace: Trace) -> TurnOutput:
        reply = format_speech(self.pack, "opening") + self._prior_note()
        checks = [self._check(reply, now, trace, semantic=False)]
        self.cases.add_message(self.session_id, "AI坐席", reply, {"stage": self.stage}, now)
        return TurnOutput(reply=reply, customer_intent="其他", system_intent="提醒催收", emotion="平静",
                          emotion_intensity=0.0, stage=self.stage, compliance=checks)

    def handle(self, text: str, now: datetime, trace: Trace) -> TurnOutput:
        clean, issue = self.understander.preprocess(text)
        review: list[str] = []
        if issue:
            trace.add("understand", "输入预处理", "warn", issue=issue)
            review.append(f"输入异常：{issue}")
        if issue in ("空输入", "乱码"):
            reply = format_speech(self.pack, "clarify")
            checks = [self._check(reply, now, trace, semantic=False)]
            self._log_turn(text, reply, {"issue": issue}, now)
            return TurnOutput(reply=reply, customer_intent="其他",
                              system_intent=self.intents["customer_to_system"].get("其他", "提醒催收"), emotion="平静",
                              emotion_intensity=0.0, stage=self.stage, compliance=checks, needs_review=True,
                              review_reasons=review)
        ctx = self.cases.context(self.session_id)
        follow_up = any(m["role"] == "AI坐席" for m in self.cases.messages(self.session_id))
        understand_ctx = {"summary": ctx["summary"], "recent": ctx["recent"][-4:]}
        if follow_up:
            understand_ctx["nodes"] = [{"id": s["id"], "goal": s["goal"]} for s in self.pack["stages"]]
        try:
            u = self.registry.call("intent_emotion", trace, text=clean, context=understand_ctx)
        except CallError as e:
            u = self.understander.fallback_customer(clean)
            trace.add("understand", "意图识别降级", "warn", reason=e.reason)
        trace.add("understand", "意图情绪识别", intent=u.customer_intent, emotion=u.emotion,
                  intensity=u.emotion_intensity, confidence=u.confidence, method=u.method)
        combo = _combo_intent(u, clean)
        cp3 = self.engine.check_customer_input(clean)
        trace.add("guard", "检查点三：客户输入", signals=cp3.signals)
        if u.confidence < self.intents.get("low_confidence", 0.6) or u.conflict:
            review.append("意图识别置信度低或存在意图冲突，需人工复核")
        had_dialog = bool(self._mem("intents"))
        stage_before = self.stage
        self._update_memory(u, clean)
        self._turn_nodes = self._apply_nodes(u, follow_up, stage_before)
        actions = list(self.pack["intent_actions"].get(u.customer_intent, []))
        if combo == COMBO_PLAN and "installment_plan" not in actions:
            actions.append("installment_plan")
        if combo == COMBO_HANDOFF and "reduce_contact" not in actions:
            actions.append("reduce_contact")
        trace.add("decide", "阶段与动作", stage=self.stage, actions=actions)
        done: list[str] = []
        plan: PlanResult | None = None
        loans: LoanPriority | None = None
        for name in actions:
            try:
                out = self.registry.call(name, trace, **self._action_args(name, u, clean))
            except CallError as e:
                review.append(f"{name} 调用失败：{e.reason}")
                continue
            done.append(name)
            if isinstance(out, PlanResult):
                plan = out
                review += out.conflicts
                self._record_negotiation(out, clean)
            elif isinstance(out, LoanPriority):
                loans = out
                review += out.conflicts
        will, strategy = None, None
        try:
            fresh = self.snapshot.willingness if not had_dialog and self.snapshot.willingness else None
            if fresh:
                will = Willingness.model_validate(fresh)
                trace.add("call", "willingness_update", "ok", source="前置工具")
            else:
                will = self.registry.call("willingness_update", trace, intents=self._mem("intents", []))
            strategy = choose_strategy(self.policy, will.level, max_overdue_days(self.conn, self.customer_id))
            self._set("willingness", will.model_dump(mode="json"), "willingness_update")
            self._set("strategy", strategy, "策略表")
            review += will.review_reasons
        except CallError as e:
            review.append(f"意愿评分失败：{e.reason}")
        system_intent = combo or self.intents["customer_to_system"].get(u.customer_intent, "提醒催收")
        handoff_reason = self._handoff_reason(u, cp3, done)
        facts = self._facts(plan, loans, will, follow_up=follow_up)
        self._capture_agreement(u, clean, plan)
        blocked = bool(handoff_reason or combo == COMBO_HANDOFF)
        refusal_end = None if blocked else self._refusal_end_reason()
        end_reason = refusal_end or (None if blocked else self._agreement_end_reason())
        if end_reason:
            self.stage = "结束"
            reply = (format_speech(self.pack, "拒绝结束") if refusal_end else
                     format_speech(self.pack, "结束确认", plan_brief=self._mem("agreed_plan") or "",
                                   repay_date=self._mem("repay_date") or ""))
            checks = [self._check(reply, now, trace, semantic=False)]
        elif combo == COMBO_HANDOFF:
            handoff_reason = handoff_reason or "客户情绪强烈并要求停止联系或转人工"
            reply = format_speech(self.pack, "情绪感知") + "\n" + format_speech(self.pack, "handoff")
            checks = [self._check(reply, now, trace, semantic=False)]
        elif handoff_reason:
            system_intent = "人工接管"
            reply = format_speech(self.pack, "handoff")
            if u.customer_intent == "投诉":
                reply = "非常抱歉给您带来困扰。\n" + reply
            checks = [self._check(reply, now, trace, semantic=False)]
        else:
            reply, checks, forced = self._compose(clean, u, system_intent, facts, now, trace, follow_up=follow_up)
            if forced:
                handoff_reason, system_intent = forced, "人工接管"
        if end_reason is None and not handoff_reason and self._skip_amount_question() and _ASK_AMOUNT.search(reply):
            reply = self._followup_reply(facts)
            checks = [self._check(reply, now, trace, semantic=False)]
        if _ASK_AMOUNT.search(reply):
            self._set("amount_asked", True, "已询问还款金额")
        if system_intent == "多贷款管理" and loans and loans.items:
            strategy = "优先处理逾期最长的一笔"
        if any(c.degraded for c in checks):
            review.append("语义合规判定不可用，已按关键词规则判定")
        self._log_turn(text, reply, {"intent": u.customer_intent, "system_intent": system_intent, "emotion": u.emotion,
                                     "actions": done}, now)
        self._bump_pressure(reply)
        trace.add("reply", "坐席回复", system_intent=system_intent, handoff=bool(handoff_reason))
        return TurnOutput(reply=reply, customer_intent=u.customer_intent, system_intent=system_intent, emotion=u.emotion,
                          emotion_intensity=u.emotion_intensity, stage=self.stage, actions=done, compliance=checks,
                          willingness=will, strategy=strategy, plan=plan, loans=loans, handoff=bool(handoff_reason),
                          handoff_reason=handoff_reason, end_reason=end_reason, needs_review=bool(review),
                          review_reasons=list(dict.fromkeys(review)), confidence=u.confidence)

    def _update_memory(self, u: Understanding, text: str) -> None:
        intents = self._mem("intents", [])
        if u.customer_intent not in intents:
            self._set("intents", [*intents, u.customer_intent], "意图识别")
        self._append("emotions", {"turn": self._turn(), "emotion": u.emotion, "intensity": u.emotion_intensity,
                                  "text": text[:40]}, "情绪识别")
        if u.customer_intent == "拒绝还款":
            self._set("refusals", self._mem("refusals", 0) + 1, "意图识别")
        low = u.confidence < self.intents.get("low_confidence", 0.6)
        self._set("low_conf_turns", self._mem("low_conf_turns", 0) + 1 if low else 0, "意图识别")
        if u.monthly_affordable:
            self._set("monthly_affordable", u.monthly_affordable, "客户表述")
        if u.date:
            self._set("repay_date", u.date, "客户表述")
        if self.stage == self.pack["stages"][0]["id"] and re.search(r"(?<!\d)\d{4}(?!\d)", text):
            self._set("verified", True, "客户提供身份证后四位")

    def _next_stage(self, intent: str) -> str:
        target = self.pack["stage_advance"].get(intent)
        if target:
            return target
        first = self.pack["stages"][0]
        if self.stage == first["id"] and self._mem("verified") and first.get("next"):
            return first["next"]
        return self.stage

    def _action_args(self, name: str, u: Understanding, text: str) -> dict:
        if name == "installment_plan":
            return {"monthly_affordable": u.monthly_affordable or self._mem("monthly_affordable"),
                    "requested_periods": u.periods,
                    "lump_sum": u.customer_intent == "要求减免" or bool(_LUMP_SUM.search(text))}
        if name == "record_promise":
            return {"amount": u.amount, "date": u.date}
        if name == "record_grace_request":
            return {"date": u.date, "reason": text[:60]}
        if name == "submit_waiver_request":
            return {"scope": text[:60]}
        if name == "request_handoff":
            return {"reason": "客户要求转人工"}
        return {}

    def _record_negotiation(self, plan: PlanResult, text: str) -> None:
        self._append("negotiation", {
            "turn": self._turn(), "客户诉求": text[:60],
            "方案": [f"{p.template_id} {p.template_type} {p.periods}期 月供{p.monthly_payment:.2f}元 "
                     f"年化{p.annual_rate * 100:.2f}% 手续费{p.fee:.2f}元" for p in plan.plans],
            "结果": plan.message or ("已提供方案" if plan.plans else "无可用方案"),
        }, "installment_plan")
        if plan.plans:
            self._set("saved_plans", _plan_fact_block(plan), "分期方案")

    def _goal_of(self, stage_id: str) -> str:
        return next((s["goal"] for s in self.pack["stages"] if s["id"] == stage_id), "")

    def _apply_nodes(self, u: Understanding, follow_up: bool, stage_before: str) -> tuple[str, str | None] | None:
        """跟进轮采用理解模块按上下文判断的节点。没有判断时不编造下一节点。"""
        if not follow_up:
            self.stage = self._next_stage(u.customer_intent)
            return None
        if not (u.current_node or u.next_node):
            self.stage = self._next_stage(u.customer_intent)
            return self.stage, None
        current = u.current_node or stage_before
        nxt = u.next_node
        first = self.pack["stages"][0]["id"]
        if stage_before == first and not self._mem("verified"):
            current, nxt = first, None
        if nxt == current:
            nxt = None
        self.stage = nxt or current
        return current, nxt

    def _handoff_reason(self, u: Understanding, cp3: CheckResult, done: list[str]) -> str | None:
        h = self.pack["handoff"]
        if "request_handoff" in done or "要求转人工" in cp3.signals:
            return "客户要求转人工"
        emo = h["emotion"]
        if u.emotion in emo["types"] and u.emotion_intensity >= emo["min_intensity"]:
            return f"客户情绪{u.emotion}（强度{u.emotion_intensity:.2f}）超过阈值"
        if self._mem("low_conf_turns", 0) >= h["low_confidence_turns"]:
            return "识别置信度连续偏低"
        return None

    def _facts(self, plan: PlanResult | None, loans: LoanPriority | None, will: Willingness | None,
               follow_up: bool = False) -> dict:
        ods = self.snapshot.overdues
        f: dict = {"欠款总额": f"{sum(o.get('overdue_amount') or 0 for o in ods):.0f}元", "逾期笔数": len(ods)}
        if ods:
            f["最长逾期"] = f"{max(o.get('overdue_days') or 0 for o in ods)}天"
        if plan:
            f.update(_plan_fact_block(plan))
        elif follow_up and self._mem("saved_plans"):
            f.update(self._mem("saved_plans"))
        if follow_up and ods and "逾期明细" not in f:
            f["逾期明细"] = [{"编号": o.get("overdue_id"), "贷款类型": o.get("loan_type") or "未知",
                          "逾期天数": o.get("overdue_days"),
                          "欠款": f"{float(o.get('overdue_amount') or 0):.0f}元"} for o in ods]
        if loans:
            f["逾期明细"] = [{"编号": i.overdue_id, "贷款类型": i.loan_type or "未知", "逾期天数": i.overdue_days,
                          "欠款": f"{i.overdue_amount:.0f}元"} for i in loans.items]
            f["优先建议"] = loans.suggestion
            f["欠款总额"] = f"{loans.total_amount:.0f}元"
            f["风险等级"] = _risk_grade(max(i.overdue_days for i in loans.items))
        if self._mem("waivers"):
            f["减免申请"] = "已提交，结果以审批为准"
        if self._mem("grace_request"):
            f["宽限请求"] = "已记录"
        if self._mem("promises"):
            f["已确认承诺"] = [{"金额": f"{p['amount']:.0f}元" if p.get("amount") else None, "日期": p.get("date")}
                          for p in self._mem("promises")]
        if self.snapshot.contact_count_today:
            f["今日已联系"] = f"{self.snapshot.contact_count_today}次"
        f.update(self._prior_facts())
        if will:
            f["还款意愿"] = f"{will.score}分（{will.level}，置信度{will.confidence}）"
            f["还款意愿等级"] = will.level
        if self._mem("monthly_affordable"):
            f["可承受金额"] = f"{float(self._mem('monthly_affordable')):.0f}元"
        if self._mem("amount_asked"):
            f["已问过金额"] = "是"
        if self._mem("repay_date"):
            f["还款时间"] = self._mem("repay_date")
        if self._mem("offered_plan"):
            f["已给方案"] = self._mem("offered_plan")
        return f

    def _skip_amount_question(self) -> bool:
        return bool(self._mem("monthly_affordable") or self._mem("amount_asked"))

    def _capture_agreement(self, u: Understanding, text: str, plan: PlanResult | None) -> None:
        refused = u.customer_intent == "拒绝还款" or bool(_REFUSE.search(text))
        confirmed = (not refused) and (
            u.customer_intent == "承诺还款" or bool(_CONFIRM.search(text))
            or bool(u.date and (u.periods or u.amount or self._mem("offered_plan")) and _PLAN_ACCEPT.search(text))
        )
        if confirmed:
            self._set("repay_confirmed", True, "客户确认还款")
        plan_text = None
        if plan and plan.plans:
            chosen = plan.plans[0]
            plan_text = f"{chosen.template_type}{chosen.periods}期，月供{chosen.monthly_payment:.2f}元"
            self._set("offered_plan", plan_text, "分期方案")
        elif u.periods:
            plan_text = f"{u.periods}期"
        if self._mem("repay_confirmed") and (plan_text or self._mem("offered_plan")):
            self._set("agreed_plan", plan_text or self._mem("offered_plan"), "客户确认方案")

    def _refusal_end_reason(self) -> str | None:
        limit = refusal_limit(self.pack)
        count = self._mem("refusals", 0)
        if limit is not None and count >= limit:
            return f"客户累计明确拒绝{count}次"
        return None

    def _agreement_end_reason(self) -> str | None:
        if self._mem("repay_confirmed") and self._mem("agreed_plan") and self._mem("repay_date"):
            return _END_AGREED
        return None

    def _followup_reply(self, facts: dict) -> str:
        plans = facts.get("分期方案") or []
        plan_brief = "；".join(
            f"{p['类型']}{p['期数']}期，月供{p['月供']}" for p in plans
        ) or facts.get("已给方案") or self._mem("offered_plan") or self._mem("agreed_plan") or "已有分期方案"
        date = self._mem("repay_date")
        if date:
            return format_speech(self.pack, "确认方案", plan_brief=plan_brief, repay_date=date)
        return format_speech(self.pack, "询问还款时间", plan_brief=plan_brief)

    def _compose(self, text: str, u: Understanding, system_intent: str, facts: dict, now: datetime,
                 trace: Trace, follow_up: bool = False) -> tuple[str, list[CheckResult], str | None]:
        max_rw = self.engine.cfg["checkpoints"]["agent_speech"].get("max_rewrites", 2)
        checks: list[CheckResult] = []
        hint = ""
        for _ in range(max_rw + 1):
            draft = self._draft(text, u, system_intent, facts, hint, trace, follow_up=follow_up)
            r = self._check(draft, now, trace)
            checks.append(r)
            if r.passed:
                return draft, checks, None
            if r.handoff:
                reply = format_speech(self.pack, "handoff")
                checks.append(self._check(reply, now, trace, semantic=False))
                return reply, checks, "坐席话术命中严重级规则：" + "、".join(h.rule_id for h in r.hits)
            keep = ("不得保留第三方姓名、称谓和欠款金额。"
                    if any(h.rule_id in {"R006", "R009"} for h in r.hits) else "保留业务信息。")
            hint = ("上一版话术违反：" + "；".join(f"{h.rule_id} {h.rule_content}（{h.evidence}）" for h in r.hits)
                    + "。请改写为合规表述，" + keep)
            trace.add("guard", "话术改写", "retry", hint=hint)
            if not self.llm.available:
                break
        fallback = self.pack["speech"]["rewrite_fallback"]
        r = self._check(fallback, now, trace, semantic=False)
        checks.append(r)
        if r.passed:
            return fallback, checks, None
        reply = format_speech(self.pack, "handoff")
        checks.append(self._check(reply, now, trace, semantic=False))
        return reply, checks, "话术多次改写仍不合规"

    def _stage_goal(self) -> str:
        return self._goal_of(self.stage)

    def _draft(self, text: str, u: Understanding, system_intent: str, facts: dict, hint: str, trace: Trace,
               follow_up: bool = False) -> str:
        ctx = self.cases.context(self.session_id)
        payload = {
            "system_intent": system_intent, "customer_intent": u.customer_intent,
            "stage_goal": self._stage_goal(),
            "客户说": text, "客户情绪": f"{u.emotion}（{u.emotion_intensity:.2f}）", "事实": facts,
            "对话摘要": ctx["summary"], "最近对话": ctx["recent"],
        }
        if follow_up and self._turn_nodes:
            current, nxt = self._turn_nodes
            payload["跟进"] = True
            payload["当前节点"] = current
            payload["当前目标"] = self._goal_of(current)
            if nxt:
                payload["下一节点"] = nxt
                payload["下一节点目标"] = self._goal_of(nxt)
        try:
            return self.registry.call("generate_reply", trace, payload=payload, rewrite_hint=hint).reply
        except CallError as e:
            trace.add("reply", "话术生成降级", "warn", reason=e.reason)
            body = self._template_reply(u.customer_intent, system_intent, facts)
            if "超时" in e.reason:
                return f"话术生成超时，已改为模板说明。{body}"
            return body

    def _reply_system(self, stage_goal: str, follow_up: bool = False) -> str:
        sp = self.pack["speech"]
        if follow_up:
            guide = (
                "这是跟进轮。先直接回答客户最后一句。客户若是在答应或继续上一句的提议，就完成那件事。\n"
                "最近对话里已经说过的句子不要再说。数字只用“事实”里的数据。\n"
                "「当前节点」和「下一节点」已根据上下文判断，写在输入里。不要改写节点名称。\n"
                "若输入有「下一节点」，回答之后落实「下一节点目标」，推进到该节点。\n"
                "若输入没有「下一节点」，回答之后落实「当前目标」，停在当前节点。\n"
            )
        else:
            guide = (
                f"当前阶段目标：{stage_goal}。\n"
                "按系统意图组织话术，措辞可以变化，步骤和语义按下面的逻辑，数字只用“事实”里的：\n"
            )
        script = "" if follow_up else (
            "- 提醒催收：先报工号，请客户提供身份证后四位核验身份，核验后再说明逾期并询问还款安排。\n"
            "- 还款意愿评估：先共情，询问预计哪天发工资，说明宽限申请已记录，再用事实中的分期方案推荐，并说明逾期天数继续增加会影响信用，最后问是否方便了解。\n"
            "- 分期方案协商：还没有可承受金额、也没有「已问过金额」时，先说明可以分期，再问一次每月可承受金额，然后推荐事实中的方案，并说明会生成协商记录。已有可承受金额或已问过金额时，不要再问还多少或每月可承受金额，直接推荐事实中的方案；还没有还款时间时只问哪一天还。\n"
            "- 情绪感知：先道歉并承认频繁来电造成不适，记录反馈，说明会控制联系频次，再约一个方便的沟通时间。这一句不要转人工。\n"
            "- 合规检测：感谢还款意愿，明确不能承诺减免全部利息或未经审批的减免，说明可先提交申请、以审批为准，并推荐事实中的分期方案。\n"
            "- 人工接管：马上转接人工，说明对话记录和账户信息已同步、无需重复，并给出预计等待秒数。\n"
            "- 多贷款管理：先给出笔数、合计金额和风险等级，再按事实逐笔列出贷款类型、逾期天数和欠款金额，建议优先处理逾期天数最长的一笔，询问是否同意。\n"
            "系统意图含「+」时，按加号两侧的逻辑依次说完。\n"
        )
        return (
            f"你是{self.pack['name']}的 AI 坐席，工号{sp['agent_id']}，正在和客户本人通话。语气：{sp['tone']}。\n"
            + guide + script +
            "硬性要求：1. 金额、期数、利率、日期、笔数只能引用“事实”里的数据，不得编造；"
            "2. 不得承诺减免、免息、延期的审批结果，只能说提交申请、以审批为准；"
            "3. 不得威胁、辱骂、冒充公检法、提及联系客户亲友，不得说不还款没有后果；"
            f"4. 禁用词：{'、'.join(sp.get('forbidden_words', []))}；"
            "5. 尖括号占位符（如 <AMOUNT_1>）原样保留；6. 不超过 220 字，先回应客户，再按上面的逻辑说完；"
            "7. 若有“改写要求”，必须按要求修正；"
            "8. 事实里已有可承受金额或已问过金额时，禁止再问还多少、每月可承受或每月多少。\n"
            '输出 json，例如 {"reply": "……"}'
        )

    def _generate_reply(self, a: ReplyIn) -> ReplyOut:
        p = dict(a.payload)
        follow_up = bool(p.pop("跟进", False))
        if not self.llm.available:
            return ReplyOut(reply=self._template_reply(p["customer_intent"], p["system_intent"], p["事实"]), source="模板")
        user = {**p, "改写要求": a.rewrite_hint} if a.rewrite_hint else p
        out = self.llm.json("reply", self._reply_system(p.get("stage_goal", ""), follow_up=follow_up), user,
                            names=[self.snapshot.profile.get("customer_name") or ""])
        reply = str(out.get("reply") or "").strip()
        if not reply:
            raise ValueError("模型返回空话术")
        return ReplyOut(reply=reply[:300])

    def _template_reply(self, customer_intent: str, system_intent: str, facts: dict) -> str:
        plans = facts.get("分期方案") or []
        plan_brief = "；".join(f"{p['类型']}{p['期数']}期，月供{p['月供']}，年化利率{p['年化利率']}，手续费{p['手续费']}"
                               for p in plans) or facts.get("方案提示") or "可以根据您的情况再为您测算"
        details = facts.get("逾期明细") or []
        loan_list = "；".join(f"{n}. {x['贷款类型']}逾期{x['逾期天数']}天，欠款{x['欠款']}（{x['编号']}）"
                              for n, x in enumerate(details, 1))
        promises = facts.get("已确认承诺") or []
        last = promises[-1] if promises else {}
        promise_brief = "，".join(v for v in (last.get("日期"), last.get("金额")) if v) or "按约定时间还款"
        templates = self.pack["speech"]["templates"]
        if system_intent == COMBO_PLAN:
            return format_speech(self.pack, "情绪感知") + "\n" + format_speech(
                self.pack, "分期方案协商", plan_brief=plan_brief, principal=facts.get("欠款总额", ""))
        key = next((k for k in (customer_intent, system_intent) if k in templates), "default")
        return format_speech(self.pack, key, plan_brief=plan_brief, principal=facts.get("欠款总额", ""),
                             loan_count=len(details), loan_list=loan_list, suggestion=facts.get("优先建议", ""),
                             promise_brief=promise_brief, total=facts.get("欠款总额", ""),
                             risk=facts.get("风险等级", ""))

    def _check(self, text: str, now: datetime, trace: Trace, semantic: bool = True) -> CheckResult:
        r = self.engine.check_agent_speech(text, extra_rules=self.pack.get("extra_rules", []),
                                           pressure_so_far=self._mem("pressure", 0), use_semantic=semantic)
        write_audit(self.conn, result=r, customer_id=self.customer_id, scene=self.pack["name"], content=text,
                    config_version=self.audit_version, ts=now, session_id=self.session_id)
        trace.add("guard", "检查点一：坐席话术", "ok" if r.passed else "warn",
                  hits=[f"{h.rule_id}:{h.evidence}" for h in r.hits], degraded=r.degraded)
        return r

    def _bump_pressure(self, reply: str) -> None:
        n = sum(len(re.findall(p, reply)) for det in self.engine.cfg["detectors"].values()
                for p in det.get("pressure_patterns", []))
        if n:
            self._set("pressure", self._mem("pressure", 0) + n, "施压计数")

    def _log_turn(self, text: str, reply: str, meta: dict, now: datetime) -> None:
        self.cases.add_message(self.session_id, "客户", text, meta, now)
        self.cases.add_message(self.session_id, "AI坐席", reply, {"stage": self.stage}, now)

    def handoff_package(self, reason: str, now: datetime) -> dict:
        prof = self.snapshot.profile
        hits = [dict(r) for r in self.conn.execute(
            "SELECT rule_id, rule_type, level, action, ts FROM audit_log WHERE session_id=? AND rule_id IS NOT NULL "
            "ORDER BY rowid", (self.session_id,))]
        return {
            "转接原因": reason, "转接时间": now.isoformat(timespec="seconds"), "会话编号": self.session_id,
            "业务包": self.pack["name"],
            "客户档案快照": {"客户编号": self.customer_id, "姓名": display_mask(prof.get("customer_name") or ""),
                       "职业": prof.get("occupation"), "月收入": prof.get("monthly_income"),
                       "信用评分": prof.get("credit_score"), "客户等级": prof.get("customer_level"),
                       "逾期": self.snapshot.overdues, "前置工具降级": self.snapshot.degraded},
            "对话全文": [{"轮次": m["turn"], "角色": m["role"], "内容": display_mask(m["text"])}
                     for m in self.cases.messages(self.session_id)],
            "情绪轨迹": self._mem("emotions", []), "还款意愿": self._mem("willingness"),
            "协商记录": self._mem("negotiation", []), "已确认承诺": self._mem("promises", []),
            "减免申请": self._mem("waivers", []), "宽限请求": self._mem("grace_request"), "命中规则": hits,
            "建议下一步": self._mem("strategy") or "由坐席确认还款安排",
        }
