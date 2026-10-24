import copy

from cagent import settings
from cagent.config_center import ConfigCenter, validate_pack

MAIN_TOOLS = {"get_customer_profile", "get_overdues", "get_contact_count_today", "get_willingness"}
ACTIONS = {"installment_plan", "record_grace_request", "submit_waiver_request", "loan_priority",
           "record_promise", "reduce_contact", "request_handoff"}
POST = {"write_call_record", "write_behavior", "write_emotion", "refresh_profile", "generate_report", "create_todos"}
RULES = {f"R{i:03d}" for i in range(1, 11)}


def test_get_loads_yaml_and_versions(db_conn):
    cc = ConfigCenter(db_conn, settings.CONFIG_DIR)
    content, version = cc.get("checkpoints")
    assert "detectors" in content and len(version) == 10
    assert cc.get("checkpoints")[1] == version


def test_save_becomes_latest_and_survives_resync(db_conn):
    cc = ConfigCenter(db_conn, settings.CONFIG_DIR)
    content, v1 = cc.get("policy")
    edited = copy.deepcopy(content)
    edited["installment"]["affordability_ratio"] = 0.4
    v2 = cc.save("policy", "default", edited)
    assert v2 != v1
    cc2 = ConfigCenter(db_conn, settings.CONFIG_DIR)
    assert cc2.get("policy") == (edited, v2)


def test_pack_ids(db_conn):
    assert ConfigCenter(db_conn, settings.CONFIG_DIR).pack_ids() == ["auto_loan", "consumer_finance"]


def test_shipped_packs_are_valid(db_conn):
    cc = ConfigCenter(db_conn, settings.CONFIG_DIR)
    for pid in cc.pack_ids():
        pack, _ = cc.get("business_pack", pid)
        assert validate_pack(pack, main_tools=MAIN_TOOLS, collection_actions=ACTIONS, rule_ids=RULES, post_tasks=POST) == []


def test_validate_pack_reports_errors(db_conn):
    pack, _ = ConfigCenter(db_conn, settings.CONFIG_DIR).get("business_pack", "consumer_finance")
    bad = copy.deepcopy(pack)
    bad["pre_tools"].append({"tool": "no_such_tool", "on_failure": "block"})
    bad["stages"][0]["next"] = "不存在的阶段"
    bad["gates"].append("R999")
    errors = validate_pack(bad, main_tools=MAIN_TOOLS, collection_actions=ACTIONS, rule_ids=RULES, post_tasks=POST)
    assert any("no_such_tool" in e for e in errors)
    assert any("不存在的阶段" in e for e in errors)
    assert any("R999" in e for e in errors)
