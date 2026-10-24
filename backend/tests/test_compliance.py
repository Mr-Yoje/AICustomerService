import csv
from datetime import datetime

import pytest

from cagent import db, settings
from cagent.audit import export_audit_csv, write_audit
from cagent.compliance import ComplianceEngine
from cagent.config_center import ConfigCenter
from cagent.llm import FakeLLM, GuardedLLM

DAY = datetime(2026, 10, 24, 10, 0)


@pytest.fixture
def engine(db_conn):
    cfg, version = ConfigCenter(db_conn, settings.CONFIG_DIR).get("checkpoints")
    return ComplianceEngine(db_conn, cfg, version, None)


def rule_ids(result):
    return [h.rule_id for h in result.hits]


def test_threat_hit_triggers_handoff(engine):
    r = engine.check_agent_speech("如果你今天不还款，我们就上门催收！", use_semantic=False)
    assert rule_ids(r) == ["R002"]
    assert r.hits[0].evidence == "上门催收"
    assert r.block and r.handoff


def test_negation_is_not_violation(engine):
    r = engine.check_agent_speech("我们不会上门打扰您，我也无法承诺减免全部利息。", use_semantic=False)
    assert r.passed


@pytest.mark.parametrize("text,rid", [
    ("我保证帮您把利息全部减免", "R003"),
    ("我是法院的，你再不还试试", "R007"),
    ("你个老赖", "R001"),
    ("不还也没事，放心吧", "R008"),
    ("我们会联系你的家人", "R009"),
])
def test_keyword_rules(engine, text, rid):
    assert rid in rule_ids(engine.check_agent_speech(text, use_semantic=False))


def test_repeated_pressure(engine):
    r = engine.check_agent_speech("请尽快还款。今天必须还。马上还！", use_semantic=False)
    assert "R010" in rule_ids(r)
    assert r.handoff is False
    assert "R010" in rule_ids(engine.check_agent_speech("请尽快还款", pressure_so_far=2, use_semantic=False))
    assert engine.check_agent_speech("请问方便沟通吗", pressure_so_far=5, use_semantic=False).passed


def test_pack_extra_rule(engine):
    extra = [{"rule_id": "BR-AUTO-01", "rule_type": "威胁", "rule_content": "不得以拖车、收车相威胁",
              "violation_level": "严重", "action": "人工接管", "patterns": ["拖车"]}]
    r = engine.check_agent_speech("再不还就安排拖车", extra_rules=extra, use_semantic=False)
    assert rule_ids(r) == ["BR-AUTO-01"] and r.handoff


def test_semantic_hits_filtered_and_degraded(db_conn):
    cfg, version = ConfigCenter(db_conn, settings.CONFIG_DIR).get("checkpoints")
    fake = FakeLLM(lambda s, u: {"hits": [{"rule_id": "R008", "evidence": "拖着也行", "confidence": 0.9},
                                          {"rule_id": "R999", "evidence": "x", "confidence": 0.99},
                                          {"rule_id": "R002", "evidence": "y", "confidence": 0.3}]})
    r = ComplianceEngine(db_conn, cfg, version, GuardedLLM(fake, db_conn)).check_agent_speech("这钱拖着也行")
    assert rule_ids(r) == ["R008"] and r.hits[0].method == "语义"
    failing = ComplianceEngine(db_conn, cfg, version, GuardedLLM(FakeLLM(fail=True), db_conn))
    r2 = failing.check_agent_speech("您好，请问方便沟通吗")
    assert r2.passed and r2.degraded and r2.notes


def test_quiet_hours_block(engine):
    r = engine.check_operator_action(action="外呼", customer_id="C0003", now=datetime(2026, 10, 24, 23, 0))
    assert rule_ids(r) == ["R004"]
    assert r.next_allowed == "2026-10-25T08:00"
    assert r.hits[0].action == "阻断外呼并提示下次可外呼时间"


def test_frequency_block_excludes_inbound(engine, db_conn):
    for i in range(3):
        db.insert_generated(db_conn, "fact_call_history", {"call_id": f"CH-F{i}", "customer_id": "C0001",
                            "call_time": f"2026-10-24 {i + 8:02d}:00", "call_method": "电话"}, session_id=None)
    db.insert_generated(db_conn, "fact_call_history", {"call_id": "CH-IN", "customer_id": "C0002",
                        "call_time": "2026-10-24 09:00", "call_method": "客户进线"}, session_id=None)
    db_conn.commit()
    assert rule_ids(engine.check_operator_action(action="外呼", customer_id="C0001", now=DAY)) == ["R005"]
    assert engine.check_operator_action(action="外呼", customer_id="C0002", now=DAY).passed


def test_operator_only_filter_and_contact_target(engine):
    night = datetime(2026, 10, 24, 23, 0)
    assert engine.check_operator_action(action="外呼", customer_id="C0003", now=night, only=["R005"]).passed
    r = engine.check_operator_action(action="外呼", customer_id="C0003", now=DAY, contact_target="家人")
    assert rule_ids(r) == ["R009"]


def test_customer_input_signals(engine):
    r = engine.check_customer_input("你们天天打电话烦不烦！再打我就投诉你们！我要转人工")
    assert r.passed and "投诉" in r.signals and "要求转人工" in r.signals


def test_audit_written_and_exported(engine, db_conn, tmp_path):
    r = engine.check_agent_speech("如果你今天不还款，我们就上门催收！", use_semantic=False)
    ids = write_audit(db_conn, result=r, customer_id="C0005", scene="话术审核", content="如果你今天不还款，我们就上门催收！",
                      config_version=engine.version, ts=DAY)
    assert len(ids) == 1
    path = tmp_path / "audit.csv"
    assert export_audit_csv(db_conn, path) == 1
    with path.open(encoding="utf-8-sig") as f:
        row = next(csv.DictReader(f))
    assert row["命中规则编号"] == "R002" and row["是否转人工"] == "是"
