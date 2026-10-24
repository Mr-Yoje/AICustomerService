from datetime import datetime

import pytest

from cagent import settings
from cagent.agents.ops import SKILL_NAMES, TOOL_NAMES, OpsAgent
from cagent.trace import Trace
from cagent.cases import CaseStore
from cagent.compliance import ComplianceEngine
from cagent.config_center import ConfigCenter
from cagent.llm import GuardedLLM
from cagent.memory import MemoryStore

NOW = datetime(2026, 10, 24, 10, 0)


@pytest.fixture
def ops(db_conn):
    cc = ConfigCenter(db_conn, settings.CONFIG_DIR)
    cp, version = cc.get("checkpoints")
    return OpsAgent(
        db_conn, cc=cc, llm=GuardedLLM(None, db_conn),
        engine=ComplianceEngine(db_conn, cp, version, None),
        policy=cc.get("policy")[0], intents=cc.get("intents")[0],
        main_cfg=cc.get("main_agent")[0], cp_version=version,
        cases=CaseStore(db_conn), memory=MemoryStore(db_conn),
    )


def test_registry_owns_query_tools(ops):
    assert ops.registry.owner == "ops"
    assert TOOL_NAMES <= set(ops.registry.names("tool"))
    assert SKILL_NAMES <= set(ops.registry.names("skill"))


def test_lookup_returns_priority_loan_type(ops):
    found = ops.lookup("C0001")
    assert found.customer_id == "C0001" and found.loan_type


def test_lookup_unknown_customer(ops):
    with pytest.raises(ValueError, match="客户不存在: C9999"):
        ops.lookup("C9999")


def test_answer_overdues_mentions_conflict_record(ops):
    out = ops.answer("查询欠款", "查询C0001的欠款", "C0001", NOW, Trace())
    assert "O0033" in out.reply and out.needs_review and out.review_reasons


def test_answer_profile_includes_willingness(ops):
    out = ops.answer("查询画像", "看下C0002的客户画像", "C0002", NOW, Trace())
    assert "还款意愿" in out.reply and out.willingness_score is not None


def test_answer_unknown_customer(ops):
    with pytest.raises(ValueError, match="客户不存在: C9999"):
        ops.answer("查询画像", "画像", "C9999", NOW, Trace())


def test_eval_report_one_customer_or_all(ops):
    one = ops.answer("生成报告", "生成C0001本月催收评估报告", "C0001", NOW, Trace())
    assert "范围：单客户" in one.reply and "C0001：" in one.reply and "C0002" not in one.reply
    assert "催收次数" in one.reply and "还款意愿" in one.reply and "合规审计" in one.reply
    assert ops.cases.artifacts(kind="催收评估报告")
    all_ = ops.answer("生成报告", "生成全部客户本月催收评估报告", "C0001", NOW, Trace())
    assert "范围：全量" in all_.reply and "C0001：" in all_.reply and "C0010：" in all_.reply


def test_compliance_audit_report_by_customer(ops):
    out = ops.answer("生成报告", "生成本月合规审计报告", None, NOW, Trace())
    assert out.reply.startswith("2026-10 合规审计报告") and "范围：全量" in out.reply
    assert "C0001：" in out.reply and ops.cases.artifacts(kind="合规审计报告")
    one = ops.answer("生成报告", "生成C0003的合规审计报告", "C0003", NOW, Trace())
    assert "范围：单客户" in one.reply and "C0003：" in one.reply and "C0001：" not in one.reply


def test_answer_summary_report_writes_artifact(ops):
    out = ops.answer("生成报告", "生成本月催收报告", None, NOW, Trace())
    assert "催收次数" in out.reply and "/reports/" in out.reply and "查看完整报告" in out.reply
    saved = ops.cases.artifacts(kind="汇总报告")
    assert saved and saved[-1]["content"]["markdown"].startswith("#")


def test_answer_quiet_hours(ops):
    night = datetime(2026, 10, 24, 23, 0)
    out = ops.answer("合规问答", "现在晚上11点了，能给C0003打电话吗？", "C0003", night, Trace())
    assert "R004" in out.hit_rules and "不可以" in out.reply and "08:00" in out.reply
    assert out.compliance and out.compliance[0].checkpoint


def test_prepare_blocks_quiet_hours_without_session(ops, db_conn):
    night = datetime(2026, 10, 24, 23, 30)
    tools = [{"tool": "get_overdues", "on_failure": "block"}]
    out = ops.prepare("C0001", "外呼", "通用消费金融催收", ["R004", "R005"], tools, night, Trace(), "S-none")
    assert out.blocked and "R004" in out.blocked and out.gate and not out.gate.passed
    assert db_conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_prepare_inbound_skips_gate_and_fills_snapshot(ops):
    tools = [
        {"tool": "get_customer_profile", "on_failure": "degrade"},
        {"tool": "get_overdues", "on_failure": "block"},
        {"tool": "get_contact_count_today", "on_failure": "block"},
        {"tool": "get_willingness", "on_failure": "degrade"},
    ]
    out = ops.prepare("C0001", "客户进线", "通用消费金融催收", ["R004", "R005"], tools, NOW, Trace(), "S-in")
    assert out.blocked is None and out.gate is None
    assert out.snapshot.customer_id == "C0001" and out.snapshot.overdues
    assert out.snapshot.willingness is not None


def test_prepare_block_and_degrade(ops):
    blocked = ops.prepare("C0001", "客户进线", "包", [], [{"tool": "missing", "on_failure": "block"}],
                          NOW, Trace(), "S-b")
    assert blocked.blocked.startswith("前置工具 missing 调用失败")
    degraded = ops.prepare("C0001", "客户进线", "包", [], [{"tool": "missing", "on_failure": "warn"}],
                           NOW, Trace(), "S-d")
    assert degraded.blocked is None and degraded.snapshot.degraded == ["missing"]


def test_review_speech_blocks_threat_without_todo(ops, db_conn):
    out = ops.review_speech(
        "你今天不还就上门找你", customer_id=None, scene="话术审核", extra_rules=[],
        rewrite_fallback="请按约定时间处理逾期。", opening="您好，我是工号AI01。", now=NOW, trace=Trace())
    assert not out.passed and out.reply.startswith("[合规拦截]") and "R002" in out.hit_rules
    assert db_conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0] == 0
    assert db_conn.execute("SELECT scene FROM audit_log ORDER BY rowid DESC LIMIT 1").fetchone()["scene"] == "话术审核"


def test_third_party_relay_rewrite_drops_amount_and_name(ops):
    out = ops.review_speech(
        "您好，请问是李女士吗？您同事王先生在我们平台有32600元欠款逾期半年多了，麻烦您转告他赶紧处理，不然我们只能联系他家里人了。",
        customer_id="C0001", scene="话术审核", extra_rules=[],
        rewrite_fallback="麻烦您转告王先生，他有一笔32600元的欠款已逾期。", opening="规范开场", now=NOW, trace=Trace())
    suggestion = out.reply.split("建议改写：", 1)[-1]
    assert "32600" not in suggestion and "王先生" not in suggestion and "李女士" not in suggestion and "转告" not in suggestion


def test_review_speech_strips_third_party_debt(ops):
    out = ops.review_speech(
        "不然我们只能联系你同事了，王先生欠款32600元，请转告李女士。", customer_id="C0001", scene="话术审核",
        extra_rules=[], rewrite_fallback="王先生还欠32600元，请转告李女士。", opening="规范开场", now=NOW, trace=Trace())
    assert "R009" in out.hit_rules
    assert "王先生" not in out.reply and "李女士" not in out.reply and "32600" not in out.reply
    assert out.reply.endswith("请只与客户本人沟通账单，不要提及他人的姓名和欠款。")


def test_profile_reads_customer_memory(ops):
    ops.memory.set("customer", "C0001", "last_willingness", {"score": 42, "level": "低"}, "测试")
    ops.memory.set("customer", "C0001", "open_promises", [{"amount": 3000, "date": "2026-11-01"}], "测试")
    out = ops.answer("查询画像", "查一下画像", "C0001", NOW, Trace())
    assert "上次会话还款意愿42分" in out.reply and "2026-11-01" in out.reply


def test_review_speech_pass_uses_opening(ops):
    out = ops.review_speech(
        "您好，请核实本人身份。", customer_id="C0001", scene="人工坐席", extra_rules=[],
        rewrite_fallback="请按约定时间处理逾期。", opening="规范开场", now=NOW, trace=Trace(), session_id="S1")
    assert out.passed and out.reply.startswith("[合规通过]") and "规范开场" in out.reply
