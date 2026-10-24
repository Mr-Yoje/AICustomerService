import pytest

from cagent import settings
from cagent.config_center import ConfigCenter
from cagent.scoring import choose_strategy, max_overdue_days, score_customer


@pytest.fixture
def policy(db_conn):
    return ConfigCenter(db_conn, settings.CONFIG_DIR).get("policy")[0]


def test_score_range_factors_and_evidence(db_conn, policy):
    w = score_customer(db_conn, "C0001", policy)
    assert 0 <= w.score <= 100
    assert [f.name for f in w.factors] == ["还款履约", "逾期严重度", "行为信号", "历史催收结果", "情绪", "信用画像"]
    assert "RP0006" in w.factors[0].evidence
    assert w.level in ("高", "中", "低")


def test_data_conflict_requires_review(db_conn, policy):
    w = score_customer(db_conn, "C0001", policy)
    assert w.needs_review and any("数据冲突" in r for r in w.review_reasons)


def test_dialog_adjustments(db_conn, policy):
    promise = score_customer(db_conn, "C0002", policy, ["承诺还款"])
    refuse = score_customer(db_conn, "C0002", policy, ["拒绝还款"])
    assert promise.score > refuse.score
    assert promise.adjustments == [{"intent": "承诺还款", "delta": 10}]


def test_unknown_customer(db_conn, policy):
    with pytest.raises(ValueError):
        score_customer(db_conn, "C9999", policy)


def test_max_overdue_days(db_conn):
    assert max_overdue_days(db_conn, "C0007") == 365


@pytest.mark.parametrize("level,days,expected", [
    ("高", 20, "温和提醒"), ("高", 100, "协商分期"), ("中", 90, "协商分期"),
    ("中", 120, "提交结清减免申请"), ("低", 365, "转人工重点跟进"),
])
def test_choose_strategy(policy, level, days, expected):
    assert choose_strategy(policy, level, days) == expected
