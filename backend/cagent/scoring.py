from __future__ import annotations

import sqlite3
from typing import Iterable

from pydantic import BaseModel


class Factor(BaseModel):
    name: str
    score: float
    weight: float
    contribution: float
    evidence: list[str] = []
    note: str = ""


class Willingness(BaseModel):
    customer_id: str
    score: int
    level: str
    factors: list[Factor]
    adjustments: list[dict] = []
    confidence: float
    needs_review: bool
    review_reasons: list[str] = []


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _signed(items: list[tuple[str, str]], positive: list[str], negative: list[str], step: float):
    pos = [i for i in items if i[1] in positive]
    neg = [i for i in items if i[1] in negative]
    return _clamp(50 + step * (len(pos) - len(neg))), [i[0] for i in pos + neg], f"正向{len(pos)}条，负向{len(neg)}条"


def score_customer(conn: sqlite3.Connection, customer_id: str, policy: dict,
                   dialog_intents: Iterable[str] = ()) -> Willingness:
    cfg = policy["scoring"]
    w = cfg["weights"]
    cust = conn.execute("SELECT * FROM dim_customer WHERE customer_id=?", (customer_id,)).fetchone()
    if cust is None:
        raise ValueError(f"客户不存在: {customer_id}")
    factors: list[Factor] = []
    covered = 0

    def add(name: str, key: str, score: float, evidence: list[str], note: str, has_data: bool) -> None:
        nonlocal covered
        covered += 1 if has_data else 0
        factors.append(Factor(name=name, score=round(score, 1), weight=w[key], contribution=round(score * w[key], 2),
                              evidence=evidence, note=note))

    reps = conn.execute("SELECT repay_id, is_on_time FROM fact_repayment WHERE customer_id=? ORDER BY repay_time",
                        (customer_id,)).fetchall()
    if reps:
        ontime = sum(1 for r in reps if r["is_on_time"] == "是")
        add("还款履约", "repayment", ontime / len(reps) * 100, [r["repay_id"] for r in reps],
            f"按时还款{ontime}/{len(reps)}次", True)
    else:
        add("还款履约", "repayment", 50, [], "无还款记录，取中性值", False)

    ods = conn.execute("SELECT overdue_id, overdue_amount, overdue_days FROM fact_overdue WHERE customer_id=?",
                       (customer_id,)).fetchall()
    if ods:
        sev = cfg["severity"]
        maxd = max(o["overdue_days"] or 0 for o in ods)
        total = sum(o["overdue_amount"] or 0 for o in ods)
        ratio = total / max(cust["monthly_income"] or 0, 1)
        cap = sev["debt_income_cap"]
        s = (_clamp(100 - maxd / sev["max_days"] * 100) + _clamp(100 - min(ratio, cap) / cap * 100)) / 2
        add("逾期严重度", "severity", s, [o["overdue_id"] for o in ods],
            f"最长逾期{maxd}天，总欠款{total:.0f}元，为月收入的{ratio:.2f}倍", True)
    else:
        add("逾期严重度", "severity", 100, [], "无逾期记录", False)

    b = cfg["behavior"]
    bs = [(r["behavior_id"], r["behavior_type"]) for r in conn.execute(
        "SELECT behavior_id, behavior_type FROM fact_user_behavior WHERE customer_id=?", (customer_id,))]
    s, ev, note = _signed(bs, b["positive"], b["negative"], b["step"])
    add("行为信号", "behavior", s, ev, note, bool(bs))

    c = cfg["call_result"]
    cs = [(r["call_id"], r["call_result"]) for r in conn.execute(
        "SELECT call_id, call_result FROM fact_call_history WHERE customer_id=?", (customer_id,))]
    s, ev, note = _signed(cs, c["positive"], c["negative"], c["step"])
    add("历史催收结果", "call_result", s, ev, note, bool(cs))

    e = cfg["emotion"]
    emos = conn.execute("SELECT emotion_id, emotion_type, emotion_intensity FROM fact_emotion_log WHERE customer_id=? "
                        "ORDER BY record_time DESC LIMIT ?", (customer_id, e["recent"])).fetchall()
    if emos:
        vals = [(r["emotion_intensity"] or 0) * (1 if r["emotion_type"] in e["positive"]
                                                 else -1 if r["emotion_type"] in e["negative"] else 0) for r in emos]
        add("情绪", "emotion", _clamp(50 + 50 * sum(vals) / len(vals)), [r["emotion_id"] for r in emos],
            "、".join(f"{r['emotion_type']}({r['emotion_intensity']})" for r in emos), True)
    else:
        add("情绪", "emotion", 50, [], "无情绪记录，取中性值", False)

    cr = cfg["credit"]
    credit = cust["credit_score"] or cr["min"]
    norm = _clamp((credit - cr["min"]) / (cr["max"] - cr["min"]) * 100)
    lvl = cr["level_scores"].get(cust["customer_level"], 50)
    add("信用画像", "credit", norm * (1 - cr["level_weight"]) + lvl * cr["level_weight"], [customer_id],
        f"信用评分{credit}，客户等级{cust['customer_level']}", True)

    base = sum(f.contribution for f in factors)
    adjustments = [{"intent": i, "delta": cfg["dialog_adjust"][i]} for i in dict.fromkeys(dialog_intents)
                   if cfg["dialog_adjust"].get(i)]
    score = int(round(_clamp(base + sum(a["delta"] for a in adjustments))))
    lv = cfg["levels"]
    level = "高" if score >= lv["high"] else "中" if score >= lv["medium"] else "低"
    rv = cfg["review"]
    conflicts = conn.execute("SELECT record_id FROM data_quality WHERE customer_id=?", (customer_id,)).fetchall()
    confidence = round(_clamp(0.5 + 0.5 * covered / len(factors) - (rv["conflict_penalty"] if conflicts else 0), 0, 1), 2)
    reasons = []
    if conflicts:
        reasons.append(f"存在{len(conflicts)}条数据冲突（如 {conflicts[0]['record_id']}）")
    if confidence < rv["min_confidence"]:
        reasons.append(f"置信度{confidence}低于{rv['min_confidence']}")
    return Willingness(customer_id=customer_id, score=score, level=level, factors=factors, adjustments=adjustments,
                       confidence=confidence, needs_review=bool(reasons), review_reasons=reasons)


def max_overdue_days(conn: sqlite3.Connection, customer_id: str) -> int:
    row = conn.execute("SELECT MAX(overdue_days) FROM fact_overdue WHERE customer_id=?", (customer_id,)).fetchone()
    return int(row[0] or 0)


def choose_strategy(policy: dict, level: str, max_days: int) -> str:
    for rule in policy["strategies"]:
        if rule["level"] == level and ("max_days" not in rule or max_days <= rule["max_days"]):
            return rule["strategy"]
    return "转人工重点跟进"
