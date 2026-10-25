from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from ..clock import cn_to_int
from ..llm import GuardedLLM, LLMError

MAX_LEN = 500
_OK_CHARS = re.compile(r"[\u4e00-\u9fffA-Za-z0-9，。！？、,.!?；;：:\s%￥元\-（）()<>_]")
_PERIODS = re.compile(r"(?<!第)(\d{1,2}|[一二三四五六七八九十两]{1,3})\s*期")
_MONTHLY = re.compile(r"(?:每月|每个月|月供|一个月)[^\d，。]{0,6}(\d+(?:\.\d+)?)")
_AMOUNT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块)")
_DATE = re.compile(r"(\d{1,2}月\d{1,2}[日号]|下个月\d{1,2}[日号]|\d{1,2}[日号]|明天|后天|下周[一二三四五六日天]?|月底|下个月)")
_CID = re.compile(r"C\d{4}")

SPEAKER_SYSTEM = (
    "你负责判断一句话是谁说的。customer=被催收的客户本人；agent=催收坐席（对客户说话的一方）；"
    "operator=内部运营人员（向系统查询客户、要报告、问能否外呼、下达发起或结束催收的指令）。"
    "scene_type_hint 只是参考。若给出上一轮，那是这句话之前刚结束的至多两条对话，用来判断当前句在接谁的话。\n"
    '输出 json，例如 {"speaker": "customer", "confidence": 0.9}'
)


class SpeakerResult(BaseModel):
    speaker: Literal["customer", "agent", "operator"]
    confidence: float
    method: str
    needs_clarify: bool = False


class Understanding(BaseModel):
    customer_intent: str
    emotion: str
    emotion_intensity: float
    amount: float | None = None
    periods: int | None = None
    date: str | None = None
    monthly_affordable: float | None = None
    confidence: float
    conflict: bool = False
    method: str
    current_node: str | None = None
    next_node: str | None = None


class OperatorIntent(BaseModel):
    intent: str
    customer_id: str | None = None
    confidence: float
    method: str


def _num(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"\d+(?:\.\d+)?", str(v))
    return float(m.group(0)) if m else None


def _int(v) -> int | None:
    n = _num(v)
    return int(n) if n else None


def _unit(v, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return default


def regex_entities(text: str) -> dict:
    out = {"periods": None, "monthly_affordable": None, "amount": None, "date": None}
    if m := _PERIODS.search(text):
        out["periods"] = cn_to_int(m.group(1))
    if m := _MONTHLY.search(text):
        out["monthly_affordable"] = float(m.group(1))
    if m := _AMOUNT.search(text):
        out["amount"] = float(m.group(1))
    if m := _DATE.search(text):
        out["date"] = m.group(1)
    return out


class Understander:
    def __init__(self, llm: GuardedLLM, cfg: dict):
        self.llm = llm
        self.cfg = cfg
        self.low = cfg.get("low_confidence", 0.6)

    def preprocess(self, text: str | None) -> tuple[str, str | None]:
        t = (text or "").strip()
        if not t:
            return "", "空输入"
        if "\ufffd" in t or len(_OK_CHARS.findall(t)) / len(t) < 0.5:
            return t, "乱码"
        if len(t) > MAX_LEN:
            return t[:MAX_LEN], "超长已截断"
        return t, None

    def _markers(self, text: str) -> str | None:
        m = self.cfg["speaker_markers"]
        agent = sum(1 for k in m["agent"] if k in text)
        operator = sum(1 for k in m["operator"] if k in text) + (1 if _CID.search(text) else 0)
        if agent == operator == 0:
            return None
        return "agent" if agent > operator else "operator"

    def speaker(self, text: str, scene_type: str | None = None, recent: list[dict] | None = None) -> SpeakerResult:
        payload = {"text": text, "scene_type_hint": scene_type or ""}
        if recent:
            payload["上一轮"] = recent
        if self.llm.available:
            try:
                out = self.llm.json("speaker", SPEAKER_SYSTEM, payload)
                sp, conf = out.get("speaker"), _unit(out.get("confidence"), 0.0)
                if sp in ("customer", "agent", "operator") and conf >= self.low:
                    return SpeakerResult(speaker=sp, confidence=conf, method="大模型")
            except LLMError:
                pass
        marker = self._markers(text)
        if marker:
            return SpeakerResult(speaker=marker, confidence=0.7, method="关键词兜底")
        return SpeakerResult(speaker="customer", confidence=0.5, method="默认客户", needs_clarify=True)

    def _customer_system(self) -> str:
        return (
            "你是催收对话的理解模块，分析客户这一句话。\n"
            f"customer_intent 只能取：{'、'.join(self.cfg['customer_intents'])}；\n"
            f"emotion 只能取：{'、'.join(self.cfg['emotions'])}，emotion_intensity 取 0 到 1；\n"
            "客户提到每月能还多少、想分几期、金额、日期时，分别填 monthly_affordable、periods、amount、date，没有则填 null；"
            "尖括号占位符原样填写；\n"
            "conflict 表示同一句话里意图互相矛盾；confidence 为你对意图判断的把握（0 到 1）。\n"
            "若上下文含 nodes，请根据客户这句话和已有对话判断 current_node 与 next_node："
            "current_node 是对话此刻所在的节点，next_node 是回答完这句后应进入的节点。"
            "二者都必须是 nodes 里的 id，判断不了则填 null。不要按 nodes 的顺序自动取下一个。\n"
            '输出 json，例如 {"customer_intent": "要求分期", "emotion": "焦虑", "emotion_intensity": 0.5, '
            '"monthly_affordable": null, "periods": 12, "amount": null, "date": null, "confidence": 0.85, '
            '"conflict": false, "current_node": null, "next_node": null}'
        )

    @staticmethod
    def _node_id(value, context: dict | None) -> str | None:
        ids = {n.get("id") for n in (context or {}).get("nodes") or [] if isinstance(n, dict)}
        return value if isinstance(value, str) and value in ids else None

    @staticmethod
    def _infer_from_entities(intent: str, rx: dict) -> str:
        if intent == "其他" and (rx["periods"] or rx["monthly_affordable"]):
            return "要求分期"
        return intent

    def customer(self, text: str, context: dict | None = None) -> Understanding:
        rx = regex_entities(text)
        if self.llm.available:
            try:
                out = self.llm.json("understand", self._customer_system(), {"客户说": text, "上下文": context or {}})
                intent = out.get("customer_intent")
                intent = intent if intent in self.cfg["customer_intents"] else "其他"
                emotion = out.get("emotion")
                return Understanding(
                    customer_intent=self._infer_from_entities(intent, rx),
                    emotion=emotion if emotion in self.cfg["emotions"] else "平静",
                    emotion_intensity=_unit(out.get("emotion_intensity"), 0.3),
                    amount=rx["amount"] or _num(out.get("amount")),
                    periods=rx["periods"] or _int(out.get("periods")),
                    date=rx["date"] or (str(out["date"]) if out.get("date") else None),
                    monthly_affordable=rx["monthly_affordable"] or _num(out.get("monthly_affordable")),
                    confidence=_unit(out.get("confidence"), 0.5), conflict=bool(out.get("conflict")), method="大模型",
                    current_node=self._node_id(out.get("current_node"), context),
                    next_node=self._node_id(out.get("next_node"), context),
                )
            except LLMError:
                pass
        return self.fallback_customer(text)

    def fallback_customer(self, text: str) -> Understanding:
        kw = self.cfg["customer_intents"]
        rx = regex_entities(text)
        intent = next((name for name, words in kw.items() if any(w in text for w in words)), "其他")
        intent = self._infer_from_entities(intent, rx)
        conflict = any(w in text for w in kw.get("承诺还款", [])) and any(w in text for w in kw.get("拒绝还款", []))
        emotion, intensity = self.cfg.get("fallback_emotion", {}).get(intent, ["平静", 0.2])
        return Understanding(customer_intent=intent, emotion=emotion, emotion_intensity=float(intensity),
                             **rx, confidence=0.6 if intent != "其他" else 0.4, conflict=conflict,
                             method="关键词兜底")

    def operator(self, text: str) -> OperatorIntent:
        m = _CID.search(text)
        cid = m.group(0) if m else None
        intents = self.cfg["operator_intents"]
        if self.llm.available:
            system = (f"你是运营助手的指令理解模块。intent 只能取：{'、'.join(intents)}。若提到客户编号（形如 C0001）填 customer_id。\n"
                      '输出 json，例如 {"intent": "查询欠款", "customer_id": "C0001", "confidence": 0.9}')
            try:
                out = self.llm.json("operator_intent", system, {"text": text})
                it, conf = out.get("intent"), _unit(out.get("confidence"), 0.0)
                if it in intents and conf >= self.low:
                    llm_cid = out.get("customer_id")
                    llm_cid = llm_cid if isinstance(llm_cid, str) and _CID.fullmatch(llm_cid) else None
                    return OperatorIntent(intent=it, customer_id=cid or llm_cid, confidence=conf, method="大模型")
            except LLMError:
                pass
        it = next((name for name, words in intents.items() if any(w in text for w in words)), "其他")
        return OperatorIntent(intent=it, customer_id=cid, confidence=0.6 if it != "其他" else 0.4, method="关键词兜底")
