from __future__ import annotations

import re
import sqlite3

from pydantic import BaseModel

METHOD = "利率按年化计算，月供按等额本息；手续费按欠款金额一次性收取"


def parse_condition(text: str) -> dict:
    t = text or ""
    m1 = re.search(r"逾期\s*(?:≤|<=)\s*(\d+)\s*天", t)
    m2 = re.search(r"逾期\s*(?:>|＞)\s*(\d+)\s*天", t)
    m3 = re.search(r"信用评分\s*(?:≥|>=)\s*(\d+)", t)
    return {"text": t, "max_days": int(m1.group(1)) if m1 else None,
            "min_days_exclusive": int(m2.group(1)) if m2 else None,
            "min_credit": int(m3.group(1)) if m3 else None, "lump_sum": "一次性结清" in t}


def load_templates(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute('SELECT template_id, template_type, max_periods, rate, fee, "condition" '
                        'FROM dim_installment_template ORDER BY template_id')
    return [{**dict(r), "cond": parse_condition(r["condition"])} for r in rows]


def eligible(cond: dict, days: int, credit: int) -> bool:
    if cond["max_days"] is not None and days > cond["max_days"]:
        return False
    if cond["min_days_exclusive"] is not None and days <= cond["min_days_exclusive"]:
        return False
    if cond["min_credit"] is not None and credit < cond["min_credit"]:
        return False
    return True


def monthly_payment(principal: float, annual_rate: float, n: int) -> float:
    if n <= 1:
        return principal
    r = annual_rate / 12
    if r == 0:
        return principal / n
    return principal * r * (1 + r) ** n / ((1 + r) ** n - 1)


class Plan(BaseModel):
    template_id: str
    template_type: str
    periods: int
    annual_rate: float
    fee_rate: float
    principal: float
    monthly_payment: float
    fee: float
    total_cost: float
    lump_sum: bool
    affordable: bool
    note: str = ""


class PlanResult(BaseModel):
    customer_id: str
    principal: float
    overdue_ids: list[str]
    max_overdue_days: int
    credit_score: int
    affordable_monthly: float
    affordability_source: str
    eligible_templates: list[str]
    plans: list[Plan]
    conflicts: list[str] = []
    message: str = ""
    method: str = METHOD


def _plan(t: dict, principal: float, n: int, afford: float, note: str = "") -> Plan:
    mp = monthly_payment(principal, t["rate"], n)
    fee = principal * t["fee"]
    return Plan(template_id=t["template_id"], template_type=t["template_type"], periods=n, annual_rate=t["rate"],
                fee_rate=t["fee"], principal=principal, monthly_payment=round(mp, 2), fee=round(fee, 2),
                total_cost=round(mp * n + fee, 2), lump_sum=t["cond"]["lump_sum"], affordable=mp <= afford, note=note)


def build_plans(conn: sqlite3.Connection, customer_id: str, policy: dict, *, allowed_templates=None,
                monthly_affordable: float | None = None, requested_periods: int | None = None,
                lump_sum_requested: bool = False) -> PlanResult:
    cfg = policy["installment"]
    cust = conn.execute("SELECT credit_score, monthly_income FROM dim_customer WHERE customer_id=?",
                        (customer_id,)).fetchone()
    if cust is None:
        raise ValueError(f"客户不存在: {customer_id}")
    ods = conn.execute("SELECT overdue_id, overdue_amount, overdue_days, recommended_template_id FROM fact_overdue "
                       "WHERE customer_id=? ORDER BY overdue_id", (customer_id,)).fetchall()
    principal = round(sum(o["overdue_amount"] or 0 for o in ods), 2)
    days = max((o["overdue_days"] or 0 for o in ods), default=0)
    credit = cust["credit_score"] or 0
    if monthly_affordable:
        afford, source = float(monthly_affordable), "客户表述"
    else:
        ratio = cfg["affordability_ratio"]
        afford, source = round((cust["monthly_income"] or 0) * ratio, 2), f"按月收入的{int(ratio * 100)}%估算"
    all_templates = load_templates(conn)
    by_id = {t["template_id"]: t for t in all_templates}
    templates = [t for t in all_templates if allowed_templates is None or t["template_id"] in allowed_templates]
    elig = [t for t in templates if eligible(t["cond"], days, credit)]
    conflicts = []
    for o in ods:
        rec = o["recommended_template_id"]
        if rec and (rec not in by_id or not eligible(by_id[rec]["cond"], o["overdue_days"] or 0, credit)):
            conflicts.append(f"{o['overdue_id']} 推荐模板 {rec} 与适用条件不符（逾期{o['overdue_days']}天、信用评分{credit}），已按适用条件重新匹配")
    plans: list[Plan] = []
    for t in elig:
        if t["cond"]["lump_sum"]:
            if requested_periods in (None, 1):
                plans.append(_plan(t, principal, 1, afford, "一次性结清可申请减免违约金，需审批，不承诺结果"))
            continue
        maxp = t["max_periods"]
        if requested_periods:
            if 1 < requested_periods <= maxp:
                plans.append(_plan(t, principal, requested_periods, afford))
            continue
        options = sorted({p for p in cfg["period_options"] if p <= maxp} | {maxp})
        chosen = next((p for p in options if monthly_payment(principal, t["rate"], p) <= afford), maxp)
        plans.append(_plan(t, principal, chosen, afford))
    message = ""
    if principal <= 0:
        message = "当前无逾期欠款"
    elif requested_periods and not plans:
        limit = max((t["max_periods"] for t in elig if not t["cond"]["lump_sum"]), default=0)
        message = f"可用方案最长{limit}期，暂无法办理{requested_periods}期"
    elif lump_sum_requested and not any(p.lump_sum for p in plans):
        message = "当前逾期情况不满足一次性结清减免条件，减免仍可提交申请，结果以审批为准"
    plans.sort(key=lambda p: (not p.affordable, p.total_cost))
    return PlanResult(customer_id=customer_id, principal=principal, overdue_ids=[o["overdue_id"] for o in ods],
                      max_overdue_days=days, credit_score=credit, affordable_monthly=afford,
                      affordability_source=source, eligible_templates=[t["template_id"] for t in elig],
                      plans=plans[:cfg["max_plans"]], conflicts=conflicts, message=message)
