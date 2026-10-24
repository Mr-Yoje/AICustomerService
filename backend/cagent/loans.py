from __future__ import annotations

import sqlite3

from pydantic import BaseModel

FIELD_NAMES = {"overdue_days": "逾期天数", "overdue_amount": "欠款金额", "overdue_periods": "逾期期次"}


class LoanItem(BaseModel):
    overdue_id: str
    loan_id: str | None
    loan_type: str | None
    overdue_amount: float
    overdue_days: int
    overdue_periods: int | None
    last_repay_date: str | None
    recommended_template_id: str | None
    owner_conflict: bool


class LoanPriority(BaseModel):
    customer_id: str
    items: list[LoanItem]
    total_amount: float
    suggestion: str
    basis: str
    conflicts: list[str] = []


def prioritize(conn: sqlite3.Connection, customer_id: str, policy: dict) -> LoanPriority:
    def loan_col(col: str) -> str:
        return (
            f"COALESCE((SELECT l.{col} FROM dim_loan l WHERE l.loan_id = o.loan_id AND l.customer_id = o.customer_id LIMIT 1), "
            f"(SELECT l.{col} FROM dim_loan l WHERE l.loan_id = o.loan_id ORDER BY l.rowid LIMIT 1))"
        )

    rows = conn.execute(
        "SELECT o.overdue_id, o.loan_id, " + loan_col("loan_type") + " AS loan_type, "
        + loan_col("customer_id") + " AS owner, o.overdue_amount, o.overdue_days, "
        "o.overdue_periods, o.last_repay_date, o.recommended_template_id FROM fact_overdue o "
        "WHERE o.customer_id=? ORDER BY o.overdue_id", (customer_id,))
    items = [LoanItem(overdue_id=r["overdue_id"], loan_id=r["loan_id"], loan_type=r["loan_type"],
                      overdue_amount=r["overdue_amount"] or 0, overdue_days=r["overdue_days"] or 0,
                      overdue_periods=r["overdue_periods"], last_repay_date=r["last_repay_date"],
                      recommended_template_id=r["recommended_template_id"],
                      owner_conflict=r["owner"] is not None and r["owner"] != customer_id) for r in rows]
    sort_rules = policy["priority"]["sort"]
    for rule in reversed(sort_rules):
        items.sort(key=lambda i, f=rule["field"]: getattr(i, f) or 0, reverse=rule.get("desc", True))
    if items:
        top = items[0]
        suggestion = (f"建议优先处理 {top.overdue_id}（{top.loan_type or '未知类型'}，逾期{top.overdue_days}天，"
                      f"欠款{top.overdue_amount:.0f}元）")
    else:
        suggestion = "当前无逾期记录"
    basis = "排序规则：" + "，".join(
        f"{FIELD_NAMES.get(r['field'], r['field'])}{'从高到低' if r.get('desc', True) else '从低到高'}" for r in sort_rules)
    conflicts = [f"{i.overdue_id} 关联的贷款 {i.loan_id} 在贷款表中不属于该客户，需人工复核" for i in items if i.owner_conflict]
    return LoanPriority(customer_id=customer_id, items=items, total_amount=round(sum(i.overdue_amount for i in items), 2),
                        suggestion=suggestion, basis=basis, conflicts=conflicts)
