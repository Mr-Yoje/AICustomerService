from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from .. import db, importer, settings
from ..audit import write_event
from ..cases import CaseStore
from ..clock import parse_time_hint
from ..compliance import CheckResult, ComplianceEngine
from ..config_center import ConfigCenter, ConfigError, registry_overrides, validate_pack
from ..desensitize import display_mask
from ..llm import GuardedLLM, bind_session, build_llm, set_llm_session
from ..memory import MemoryStore
from ..postcall import STAGES, PostCallRunner
from ..registry import CallError, Registry
from ..trace import Trace, TraceEvent
from .collection import ACTION_NAMES, CollectionAgent, TurnOutput, format_speech
from .ops import OpsAgent
from .understanding import SpeakerResult, Understander

MODE_OPERATOR, MODE_COLLECTION, MODE_HANDOFF = "运营模式", "催收对话模式", "人工接管"
ROLE_MAP = {"customer": "customer", "客户": "customer", "agent": "agent", "坐席": "agent", "AI坐席": "agent",
            "人工坐席": "agent", "operator": "operator", "运营": "operator"}
HELP_TEXT = ("我可以帮您：查询客户画像或欠款、判断当前能否外呼、生成单客户或本月汇总催收报告、对客户发起或结束催收。"
             "请带上客户编号，例如「查询C0001的欠款」。")
CLARIFY_SPEAKER = "请问这句话是客户说的、坐席说的，还是您（运营）的指令？"


class TurnUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0


class MainTurn(BaseModel):
    session_id: str | None = None
    mode: str = MODE_OPERATOR
    speaker: str
    speaker_method: str = ""
    system_intent: str
    reply: str
    usage: TurnUsage | None = None
    customer_id: str | None = None
    hit_rules: list[str] = []
    compliance: list[CheckResult] = []
    willingness_score: int | None = None
    willingness_level: str | None = None
    strategy: str | None = None
    handoff: bool = False
    handoff_package_id: str | None = None
    end_reason: str | None = None
    needs_review: bool = False
    review_reasons: list[str] = []
    key_points: list[str] = []
    trace_id: str = ""
    trace: list[dict] = []


@dataclass
class Started:
    session_id: str
    blocked: str | None = None
    gate: CheckResult | None = None


def _ids(results: list[CheckResult]) -> list[str]:
    return list(dict.fromkeys(h.rule_id for r in results for h in r.hits))


class MainAgent:
    def __init__(self, conn: sqlite3.Connection, *, db_path: Path | str, config_dir: Path | str, llm_client=None,
                 offline: bool = False, sync_post: bool = False):
        self.conn, self.db_path, self.config_dir, self.sync_post = conn, db_path, Path(config_dir), sync_post
        self.cc = ConfigCenter(conn, self.config_dir)
        client = None if offline else (llm_client or build_llm(self.cc.get("llm")[0]))
        self.llm = GuardedLLM(client, conn)
        self.cases, self.memory = CaseStore(conn), MemoryStore(conn)
        self.post = PostCallRunner(db_path, self.config_dir)
        self.agents: dict[str, CollectionAgent] = {}
        self.ops = None
        self.reload()
        self.ops = OpsAgent(
            conn, cc=self.cc, llm=self.llm, engine=self.engine, policy=self.policy, intents=self.intents,
            main_cfg=self.main_cfg, cp_version=self.cp_version, cases=self.cases, memory=self.memory)
        self.registry = Registry("main")

    @classmethod
    def from_settings(cls, **kw) -> "MainAgent":
        conn = db.connect(settings.DB_PATH)
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dim_customer'").fetchone() is None:
            importer.import_all(conn, settings.DATA_DIR)
        db.init_runtime(conn)
        return cls(conn, db_path=settings.DB_PATH, config_dir=settings.CONFIG_DIR, **kw)

    def reload(self) -> None:
        self.policy = self.cc.get("policy")[0]
        self.intents = self.cc.get("intents")[0]
        self.main_cfg = self.cc.get("main_agent")[0]
        cp, self.cp_version = self.cc.get("checkpoints")
        self.engine = ComplianceEngine(self.conn, cp, self.cp_version, self.llm)
        self.understander = Understander(self.llm, self.intents)
        ops = getattr(self, "ops", None)
        if ops is not None:
            ops.engine, ops.policy, ops.intents = self.engine, self.policy, self.intents
            ops.main_cfg, ops.cp_version, ops.llm = self.main_cfg, self.cp_version, self.llm

    def _latest_round(self, session_id: str | None) -> list[dict] | None:
        if not session_id or self.cases.get_session(session_id) is None:
            return None
        msgs = self.cases.messages(session_id)[-2:]
        if not msgs:
            return None
        return [{"role": m["role"], "text": m["text"][:200]} for m in msgs]

    def _default_pack(self) -> dict:
        return self.cc.get("business_pack", self.main_cfg["default_pack"])[0]

    def registry_items(self) -> dict:
        try:
            return registry_overrides(self.cc.get("registry")[0])
        except ConfigError:
            return {}

    def validate(self, pack: dict) -> list[str]:
        return validate_pack(pack, main_tools=set(self.ops.registry.names("tool")), collection_actions=ACTION_NAMES,
                             rule_ids=set(self.engine.rules), post_tasks=set(STAGES))

    def _finish(self, trace: Trace, **kw) -> MainTurn:
        return MainTurn(**kw, trace_id=trace.trace_id, trace=trace.to_list())

    def handle(self, text: str | None, *, session_id: str | None = None, role: str | None = None,
               scene_type: str | None = None, customer_id: str | None = None, now: datetime | None = None,
               interactive: bool = True, sink: Callable[[TraceEvent], None] | None = None) -> MainTurn:
        with bind_session(session_id):
            return self._handle(text, session_id=session_id, role=role, scene_type=scene_type,
                                customer_id=customer_id, now=now, interactive=interactive, sink=sink)

    def _handle(self, text: str | None, *, session_id: str | None = None, role: str | None = None,
                scene_type: str | None = None, customer_id: str | None = None, now: datetime | None = None,
                interactive: bool = True, sink: Callable[[TraceEvent], None] | None = None) -> MainTurn:
        self.reload()
        trace = Trace(sink)
        base = now or datetime.now()
        hinted = parse_time_hint(text or "", base)
        now = hinted or base
        if hinted:
            trace.add("understand", "题面时间", time=now.isoformat(timespec="minutes"))
        role = ROLE_MAP.get(role or "")
        sess = self.cases.get_session(session_id) if session_id else None
        if sess and not sess["ended_at"] and sess["mode"] == MODE_HANDOFF:
            return self._handoff_turn(session_id, text or "", role, now, trace)
        clean, issue = self.understander.preprocess(text)
        if issue in ("空输入", "乱码"):
            trace.add("understand", "输入预处理", "warn", issue=issue)
            return self._finish(trace, session_id=session_id, speaker="unknown", system_intent="提醒催收",
                                reply="没有识别到有效内容，请重新输入。", customer_id=customer_id, needs_review=True,
                                review_reasons=[f"输入异常：{issue}"])
        judged = self.understander.speaker(clean, scene_type, recent=self._latest_round(session_id))
        in_collection = bool(sess and not sess["ended_at"] and sess["mode"] == MODE_COLLECTION and session_id in self.agents)
        if not judged.needs_clarify:
            sp = judged
        elif in_collection:
            sp = SpeakerResult(speaker="customer", confidence=judged.confidence, method="会话模式")
        elif role:
            sp = SpeakerResult(speaker=role, confidence=1.0, method="显式角色")
        else:
            sp = judged
        trace.add("route", "说话人判定", speaker=sp.speaker, method=sp.method, confidence=sp.confidence)
        if sp.needs_clarify and interactive and not customer_id and not in_collection:
            return self._finish(trace, session_id=session_id, speaker="unknown", speaker_method=sp.method,
                                system_intent="提醒催收", reply=CLARIFY_SPEAKER, needs_review=True,
                                review_reasons=["说话人无法判定"])
        if sp.speaker == "operator":
            return self._operator_turn(clean, customer_id, session_id, now, trace, sp)
        if sp.speaker == "agent":
            return self._audit_turn(clean, customer_id, now, trace, sp)
        if in_collection:
            return self._customer_turn(session_id, text or "", now, trace, sp.method)
        cid = customer_id or (sess["customer_id"] if sess else None)
        if not cid:
            return self._finish(trace, speaker="customer", speaker_method=sp.method, system_intent="提醒催收",
                                reply="请先提供客户编号（如 C0001），再开始对话。", needs_review=True,
                                review_reasons=["缺少客户编号"])
        started = self.start_collection(cid, now, trace, channel="客户进线")
        if started.blocked:
            return self._finish(trace, session_id=started.session_id, speaker="customer", speaker_method=sp.method,
                                system_intent="提醒催收", reply=started.blocked, customer_id=cid, needs_review=True,
                                review_reasons=[started.blocked])
        return self._customer_turn(started.session_id, text or "", now, trace, sp.method)

    def match_pack(self, customer_id: str, trace: Trace) -> tuple[dict, str]:
        return self._match_loan_type(self.ops.lookup(customer_id).loan_type, trace)

    def _match_loan_type(self, loan_type: str | None, trace: Trace) -> tuple[dict, str]:
        chosen = self.main_cfg["default_pack"]
        for pid in self.cc.pack_ids():
            if loan_type and loan_type in self.cc.get("business_pack", pid)[0]["match"]["loan_types"]:
                chosen = pid
                break
        pack, version = self.cc.get("business_pack", chosen)
        trace.add("decide", "匹配业务包", loan_type=loan_type, pack=chosen, version=version)
        return pack, version

    def start_collection(self, customer_id: str, now: datetime, trace: Trace, channel: str = "外呼") -> Started:
        looked = self.ops.lookup(customer_id)
        pack, pv = self._match_loan_type(looked.loan_type, trace)
        case_id = self.cases.create_case(customer_id, pack["pack_id"], now)
        sid = self.cases.open_session(case_id, customer_id, MODE_OPERATOR, pack["pack_id"], f"{pv}/{self.cp_version}",
                                      channel, now)
        set_llm_session(sid)
        self.cases.set_case_status(case_id, "前置检查", now)
        errors = self.validate(pack)
        if errors:
            msg = "业务包配置有误，无法发起：" + "；".join(errors)
            self.end_collection(sid, now, msg)
            return Started(sid, msg)
        prepared = self.ops.prepare(customer_id, channel, pack["name"], pack["gates"], pack["pre_tools"], now, trace, sid)
        if prepared.blocked:
            if prepared.gate and not prepared.gate.passed:
                self.memory.set("session", sid, "gate",
                                {"rule_ids": _ids([prepared.gate]), "next_allowed": prepared.gate.next_allowed}, "外呼门禁")
                self.end_collection(sid, now, "外呼门禁拦截")
            else:
                self.end_collection(sid, now, prepared.blocked)
            return Started(sid, prepared.blocked, prepared.gate)
        self.agents[sid] = CollectionAgent(
            conn=self.conn, session_id=sid, case_id=case_id, customer_id=customer_id, pack=pack,
            audit_version=f"{pv}/{self.cp_version}", policy=self.policy, intents_cfg=self.intents, engine=self.engine,
            llm=self.llm, understander=self.understander, memory=self.memory, cases=self.cases,
            snapshot=prepared.snapshot, verified=channel == "客户进线", registry_cfg=self.registry_items())
        self.cases.update_session(sid, mode=MODE_COLLECTION)
        self.cases.set_case_status(case_id, "对话中", now)
        trace.add("route", "切换模式", mode=MODE_COLLECTION, pack=pack["pack_id"], session_id=sid)
        return Started(sid)

    def end_collection(self, session_id: str, now: datetime, reason: str, sync_post: bool | None = None) -> None:
        s = self.cases.get_session(session_id)
        if s is None or s["ended_at"]:
            return
        self.cases.update_session(session_id, mode=MODE_OPERATOR, ended_at=now.isoformat(timespec="seconds"),
                                  end_reason=reason)
        self.agents.pop(session_id, None)
        self.post.enqueue(session_id, sync=self.sync_post if sync_post is None else sync_post)

    def finish_handoff(self, session_id: str, conclusion: str, now: datetime) -> None:
        self.cases.add_message(session_id, "人工坐席", f"[处理结论] {conclusion}", {"conclusion": True}, now)
        self.memory.set("session", session_id, "handoff_conclusion", conclusion, "人工坐席")
        self.end_collection(session_id, now, "人工处理完成")

    def _key_points(self, out: TurnOutput) -> list[str]:
        pts = [f"客户意图：{out.customer_intent}", f"情绪：{out.emotion}（强度{out.emotion_intensity:.2f}）"]
        if out.plan and out.plan.plans:
            p = out.plan.plans[0]
            pts.append(f"推荐方案：{p.template_id} {p.template_type} {p.periods}期，月供{p.monthly_payment:.2f}元")
        if out.plan and out.plan.message:
            pts.append(out.plan.message)
        if out.loans and out.loans.items:
            pts.append(out.loans.suggestion)
        if out.confidence is not None:
            pts.append(f"识别置信度：{out.confidence}")
        if out.willingness:
            pts.append(f"还款意愿：{out.willingness.score}分（{out.willingness.level}，置信度{out.willingness.confidence}）")
        if out.strategy:
            pts.append(f"建议策略：{out.strategy}")
        if out.handoff:
            pts.append(f"转人工：{out.handoff_reason}")
        if out.end_reason:
            pts.append(f"主动结束：{out.end_reason}")
        return pts

    def _customer_turn(self, sid: str, text: str, now: datetime, trace: Trace, method: str) -> MainTurn:
        agent = self.agents[sid]
        out = agent.handle(text, now, trace)
        pkg = self._handoff(sid, out.handoff_reason or "", now, trace) if out.handoff else None
        mode = MODE_HANDOFF if out.handoff else MODE_COLLECTION
        if out.end_reason:
            self.end_collection(sid, now, out.end_reason)
            trace.add("route", "切换模式", mode=MODE_OPERATOR, reason=out.end_reason, session_id=sid)
            mode = MODE_OPERATOR
        if out.needs_review:
            self.cases.add_todo(agent.case_id, sid, "人工复核",
                                "；".join(out.review_reasons) or "本轮需要人工复核", None, now,
                                todo_id=f"TD-{sid}-review")
        return self._finish(
            trace, session_id=sid, mode=mode, speaker="customer",
            speaker_method=method, system_intent=out.system_intent, reply=out.reply, customer_id=agent.customer_id,
            hit_rules=_ids(out.compliance), compliance=out.compliance,
            willingness_score=out.willingness.score if out.willingness else None,
            willingness_level=out.willingness.level if out.willingness else None, strategy=out.strategy,
            handoff=out.handoff, handoff_package_id=pkg, end_reason=out.end_reason, needs_review=out.needs_review,
            review_reasons=out.review_reasons, key_points=self._key_points(out))

    def _handoff(self, sid: str, reason: str, now: datetime, trace: Trace) -> str:
        agent = self.agents[sid]
        s = self.cases.get_session(sid)
        aid = self.cases.add_artifact(s["case_id"], sid, "移交包", agent.handoff_package(reason, now), now)
        self.cases.update_session(sid, mode=MODE_HANDOFF)
        self.cases.set_case_status(s["case_id"], "人工接管中", now)
        write_event(self.conn, checkpoint="人工接管", customer_id=s["customer_id"], scene=agent.pack["name"],
                    content=reason, ts=now, session_id=sid)
        trace.add("route", "转人工", reason=reason, package=aid)
        return aid

    def _handoff_turn(self, sid: str, text: str, role: str | None, now: datetime, trace: Trace) -> MainTurn:
        s = self.cases.get_session(sid)
        agent = self.agents.get(sid)
        pack = agent.pack if agent else self.cc.get("business_pack", s["pack_id"])[0]
        base = dict(session_id=sid, mode=MODE_HANDOFF, speaker_method="会话模式", customer_id=s["customer_id"])
        if role == "customer":
            self.cases.add_message(sid, "客户", text, {}, now)
            return self._finish(trace, **base, speaker="customer", system_intent="人工接管",
                                reply="已记录客户发言，等待人工坐席回复。")
        reviewed = self.ops.review_speech(
            text, customer_id=s["customer_id"], scene="人工坐席", extra_rules=pack.get("extra_rules", []),
            rewrite_fallback=pack["speech"]["rewrite_fallback"], opening=format_speech(pack, "opening"),
            now=now, trace=trace, session_id=sid)
        if reviewed.passed:
            self.cases.add_message(sid, "人工坐席", text, {}, now)
            reply = text
        else:
            reply = reviewed.reply + "（本句未发送给客户）"
        return self._finish(trace, **base, speaker="agent", system_intent="人工接管" if reviewed.passed else "合规检测",
                            reply=reply, hit_rules=reviewed.hit_rules, compliance=reviewed.compliance,
                            needs_review=reviewed.needs_review, review_reasons=reviewed.review_reasons)

    def _audit_turn(self, text: str, customer_id: str | None, now: datetime, trace: Trace, sp: SpeakerResult) -> MainTurn:
        pack = self.match_pack(customer_id, trace)[0] if customer_id else self._default_pack()
        reviewed = self.ops.review_speech(
            text, customer_id=customer_id, scene="话术审核", extra_rules=pack.get("extra_rules", []),
            rewrite_fallback=pack["speech"]["rewrite_fallback"], opening=format_speech(pack, "opening"),
            now=now, trace=trace)
        if any(h.level == "严重" for r in reviewed.compliance for h in r.hits):
            self.cases.add_todo(None, None, "主管复核",
                                f"坐席话术命中严重规则 {'、'.join(reviewed.hit_rules)}：{display_mask(text)[:60]}", None, now)
        cfg = self.intents["agent_speech_system"]
        return self._finish(trace, speaker="agent", speaker_method=sp.method,
                            system_intent=cfg["hit"] if reviewed.hit_rules else cfg["pass"], reply=reviewed.reply,
                            customer_id=customer_id, hit_rules=reviewed.hit_rules, compliance=reviewed.compliance,
                            handoff=reviewed.handoff, needs_review=reviewed.needs_review,
                            review_reasons=reviewed.review_reasons)

    def _operator_turn(self, text: str, customer_id: str | None, session_id: str | None, now: datetime, trace: Trace,
                       sp: SpeakerResult) -> MainTurn:
        oi = self.understander.operator(text)
        cid = oi.customer_id or customer_id
        trace.add("understand", "运营意图", intent=oi.intent, customer_id=cid, method=oi.method, confidence=oi.confidence)
        base = dict(session_id=session_id, speaker="operator", speaker_method=sp.method, customer_id=cid,
                    system_intent=self.intents["operator_to_system"].get(oi.intent, "提醒催收"))
        handlers = {"合规问答": self._op_query("合规问答"), "生成报告": self._op_query("生成报告"),
                    "查询欠款": self._op_query("查询欠款"), "查询画像": self._op_query("查询画像"),
                    "发起催收": self._op_start, "结束催收": self._op_end,
                    "失联修复": self._op_query("失联修复")}
        handler = handlers.get(oi.intent)
        if handler is None:
            return self._finish(trace, **base, reply=HELP_TEXT)
        if cid is None and oi.intent not in ("生成报告", "结束催收", "合规问答"):
            return self._finish(trace, **base, reply="请提供客户编号（如 C0001）。", needs_review=True,
                                review_reasons=["缺少客户编号"])
        try:
            return self._finish(trace, **{**base, **handler(text, cid, session_id, now, trace)})
        except (CallError, ValueError) as e:
            return self._finish(trace, **base, reply=f"处理失败：{e}，请稍后重试。", needs_review=True,
                                review_reasons=[str(e)])

    def _op_query(self, intent: str):
        def run(text, cid, session_id, now, trace) -> dict:
            out = self.ops.answer(intent, text, cid, now, trace, session_id)
            return {"reply": out.reply, "hit_rules": out.hit_rules, "compliance": out.compliance,
                    "willingness_score": out.willingness_score, "willingness_level": out.willingness_level,
                    "strategy": out.strategy, "needs_review": out.needs_review, "review_reasons": out.review_reasons}
        return run

    def _op_start(self, text, cid, session_id, now, trace) -> dict:
        started = self.start_collection(cid, now, trace, channel="外呼")
        if started.blocked:
            gate = [started.gate] if started.gate else []
            return {"reply": started.blocked, "session_id": started.session_id, "hit_rules": _ids(gate),
                    "compliance": gate}
        opening = self.agents[started.session_id].opening(now, trace)
        return {"reply": opening.reply, "session_id": started.session_id, "mode": MODE_COLLECTION,
                "compliance": opening.compliance}

    def _op_end(self, text, cid, session_id, now, trace) -> dict:
        sid = session_id
        s = self.cases.get_session(sid) if sid else None
        if (s is None or s["ended_at"]) and cid:
            row = self.conn.execute("SELECT session_id FROM sessions WHERE customer_id=? AND ended_at IS NULL "
                                    "ORDER BY started_at DESC LIMIT 1", (cid,)).fetchone()
            sid = row["session_id"] if row else None
            s = self.cases.get_session(sid) if sid else None
        if s is None or s["ended_at"]:
            return {"reply": "没有进行中的催收会话。"}
        self.end_collection(sid, now, "运营结束")
        return {"reply": f"已结束会话 {sid}，后置任务（写通话记录、更新画像、生成报告、生成待办）已提交。",
                "session_id": sid, "customer_id": s["customer_id"]}
