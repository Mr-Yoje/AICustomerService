import json
import logging
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.service import Service
from cagent import settings
from cagent.agents.main import MODE_COLLECTION, MODE_HANDOFF, MODE_OPERATOR, MainAgent

NOW = datetime(2026, 10, 24, 10, 0).isoformat()


@pytest.fixture
def service(db_conn, db_path):
    return Service(MainAgent(db_conn, db_path=db_path, config_dir=settings.CONFIG_DIR, offline=True, sync_post=True))


@pytest.fixture
def client(service, tmp_path):
    return TestClient(create_app(service, web_dist=tmp_path / "no-dist"))


def sse(resp) -> list[tuple[str, dict]]:
    events, kind = [], None
    for line in resp.text.splitlines():
        if line.startswith("event:"):
            kind = line[6:].strip()
        elif line.startswith("data:"):
            events.append((kind, json.loads(line[5:].strip())))
    return events


def chat(client, **body):
    resp = client.post("/api/chat", json={"now": NOW, **body})
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
    return sse(resp)


def _kinds(events):
    out = []
    for k, _ in events:
        if not out or out[-1] != k:
            out.append(k)
    return out


def test_chat_done_sums_llm_usage_for_the_reply(client, service, db_conn):
    original = service.agent.handle

    def wrapped(*args, **kwargs):
        db_conn.execute(
            "INSERT INTO llm_calls(ts, purpose, masked_input, masked_output, status, latency_ms, "
            "prompt_tokens, completion_tokens, cache_hit_tokens, cache_miss_tokens) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (NOW, "narrate", "", "", "ok", 1, 120, 30, 80, 40),
        )
        db_conn.commit()
        return original(*args, **kwargs)

    service.agent.handle = wrapped
    try:
        done = chat(client, text="能不能分12期", role="customer", customer_id="C0003")[-1][1]
    finally:
        service.agent.handle = original
    assert done["usage"] == {"prompt_tokens": 120, "completion_tokens": 30, "cache_hit_tokens": 80}
    msgs = client.get(f"/api/sessions/{done['session_id']}").json()["messages"]
    ai = [m for m in msgs if m["role"] == "AI坐席"][-1]
    assert ai["meta"]["usage"] == done["usage"]
    assert chat(client, text="查询C0001的欠款")[-1][1]["usage"] is None


def test_chat_operator_query_streams_trace_delta_done(client):
    events = chat(client, text="查询C0001的欠款")
    assert _kinds(events) == ["trace", "delta", "done"]
    done = events[-1][1]
    assert done["speaker"] == "operator" and "O0033" in done["reply"]
    assert done["reply"] == "".join(d["text"] for k, d in events if k == "delta")
    assert "trace" not in done


def test_chat_customer_turn_enters_collection(client):
    done = chat(client, text="能不能分12期", role="customer", customer_id="C0003")[-1][1]
    assert done["mode"] == MODE_COLLECTION and done["system_intent"] == "分期方案协商" and done["session_id"]


def test_chat_handoff_and_finish(client):
    sid = chat(client, text="能不能分12期", role="customer", customer_id="C0003")[-1][1]["session_id"]
    done = chat(client, text="给我转人工", session_id=sid)[-1][1]
    assert done["mode"] == MODE_HANDOFF and done["handoff_package_id"]
    ok = chat(client, text="您好，我是人工坐席，您的情况我已了解。", session_id=sid, role="human_agent")[-1][1]
    assert ok["speaker"] == "agent" and ok["hit_rules"] == []
    resp = client.post(f"/api/sessions/{sid}/handoff/finish", json={"conclusion": "客户同意下月还款", "now": NOW})
    assert resp.status_code == 200
    assert client.post(f"/api/sessions/{sid}/end", json={}).status_code == 409


def test_chat_outbound_opening_not_narrated(client, service, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("开场白不应交给大模型")

    monkeypatch.setattr("api.service.narrate", boom)
    done = chat(client, text="对C0001发起催收")[-1][1]
    assert done["mode"] == MODE_COLLECTION and "工号AI01" in done["reply"]
    assert client.post(f"/api/sessions/{done['session_id']}/end", json={"now": NOW}).status_code == 200
    assert service.agent.cases.get_session(done["session_id"])["mode"] == MODE_OPERATOR


def _collection_session(client):
    sid = chat(client, text="能不能分12期", role="customer", customer_id="C0003")[-1][1]["session_id"]
    chat(client, text="给我转人工", session_id=sid)
    return sid


def test_customers_list_and_detail_masked(client):
    rows = client.get("/api/customers", params={"q": "C000"}).json()
    assert rows and all("*" in (r["customer_name"] or "*") for r in rows)
    detail = client.get("/api/customers/C0001").json()
    assert detail["overdues"]["items"] and detail["willingness"]["score"] >= 0
    assert detail["overdues"]["items"][0]["overdue_amount"] == "**"
    assert detail["overdues"]["total_amount"] == "**"
    assert rows[0]["overdue_amount"] == "**" or rows[0]["overdue_count"] == 0
    assert "*" in detail["profile"]["customer_name"] and detail["open_session_id"] is None
    assert client.get("/api/customers/C9999").status_code == 404


def test_cost_report_filters_by_time_and_sums_sessions(client):
    sid = chat(client, text="能不能分12期", role="customer", customer_id="C0003")[-1][1]["session_id"]
    client.app.state.service.agent.conn.execute(
        "INSERT INTO llm_calls(ts, purpose, masked_input, masked_output, status, latency_ms, session_id, model, "
        "prompt_tokens, completion_tokens, cache_hit_tokens, cache_miss_tokens) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("2026-10-24T09:00:00", "reply", "", "", "ok", 1, sid, "deepseek-flash", 1000, 200, 0, 1000),
    )
    client.app.state.service.agent.conn.execute(
        "INSERT INTO llm_calls(ts, purpose, masked_input, masked_output, status, latency_ms, session_id, model, "
        "prompt_tokens, completion_tokens, cache_hit_tokens, cache_miss_tokens) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("2026-09-01T09:00:00", "reply", "", "", "ok", 1, sid, "deepseek-flash", 5000, 0, 0, 5000),
    )
    client.app.state.service.agent.conn.commit()
    report = client.get("/api/costs", params={"start": "2026-10-01T00:00:00", "end": "2026-10-31T23:59:59"}).json()
    assert report["model"] == "deepseek-flash" and report["currency"] == "CNY"
    row = next(s for s in report["sessions"] if s["session_id"] == sid)
    assert row["customer_id"] == "C0003" and row["prompt_tokens"] == 1000 and row["completion_tokens"] == 200
    assert row["cost_cny"] == (1000 * 1 + 200 * 4) / 1_000_000
    assert report["summary"]["prompt_tokens"] == 1000


def test_session_list_resumable_only_while_live(client, service):
    sid = chat(client, text="能不能分12期", role="customer", customer_id="C0003")[-1][1]["session_id"]
    live = next(r for r in client.get("/api/sessions").json() if r["session_id"] == sid)
    assert live["resumable"] is True and live["customer_id"] == "C0003" and live["ended_at"] is None
    service.agent.agents.pop(sid)
    stale = next(r for r in client.get("/api/sessions").json() if r["session_id"] == sid)
    assert stale["resumable"] is False and stale["mode"] == MODE_COLLECTION
    client.post(f"/api/sessions/{sid}/end", json={"now": NOW})
    ended = next(r for r in client.get("/api/sessions").json() if r["session_id"] == sid)
    assert ended["resumable"] is False and ended["ended_at"]


def test_session_detail_and_case(client):
    sid = _collection_session(client)
    s = client.get(f"/api/sessions/{sid}").json()
    assert s["session"]["mode"] == MODE_HANDOFF and s["handoff"]["kind"] == "移交包"
    assert [m["role"] for m in s["messages"]][:2] == ["客户", "AI坐席"] and s["memory"]
    case_id = s["session"]["case_id"]
    assert any(c["case_id"] == case_id for c in client.get("/api/cases", params={"customer_id": "C0003"}).json())
    detail = client.get(f"/api/cases/{case_id}").json()
    assert detail["sessions"][0]["session_id"] == sid and detail["artifacts"]
    aid = detail["artifacts"][0]["artifact_id"]
    assert client.get(f"/api/artifacts/{aid}").json()["artifact_id"] == aid
    assert client.post(f"/api/cases/{case_id}/archive").json()["status"] == "已归档"


def test_postcall_status_and_retry(client):
    sid = _collection_session(client)
    client.post(f"/api/sessions/{sid}/handoff/finish", json={"conclusion": "已约定还款", "now": NOW})
    tasks = client.get(f"/api/sessions/{sid}").json()["postcall"]
    assert tasks and all(t["status"] == "完成" for t in tasks)
    again = client.post(f"/api/sessions/{sid}/postcall/retry", json={"name": "generate_report"}).json()
    assert next(t for t in again if t["name"] == "generate_report")["status"] == "完成"


def test_audit_query_and_export(client):
    chat(client, text="你今天不还就上门找你")
    rows = client.get("/api/audit", params={"rule_id": "R002"}).json()
    assert rows and rows[0]["checkpoint"] == "坐席话术"
    resp = client.get("/api/audit/export")
    assert resp.status_code == 200 and "日志编号" in resp.content.decode("utf-8-sig")


def test_memory_correct_and_clear_write_audit(client):
    sid = _collection_session(client)
    key = next(iter(client.get(f"/api/memory/session/{sid}").json()))
    assert client.put(f"/api/memory/session/{sid}/{key}", json={"value": "人工修正"}).status_code == 200
    assert client.get(f"/api/memory/session/{sid}").json()[key]["value"] == "人工修正"
    assert client.delete(f"/api/memory/session/{sid}/{key}").status_code == 200
    assert key not in client.get(f"/api/memory/session/{sid}").json()
    assert client.delete(f"/api/memory/session/{sid}/{key}").status_code == 404
    scenes = [r["scene"] for r in client.get("/api/audit", params={"checkpoint": "记忆操作"}).json()]
    assert "纠正" in scenes and "清除" in scenes


def test_config_read_save_and_versions(client):
    briefs = client.get("/api/config").json()
    assert {"policy", "checkpoints", "business_pack"} <= {b["kind"] for b in briefs}
    doc = client.get("/api/config/policy/default").json()
    doc["content"]["installment"]["affordability_ratio"] = 0.35
    saved = client.put("/api/config/policy/default", json={"content": doc["content"]}).json()
    assert saved["version"] != doc["version"]
    versions = client.get("/api/config/policy/default/versions").json()
    assert versions[0]["version"] == saved["version"] and len(versions) >= 2
    assert client.get("/api/config/nope/default").status_code == 404


def test_business_pack_validation_and_copy(client):
    pack = client.get("/api/config/business_pack/consumer_finance").json()["content"]
    pack["pre_tools"].append({"tool": "no_such_tool", "on_failure": "block"})
    resp = client.put("/api/config/business_pack/consumer_finance", json={"content": pack})
    assert resp.status_code == 422 and any("no_such_tool" in e for e in resp.json()["detail"]["errors"])
    copied = client.post("/api/config/business_pack/consumer_finance/copy",
                         json={"new_key": "car_insurance", "name": "车险续保"}).json()
    assert copied["content"]["pack_id"] == "car_insurance" and copied["content"]["match"]["default"] is False
    assert any(b["key"] == "car_insurance" for b in client.get("/api/config").json())
    assert client.post("/api/config/business_pack/consumer_finance/copy",
                       json={"new_key": "car_insurance", "name": "重复"}).status_code == 409


def test_registry_config_save_applies_and_rejects_unknown(client):
    doc = client.get("/api/config/registry/default").json()
    names = {i["name"] for i in doc["content"]["items"]}
    assert "get_overdues" in names and "installment_plan" in names
    edited = doc["content"]
    row = next(i for i in edited["items"] if i["name"] == "get_overdues")
    row["timeout_s"] = 9
    row["description"] = "逾期优先级（测试）"
    saved = client.put("/api/config/registry/default", json={"content": edited}).json()
    assert saved["version"] != doc["version"]
    groups = {g["owner"]: g["items"] for g in client.get("/api/registry").json()}
    item = next(i for i in groups["ops"] if i["name"] == "get_overdues")
    assert item["timeout_s"] == 9 and item["description"] == "逾期优先级（测试）" and item["enabled"] is True
    edited["items"].append({"name": "no_such", "owner": "ops", "kind": "tool", "description": "x",
                            "timeout_s": 1, "retries": 0, "enabled": True})
    resp = client.put("/api/config/registry/default", json={"content": edited})
    assert resp.status_code == 422 and any("no_such" in e for e in resp.json()["detail"]["errors"])


def test_edit_rule_and_template_overlay_raw_rows(client, service, db_conn):
    rule = next(r for r in client.get("/api/rules").json() if r["rule_id"] == "R001")
    original = rule["rule_content"]
    saved = client.put("/api/rules/R001", json={
        "rule_type": rule["rule_type"], "rule_content": "测试改写后的规则内容",
        "violation_level": "严重", "action": rule["action"],
    })
    assert saved.status_code == 200 and saved.json()["rule_content"] == "测试改写后的规则内容"
    listed = next(r for r in client.get("/api/rules").json() if r["rule_id"] == "R001")
    assert listed["rule_content"] == "测试改写后的规则内容" and listed["checkpoints"] == rule["checkpoints"]
    rows = db_conn.execute("SELECT source, rule_content FROM dim_compliance_rule WHERE rule_id='R001'").fetchall()
    assert any(r["source"] == "原始导入" and r["rule_content"] == original for r in rows)
    assert sum(r["source"] == "系统生成" for r in rows) == 1
    assert service.agent.engine.rules["R001"]["rule_content"] == "测试改写后的规则内容"
    again = client.put("/api/rules/R001", json={
        "rule_type": rule["rule_type"], "rule_content": "第二次改写",
        "violation_level": "轻微", "action": rule["action"],
    })
    assert again.status_code == 200
    rows = db_conn.execute("SELECT source FROM dim_compliance_rule WHERE rule_id='R001'").fetchall()
    assert sum(r["source"] == "系统生成" for r in rows) == 1
    bad = client.put("/api/rules/R001", json={
        "rule_type": rule["rule_type"], "rule_content": "x", "violation_level": "特别严重", "action": rule["action"],
    })
    assert bad.status_code == 422 and any("级别" in e for e in bad.json()["detail"]["errors"])
    assert client.put("/api/rules/R999", json={
        "rule_type": "自定义", "rule_content": "不存在", "violation_level": "轻微", "action": "记录",
    }).status_code == 404
    created = client.post("/api/rules", json={
        "rule_id": "R099", "rule_type": "自定义", "rule_content": "新增规则",
        "violation_level": "轻微", "action": "记录",
    })
    assert created.status_code == 200 and created.json()["rule_id"] == "R099"
    assert client.post("/api/rules", json={
        "rule_id": "R099", "rule_type": "自定义", "rule_content": "新增规则",
        "violation_level": "轻微", "action": "记录",
    }).status_code == 409

    template = client.get("/api/templates").json()[0]
    tid = template["template_id"]
    saved_t = client.put(f"/api/templates/{tid}", json={
        "template_type": template["template_type"], "max_periods": 24,
        "rate": 0.08, "fee": template["fee"], "condition": "逾期≤10天",
    })
    assert saved_t.status_code == 200
    body = saved_t.json()
    assert body["max_periods"] == 24 and body["cond"]["max_days"] == 10 and body["rate"] == 0.08
    again_t = [t for t in client.get("/api/templates").json() if t["template_id"] == tid]
    assert len(again_t) == 1 and again_t[0]["rate"] == 0.08
    raw = db_conn.execute(
        "SELECT source, max_periods FROM dim_installment_template WHERE template_id=?", (tid,)).fetchall()
    assert any(r["source"] == "原始导入" and r["max_periods"] != 24 for r in raw)


def test_readonly_rules_templates_registry(client):
    rules = client.get("/api/rules").json()
    r004 = next(r for r in rules if r["rule_id"] == "R004")
    assert r004["checkpoints"] == ["运营操作"]
    templates = client.get("/api/templates").json()
    assert templates and "max_days" in templates[0]["cond"]
    groups = {g["owner"]: g["items"] for g in client.get("/api/registry").json()}
    assert "get_overdues" not in [i["name"] for i in groups["main"]]
    assert "get_overdues" in [i["name"] for i in groups["ops"]]
    assert "installment_plan" in [i["name"] for i in groups["collection:consumer_finance"]]
    assert "get_overdues" not in [i["name"] for i in groups["collection:consumer_finance"]]


def test_spa_fallback(service, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>app</html>", encoding="utf-8")
    (dist / "assets" / "a.js").write_text("console.log(1)", encoding="utf-8")
    c = TestClient(create_app(service, web_dist=dist))
    assert c.get("/cases").text == "<html>app</html>"
    assert c.get("/assets/a.js").text == "console.log(1)"
    assert c.get("/api/nope").status_code == 404
    assert c.get("/api/customers").status_code == 200


def test_request_trace_id_reaches_done_and_logs(client, caplog):
    caplog.set_level(logging.INFO, logger="cagent")
    resp = client.post("/api/chat", json={"now": NOW, "text": "查询C0001的欠款"},
                       headers={"X-Trace-Id": "TR-abc1234567"})
    assert resp.status_code == 200
    assert resp.headers["x-trace-id"] == "TR-abc1234567"
    done = sse(resp)[-1][1]
    assert done["trace_id"] == "TR-abc1234567"
    assert any(getattr(record, "trace_id", None) == "TR-abc1234567" and "/api/chat" in record.message
               for record in caplog.records)
    other = client.get("/api/sessions", headers={"X-Trace-Id": "not-a-trace"})
    assert other.headers["x-trace-id"].startswith("TR-") and other.headers["x-trace-id"] != "not-a-trace"


def test_chat_error_event(client, service, monkeypatch):
    def fail(*a, **k):
        raise RuntimeError("模拟故障")

    monkeypatch.setattr(service.agent, "handle", fail)
    events = chat(client, text="查询C0001的欠款")
    assert events == [("error", {"message": "RuntimeError: 模拟故障"})]
