import pytest

from cagent import settings
from cagent.config_center import ConfigCenter
from cagent.installment import build_plans, monthly_payment, parse_condition
from cagent.loans import prioritize

AUTO = ["T002", "T003", "T004", "T006"]


@pytest.fixture
def policy(db_conn):
    return ConfigCenter(db_conn, settings.CONFIG_DIR).get("policy")[0]


def test_parse_condition():
    assert parse_condition("逾期≤30天且信用评分≥650") == {
        "text": "逾期≤30天且信用评分≥650", "max_days": 30, "min_days_exclusive": None, "min_credit": 650, "lump_sum": False}
    c = parse_condition("一次性结清且逾期>60天可减免违约金")
    assert c["lump_sum"] and c["min_days_exclusive"] == 60 and c["max_days"] is None


def test_monthly_payment():
    assert monthly_payment(1200, 0.0, 12) == 100
    assert 1700 < monthly_payment(20000, 0.072, 12) < 1760


def test_plans_for_c0003_default_affordability(db_conn, policy):
    r = build_plans(db_conn, "C0003", policy)
    assert r.principal == 20000 and r.max_overdue_days == 120
    assert r.eligible_templates == ["T004", "T006"]
    top = r.plans[0]
    assert (top.template_id, top.periods, top.affordable) == ("T004", 12, True)
    assert abs(top.monthly_payment - monthly_payment(20000, 0.072, 12)) < 0.01
    assert top.fee == 300.0
    assert any("O0038" in c and "T002" in c for c in r.conflicts)


def test_requested_periods_within_and_beyond_limit(db_conn, policy):
    ok = build_plans(db_conn, "C0003", policy, allowed_templates=AUTO, requested_periods=24)
    assert [(p.template_id, p.periods) for p in ok.plans] == [("T004", 24)]
    too_long = build_plans(db_conn, "C0003", policy, allowed_templates=AUTO, requested_periods=36)
    assert too_long.plans == [] and "36" in too_long.message and "24" in too_long.message


def test_customer_stated_affordability(db_conn, policy):
    r = build_plans(db_conn, "C0003", policy, allowed_templates=AUTO, monthly_affordable=1000)
    assert r.affordability_source == "客户表述"
    assert (r.plans[0].template_id, r.plans[0].periods) == ("T004", 24)


def test_priority_sorting_and_conflicts(db_conn, policy):
    lp = prioritize(db_conn, "C0010", policy)
    assert [i.overdue_id for i in lp.items][:2] == ["O0034", "O0004"]
    assert "O0034" in lp.suggestion
    single = prioritize(db_conn, "C0007", policy)
    assert len(single.items) == 1 and single.items[0].owner_conflict and single.conflicts


def test_duplicate_loan_id_uses_the_customers_own_loan(db_conn, policy):
    from cagent import db

    db.insert_generated(db_conn, "dim_loan", {
        "loan_id": "L0022", "customer_id": "C0007", "loan_type": "消费贷", "principal": 2000,
        "annual_rate": 0.1, "periods": 6, "issue_date": "2024-01-01", "due_date": "2024-07-01",
    }, None)
    db_conn.commit()
    single = prioritize(db_conn, "C0007", policy)
    assert len(single.items) == 1 and single.items[0].loan_type == "消费贷" and not single.items[0].owner_conflict
