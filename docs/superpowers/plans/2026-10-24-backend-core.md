# 催收 Agent 后端核心与命令行测评 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现规格 `docs/superpowers/specs/2026-10-24-collection-agent-design.md` 中除接口与前端以外的全部后端能力，并能用命令行批量跑出公开测评集结果、合规审计日志样例和催收报告样例。

**Architecture:** Python 包 `cagent`（位于 `backend/`）。SQLite 存放 9 张原始表（带来源字段）与运行时表；合规引擎、脱敏网关、审计为共用设施；主控 Agent 与业务子 Agent 各自持有独立的 `Registry`（工具 + Skill）；业务包、检查点、评分策略、意图映射均为 YAML 配置并在 SQLite 中记录版本。大模型只用于说话人判定、客户理解、话术生成、语义合规与改写建议，全部有关键词或模板兜底，无 API Key 时系统以降级模式完整运行。

**Tech Stack:** Python 3.11+（本机 3.12）、sqlite3（标准库）、pydantic 2、PyYAML、openai SDK（调用 DeepSeek `deepseek-flash`，OpenAI 兼容接口）、pytest。

## Global Constraints

- 所有源码 UTF-8 无 BOM；读取赛题 CSV 用 `utf-8-sig`；导出 CSV 用 `utf-8-sig`。
- 公开测评集标准答案（`公开测试集答案.csv` 的 `reference_reply`、`reference_key_points`）不得出现在提示词、配置文件、代码常量与测试断言中，只允许 `scripts/compare_eval.py` 在运行时读取。
- 原始导入行只读（数据库触发器保证）；系统写入行 `source='系统生成'`。
- 调用大模型前必须经过 `GuardedLLM`（脱敏 + 调用日志），业务代码不得直接使用 `DeepSeekClient`。
- 大模型不可用时任何流程不得崩溃，必须走兜底并在结果中标注需人工复核。
- 发给客户的每一句话必须经过检查点一并写审计日志。
- 子 Agent 的工具不接受 `customer_id` 参数，客户编号由构造时注入。
- 所有命令在 `backend/` 目录下执行（Windows PowerShell）。

## 文件结构

```
requirements.txt                 依赖锁定
.gitignore
README.md                        初始化、测试、批量测评命令
backend/
  pytest.ini
  config/
    llm.yaml                     模型名、接口地址、超时
    checkpoints.yaml             检测器 + 三个检查点
    policy.yaml                  评分权重、策略表、分期参数、优先级排序
    intents.yaml                 说话人标记、客户意图、运营意图、意图映射
    main_agent.yaml              主控默认业务包等
    business_packs/
      consumer_finance.yaml      通用消费金融催收（默认）
      auto_loan.yaml             车贷催收（只改配置演示适配）
  cagent/
    __init__.py
    settings.py                  路径与环境变量
    db.py                        连接、建表、只读触发器、写入系统生成行
    importer.py                  CSV 导入、数据质量检查、重置
    clock.py                     题面时间解析、禁呼时段计算
    trace.py                     处理轨迹
    registry.py                  工具与 Skill 注册表（校验、超时、重试）
    config_center.py             YAML 加载、版本、业务包校验
    desensitize.py               脱敏与展示脱敏
    llm.py                       DeepSeekClient / FakeLLM / GuardedLLM
    compliance.py                规则库、检测器、三个检查点
    audit.py                     审计写入与导出
    scoring.py                   还款意愿评分与策略
    installment.py               分期模板解析与方案测算
    loans.py                     多笔逾期优先级
    memory.py                    会话与客户记忆
    cases.py                     案件、会话、消息、产出物、待办
    reports.py                   单次报告、客户报告、汇总报告
    postcall.py                  后置任务与执行器
    agents/
      __init__.py
      understanding.py           预处理、说话人判定、客户理解、运营意图
      collection.py              业务子 Agent
      main.py                    主控 Agent
  scripts/
    init_db.py                   导入或重置数据库
    llm_smoke.py                 大模型连通性冒烟
    run_eval.py                  批量测评
    compare_eval.py              与标准答案对照
    redline_eval.py              红线专项统计
  tests/
    conftest.py
    data/redline_cases.yaml
    test_importer.py  test_core_utils.py  test_config_center.py  test_llm.py
    test_compliance.py  test_scoring.py  test_installment.py  test_store.py
    test_understanding.py  test_collection.py  test_reports_postcall.py  test_main_agent.py
    test_scripts.py
```

---

### Task 1: 项目骨架、数据库与 CSV 导入

**Files:**
- Create: `requirements.txt`, `.gitignore`, `backend/pytest.ini`, `backend/cagent/__init__.py`, `backend/cagent/settings.py`, `backend/cagent/db.py`, `backend/cagent/importer.py`, `backend/scripts/init_db.py`, `backend/tests/conftest.py`
- Test: `backend/tests/test_importer.py`

**Interfaces:**
- Produces:
  - `db.RAW = "原始导入"`, `db.GENERATED = "系统生成"`, `db.BUSINESS_TABLES: dict[str, list[tuple[str, str]]]`
  - `db.connect(path) -> sqlite3.Connection`（`row_factory=sqlite3.Row`）
  - `db.init_runtime(conn) -> None`
  - `db.insert_generated(conn, table: str, row: dict, session_id: str | None) -> None`（不提交事务）
  - `importer.import_all(conn, data_dir: Path) -> dict`（`{"counts": {...}, "issues": int}`）
  - `importer.reset_generated(conn) -> None`
  - 运行时表：`cases, sessions, messages, memory, artifacts, audit_log, llm_calls, todos, post_tasks, profile_snapshots, config_versions, data_quality`
  - 测试夹具：`db_path`（已导入的临时库路径）、`db_conn`

- [ ] **Step 1: 建立骨架文件**

`requirements.txt`：

```text
openai==1.55.3
pydantic==2.9.2
PyYAML==6.0.2
pytest==8.3.3
```

`.gitignore`：

```text
__pycache__/
*.pyc
.venv/
backend/runtime/
backend/output/
node_modules/
.env
```

`backend/pytest.ini`：

```ini
[pytest]
testpaths = tests
pythonpath = .
```

`backend/cagent/__init__.py`：空文件。

`backend/cagent/settings.py`：

```python
import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent
DATA_DIR = Path(os.getenv("CAGENT_DATA_DIR", REPO_DIR / "赛题" / "赛题08_金融催收对话引擎" / "data"))
CONFIG_DIR = Path(os.getenv("CAGENT_CONFIG_DIR", BACKEND_DIR / "config"))
DB_PATH = Path(os.getenv("CAGENT_DB_PATH", BACKEND_DIR / "runtime" / "collection.db"))
OUTPUT_DIR = Path(os.getenv("CAGENT_OUTPUT_DIR", BACKEND_DIR / "output"))
```

安装依赖：

```powershell
cd D:\Document\26HackSong
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

- [ ] **Step 2: 写失败测试**

`backend/tests/conftest.py`：

```python
import pytest

from cagent import db, importer, settings


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    conn = db.connect(path)
    importer.import_all(conn, settings.DATA_DIR)
    conn.close()
    return path


@pytest.fixture
def db_conn(db_path):
    conn = db.connect(db_path)
    yield conn
    conn.close()
```

`backend/tests/test_importer.py`：

```python
import sqlite3

import pytest

from cagent import db, importer


def count(conn, table, where="1=1"):
    return conn.execute(f'SELECT COUNT(*) FROM "{table}" WHERE {where}').fetchone()[0]


def test_import_counts(db_conn):
    assert count(db_conn, "dim_customer") == 10
    assert count(db_conn, "dim_loan") == 30
    assert count(db_conn, "fact_overdue") == 40
    assert count(db_conn, "fact_call_history") == 50
    assert count(db_conn, "dim_compliance_rule") == 10


def test_imported_rows_marked_raw(db_conn):
    assert count(db_conn, "fact_overdue", f"source='{db.RAW}'") == 40


def test_quality_reports_loan_owner_mismatch(db_conn):
    assert count(db_conn, "data_quality", "issue LIKE '%不一致%'") == 36


def test_raw_rows_are_read_only(db_conn):
    with pytest.raises(sqlite3.DatabaseError):
        db_conn.execute("UPDATE dim_customer SET age=1 WHERE customer_id='C0001'")
    with pytest.raises(sqlite3.DatabaseError):
        db_conn.execute("DELETE FROM fact_overdue WHERE overdue_id='O0001'")


def test_generated_rows_can_be_reset(db_conn):
    db.insert_generated(db_conn, "fact_call_history", {
        "call_id": "CH-T1", "customer_id": "C0001", "call_time": "2026-10-24 10:00",
        "call_method": "AI对话", "talk_summary": "测试", "call_result": "已沟通", "agent_id": "AI01",
    }, session_id="S-T1")
    db_conn.commit()
    assert count(db_conn, "fact_call_history") == 51
    importer.reset_generated(db_conn)
    assert count(db_conn, "fact_call_history") == 50


def test_insert_generated_rejects_unknown_column(db_conn):
    with pytest.raises(ValueError):
        db.insert_generated(db_conn, "fact_call_history", {"bad": 1}, session_id=None)
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd backend; python -m pytest tests/test_importer.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'cagent.db'`

- [ ] **Step 4: 实现 `db.py`**

`backend/cagent/db.py`：

```python
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

RAW = "原始导入"
GENERATED = "系统生成"

BUSINESS_TABLES: dict[str, list[tuple[str, str]]] = {
    "dim_customer": [("customer_id", "TEXT"), ("customer_name", "TEXT"), ("age", "INTEGER"),
                     ("occupation", "TEXT"), ("monthly_income", "REAL"), ("credit_score", "INTEGER"),
                     ("region", "TEXT"), ("customer_level", "TEXT")],
    "dim_loan": [("loan_id", "TEXT"), ("customer_id", "TEXT"), ("loan_type", "TEXT"), ("principal", "REAL"),
                 ("annual_rate", "REAL"), ("periods", "INTEGER"), ("issue_date", "TEXT"), ("due_date", "TEXT")],
    "dim_compliance_rule": [("rule_id", "TEXT"), ("rule_type", "TEXT"), ("rule_content", "TEXT"),
                            ("violation_level", "TEXT"), ("action", "TEXT")],
    "dim_installment_template": [("template_id", "TEXT"), ("template_type", "TEXT"), ("max_periods", "INTEGER"),
                                 ("rate", "REAL"), ("fee", "REAL"), ("condition", "TEXT")],
    "fact_overdue": [("overdue_id", "TEXT"), ("customer_id", "TEXT"), ("loan_id", "TEXT"),
                     ("overdue_amount", "REAL"), ("overdue_days", "INTEGER"), ("overdue_periods", "INTEGER"),
                     ("last_repay_date", "TEXT"), ("recommended_template_id", "TEXT")],
    "fact_call_history": [("call_id", "TEXT"), ("customer_id", "TEXT"), ("call_time", "TEXT"),
                          ("call_method", "TEXT"), ("talk_summary", "TEXT"), ("call_result", "TEXT"),
                          ("agent_id", "TEXT"), ("violated_rule_id", "TEXT")],
    "fact_repayment": [("repay_id", "TEXT"), ("customer_id", "TEXT"), ("repay_amount", "REAL"),
                       ("repay_time", "TEXT"), ("repay_method", "TEXT"), ("is_on_time", "TEXT")],
    "fact_user_behavior": [("behavior_id", "TEXT"), ("customer_id", "TEXT"), ("behavior_type", "TEXT"),
                           ("behavior_time", "TEXT"), ("behavior_detail", "TEXT")],
    "fact_emotion_log": [("emotion_id", "TEXT"), ("customer_id", "TEXT"), ("emotion_type", "TEXT"),
                         ("emotion_intensity", "REAL"), ("trigger_talk", "TEXT"), ("record_time", "TEXT")],
}

META_COLUMNS = ["source", "session_id", "written_at"]

RUNTIME_DDL = """
CREATE TABLE IF NOT EXISTS cases(case_id TEXT PRIMARY KEY, customer_id TEXT, pack_id TEXT, status TEXT,
  created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS sessions(session_id TEXT PRIMARY KEY, case_id TEXT, customer_id TEXT, mode TEXT,
  pack_id TEXT, config_version TEXT, channel TEXT, started_at TEXT, ended_at TEXT, end_reason TEXT);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, turn INTEGER,
  role TEXT, text TEXT, meta TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS memory(id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT, owner_id TEXT, key TEXT,
  value TEXT, source TEXT, status TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS artifacts(artifact_id TEXT PRIMARY KEY, case_id TEXT, session_id TEXT, kind TEXT,
  content TEXT, created_at TEXT, archived INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS audit_log(log_id TEXT PRIMARY KEY, ts TEXT, customer_id TEXT, checkpoint TEXT,
  scene TEXT, content TEXT, rule_id TEXT, rule_type TEXT, level TEXT, action TEXT, handoff INTEGER,
  config_version TEXT, session_id TEXT);
CREATE TABLE IF NOT EXISTS llm_calls(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, purpose TEXT,
  masked_input TEXT, masked_output TEXT, status TEXT, latency_ms INTEGER);
CREATE TABLE IF NOT EXISTS todos(todo_id TEXT PRIMARY KEY, case_id TEXT, session_id TEXT, kind TEXT,
  content TEXT, due TEXT, status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS post_tasks(session_id TEXT, name TEXT, stage INTEGER, status TEXT, attempts INTEGER,
  error TEXT, updated_at TEXT, PRIMARY KEY(session_id, name));
CREATE TABLE IF NOT EXISTS profile_snapshots(customer_id TEXT, version INTEGER, content TEXT, session_id TEXT,
  created_at TEXT, PRIMARY KEY(customer_id, version));
CREATE TABLE IF NOT EXISTS config_versions(kind TEXT, key TEXT, version TEXT, content TEXT, created_at TEXT,
  PRIMARY KEY(kind, key, version));
CREATE TABLE IF NOT EXISTS data_quality(issue_id INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT,
  record_id TEXT, customer_id TEXT, issue TEXT);
"""

RUNTIME_TABLES = ["cases", "sessions", "messages", "memory", "artifacts", "audit_log", "llm_calls", "todos",
                  "post_tasks", "profile_snapshots"]


def q(name: str) -> str:
    return f'"{name}"'


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_runtime(conn: sqlite3.Connection) -> None:
    conn.executescript(RUNTIME_DDL)
    conn.commit()


def create_business_table(conn: sqlite3.Connection, table: str) -> None:
    cols = ", ".join(f"{q(c)} {t}" for c, t in BUSINESS_TABLES[table])
    conn.execute(f"DROP TABLE IF EXISTS {q(table)}")
    conn.execute(
        f"CREATE TABLE {q(table)} ({cols}, \"source\" TEXT NOT NULL DEFAULT '{GENERATED}', "
        f"\"session_id\" TEXT, \"written_at\" TEXT)"
    )
    for op in ("UPDATE", "DELETE"):
        conn.execute(
            f"CREATE TRIGGER {q(table + '_raw_no_' + op.lower())} BEFORE {op} ON {q(table)} "
            f"WHEN OLD.source = '{RAW}' BEGIN SELECT RAISE(ABORT, '原始数据只读'); END"
        )


def insert_generated(conn: sqlite3.Connection, table: str, row: dict, session_id: str | None) -> None:
    cols = [c for c, _ in BUSINESS_TABLES[table]]
    unknown = set(row) - set(cols)
    if unknown:
        raise ValueError(f"{table} 不存在字段: {sorted(unknown)}")
    names = cols + META_COLUMNS
    values = [row.get(c) for c in cols] + [GENERATED, session_id, now_iso()]
    conn.execute(
        f"INSERT INTO {q(table)} ({', '.join(q(n) for n in names)}) VALUES ({', '.join('?' * len(names))})",
        values,
    )
```

- [ ] **Step 5: 实现 `importer.py`**

`backend/cagent/importer.py`：

```python
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from . import db

FK_CHECKS = [
    ("fact_overdue", "overdue_id", "customer_id", "dim_customer", "customer_id"),
    ("fact_overdue", "overdue_id", "loan_id", "dim_loan", "loan_id"),
    ("fact_overdue", "overdue_id", "recommended_template_id", "dim_installment_template", "template_id"),
    ("fact_call_history", "call_id", "customer_id", "dim_customer", "customer_id"),
    ("fact_call_history", "call_id", "violated_rule_id", "dim_compliance_rule", "rule_id"),
    ("fact_repayment", "repay_id", "customer_id", "dim_customer", "customer_id"),
    ("fact_user_behavior", "behavior_id", "customer_id", "dim_customer", "customer_id"),
    ("fact_emotion_log", "emotion_id", "customer_id", "dim_customer", "customer_id"),
    ("dim_loan", "loan_id", "customer_id", "dim_customer", "customer_id"),
]


def _cast(value: str | None, typ: str):
    if value is None or value.strip() == "":
        return None
    if typ == "INTEGER":
        return int(float(value))
    if typ == "REAL":
        return float(value)
    return value.strip()


def import_all(conn: sqlite3.Connection, data_dir: Path) -> dict:
    db.init_runtime(conn)
    counts: dict[str, int] = {}
    stamp = db.now_iso()
    for table, cols in db.BUSINESS_TABLES.items():
        db.create_business_table(conn, table)
        path = Path(data_dir) / f"{table}.csv"
        with path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            missing = [c for c, _ in cols if c not in (reader.fieldnames or [])]
            if missing:
                raise ValueError(f"{path.name} 缺少列: {missing}")
            records = [tuple(_cast(r[c], t) for c, t in cols) + (db.RAW, None, stamp) for r in reader]
        names = [c for c, _ in cols] + db.META_COLUMNS
        conn.executemany(
            f"INSERT INTO {db.q(table)} ({', '.join(db.q(n) for n in names)}) VALUES ({', '.join('?' * len(names))})",
            records,
        )
        counts[table] = len(records)
    issues = check_quality(conn)
    conn.execute("DELETE FROM data_quality")
    conn.executemany(
        "INSERT INTO data_quality(table_name, record_id, customer_id, issue) VALUES (?,?,?,?)",
        [(i["table"], i["record_id"], i["customer_id"], i["issue"]) for i in issues],
    )
    conn.commit()
    return {"counts": counts, "issues": len(issues)}


def check_quality(conn: sqlite3.Connection) -> list[dict]:
    issues: list[dict] = []
    for table, pk, fk, ref, ref_key in FK_CHECKS:
        sql = (f'SELECT t."{pk}" AS rid, t."{fk}" AS fk, t."customer_id" AS cid FROM "{table}" t '
               f'WHERE t."{fk}" IS NOT NULL AND t."{fk}" NOT IN (SELECT "{ref_key}" FROM "{ref}")')
        for r in conn.execute(sql):
            issues.append({"table": table, "record_id": r["rid"], "customer_id": r["cid"],
                           "issue": f"{fk}={r['fk']} 在 {ref} 中不存在"})
    sql = ("SELECT o.overdue_id, o.customer_id, o.loan_id, l.customer_id AS owner FROM fact_overdue o "
           "JOIN dim_loan l ON l.loan_id = o.loan_id WHERE l.customer_id != o.customer_id")
    for r in conn.execute(sql):
        issues.append({"table": "fact_overdue", "record_id": r["overdue_id"], "customer_id": r["customer_id"],
                       "issue": f"逾期记录的贷款 {r['loan_id']} 在贷款表中属于 {r['owner']}，与逾期客户 {r['customer_id']} 不一致"})
    return issues


def reset_generated(conn: sqlite3.Connection) -> None:
    for table in db.BUSINESS_TABLES:
        conn.execute(f"DELETE FROM {db.q(table)} WHERE source = ?", (db.GENERATED,))
    for table in db.RUNTIME_TABLES:
        conn.execute(f"DELETE FROM {db.q(table)}")
    conn.commit()
```

`backend/scripts/init_db.py`：

```python
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cagent import db, importer, settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="导入赛题数据或清空系统生成数据")
    parser.add_argument("--reset", action="store_true", help="只删除系统生成数据与运行时记录")
    args = parser.parse_args()
    conn = db.connect(settings.DB_PATH)
    if args.reset:
        importer.reset_generated(conn)
        print("已清空系统生成数据")
    else:
        result = importer.import_all(conn, settings.DATA_DIR)
        print(f"导入完成：{result['counts']}；数据质量问题 {result['issues']} 条；数据库 {settings.DB_PATH}")
    conn.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: 运行测试确认通过**

Run: `python -m pytest tests/test_importer.py -v`
Expected: 6 passed

Run: `python scripts/init_db.py`
Expected: 输出 `导入完成：{...}；数据质量问题 36 条` 或更多（外键缺失也会计入）

- [ ] **Step 7: Commit**

```powershell
cd D:\Document\26HackSong
git add requirements.txt .gitignore backend
git commit -m "feat: sqlite schema, csv import with read-only raw rows and data quality report"
```

---

### Task 2: 时间工具、处理轨迹、工具与 Skill 注册表

**Files:**
- Create: `backend/cagent/clock.py`, `backend/cagent/trace.py`, `backend/cagent/registry.py`
- Test: `backend/tests/test_core_utils.py`

**Interfaces:**
- Produces:
  - `clock.parse_time_hint(text: str, base: datetime) -> datetime | None`（只识别“现在/当前/此刻/目前”后的时间）
  - `clock.cn_to_int(s: str) -> int`
  - `clock.in_quiet_hours(now: datetime, start: str, end: str) -> bool`
  - `clock.next_allowed_time(now: datetime, start: str, end: str) -> datetime`
  - `trace.Trace(sink=None)`，属性 `trace_id: str`、`events: list[TraceEvent]`；方法 `add(step, name, status="ok", ms=0, **detail) -> TraceEvent`、`to_list() -> list[dict]`
  - `trace.jsonable(value) -> Any`
  - `registry.CallError(name, reason, retryable=True)`，属性 `.reason`、`.retryable`
  - `registry.Registry(owner: str)`：`register(name, fn, input_model, output_model, *, kind, description, timeout_s=5.0, retries=1)`、`call(name, trace, **kwargs) -> BaseModel`、`has(name)`、`names(kind=None)`、`describe() -> list[dict]`。`fn` 接收输入模型实例，返回输出模型实例或 dict

- [ ] **Step 1: 写失败测试**

`backend/tests/test_core_utils.py`：

```python
import time
from datetime import datetime

import pytest
from pydantic import BaseModel

from cagent.clock import in_quiet_hours, next_allowed_time, parse_time_hint
from cagent.registry import CallError, Registry
from cagent.trace import Trace

BASE = datetime(2026, 10, 24, 10, 0)


def test_parse_evening_digits():
    assert parse_time_hint("现在晚上11点了，可以打电话催收吗？", BASE) == datetime(2026, 10, 24, 23, 0)


def test_parse_colon_time():
    assert parse_time_hint("当前 22:31 能外呼吗", BASE) == datetime(2026, 10, 24, 22, 31)


def test_parse_chinese_numeral_half():
    assert parse_time_hint("现在是晚上十一点半", BASE) == datetime(2026, 10, 24, 23, 30)


def test_no_hint_without_now_word():
    assert parse_time_hint("我3点前还款", BASE) is None


@pytest.mark.parametrize("hour,minute,expected", [(23, 0, True), (7, 59, True), (8, 0, False), (21, 59, False), (22, 0, True)])
def test_quiet_hours(hour, minute, expected):
    assert in_quiet_hours(datetime(2026, 10, 24, hour, minute), "22:00", "08:00") is expected


def test_next_allowed_after_evening():
    assert next_allowed_time(datetime(2026, 10, 24, 23, 0), "22:00", "08:00") == datetime(2026, 10, 25, 8, 0)


def test_next_allowed_early_morning():
    assert next_allowed_time(datetime(2026, 10, 24, 3, 0), "22:00", "08:00") == datetime(2026, 10, 24, 8, 0)


class In(BaseModel):
    x: int


class Out(BaseModel):
    y: int


def test_registry_call_ok():
    reg = Registry("test")
    reg.register("double", lambda a: {"y": a.x * 2}, In, Out, kind="tool", description="翻倍")
    tr = Trace()
    assert reg.call("double", tr, x=2).y == 4
    assert tr.events[-1].status == "ok"
    assert tr.events[-1].detail["output"] == {"y": 4}


def test_registry_validation_error_not_retried():
    calls = []
    reg = Registry("test")
    reg.register("double", lambda a: calls.append(1) or {"y": 1}, In, Out, kind="tool", description="d")
    with pytest.raises(CallError) as e:
        reg.call("double", Trace(), x="abc")
    assert e.value.retryable is False
    assert calls == []


def test_registry_timeout_then_retry_succeeds():
    calls = []

    def fn(a):
        calls.append(1)
        if len(calls) == 1:
            time.sleep(0.5)
        return {"y": 1}

    reg = Registry("test")
    reg.register("slow", fn, In, Out, kind="tool", description="d", timeout_s=0.1, retries=1)
    tr = Trace()
    assert reg.call("slow", tr, x=1).y == 1
    assert [e.status for e in tr.events] == ["retry", "ok"]


def test_registry_exhausted_raises():
    calls = []

    def fn(a):
        calls.append(1)
        raise RuntimeError("boom")

    reg = Registry("test")
    reg.register("bad", fn, In, Out, kind="skill", description="d", retries=1)
    with pytest.raises(CallError) as e:
        reg.call("bad", Trace(), x=1)
    assert "boom" in e.value.reason
    assert len(calls) == 2


def test_registry_rejects_duplicate_and_describes():
    reg = Registry("owner-a")
    reg.register("t", lambda a: {"y": 1}, In, Out, kind="tool", description="d")
    with pytest.raises(ValueError):
        reg.register("t", lambda a: {"y": 1}, In, Out, kind="tool", description="d")
    info = reg.describe()[0]
    assert info["owner"] == "owner-a" and info["kind"] == "tool" and "properties" in info["input"]


def test_trace_sink_receives_events():
    got = []
    tr = Trace(sink=got.append)
    tr.add("route", "说话人判定", speaker="customer")
    assert got[0].detail == {"speaker": "customer"}
    assert tr.to_list()[0]["name"] == "说话人判定"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_core_utils.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'cagent.clock'`

- [ ] **Step 3: 实现 `clock.py`**

```python
from __future__ import annotations

import re
from datetime import datetime, time, timedelta

_CN = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

_PATTERN = re.compile(
    r"(?:现在|当前|此刻|目前)[^，。！？,.!?\d零一二两三四五六七八九十]{0,4}?"
    r"(凌晨|早上|上午|中午|下午|傍晚|晚上|夜里|半夜)?\s*"
    r"([0-9]{1,2}|[零一二两三四五六七八九十]{1,3})\s*[点:：时]\s*(半|[0-9]{1,2})?"
)


def cn_to_int(s: str) -> int:
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + _CN[s[1]]
    if s.endswith("十"):
        return _CN[s[0]] * 10
    if "十" in s:
        a, b = s.split("十")
        return _CN[a] * 10 + _CN[b]
    return _CN[s]


def parse_time_hint(text: str, base: datetime) -> datetime | None:
    m = _PATTERN.search(text or "")
    if not m:
        return None
    period, hour_s, minute_s = m.groups()
    hour = cn_to_int(hour_s)
    minute = 30 if minute_s == "半" else int(minute_s) if minute_s else 0
    if period in ("下午", "傍晚", "晚上", "夜里") and hour < 12:
        hour += 12
    if period == "中午" and hour < 11:
        hour += 12
    if period == "半夜" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _t(s: str) -> time:
    return time.fromisoformat(s)


def in_quiet_hours(now: datetime, start: str, end: str) -> bool:
    s, e, t = _t(start), _t(end), now.time()
    if s > e:
        return t >= s or t < e
    return s <= t < e


def next_allowed_time(now: datetime, start: str, end: str) -> datetime:
    if not in_quiet_hours(now, start, end):
        return now
    e = _t(end)
    candidate = now.replace(hour=e.hour, minute=e.minute, second=0, microsecond=0)
    if now.time() >= _t(start):
        candidate += timedelta(days=1)
    return candidate
```

- [ ] **Step 4: 实现 `trace.py`**

```python
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from pydantic import BaseModel


def jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


@dataclass
class TraceEvent:
    step: str
    name: str
    status: str
    detail: dict[str, Any] = field(default_factory=dict)
    ms: int = 0


class Trace:
    def __init__(self, sink: Callable[[TraceEvent], None] | None = None, trace_id: str | None = None):
        self.trace_id = trace_id or "TR-" + uuid.uuid4().hex[:10]
        self.events: list[TraceEvent] = []
        self._sink = sink

    def add(self, step: str, name: str, status: str = "ok", ms: int = 0, **detail: Any) -> TraceEvent:
        event = TraceEvent(step, name, status, jsonable(detail), ms)
        self.events.append(event)
        if self._sink:
            self._sink(event)
        return event

    def to_list(self) -> list[dict]:
        return [asdict(e) for e in self.events]
```

- [ ] **Step 5: 实现 `registry.py`**

```python
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from .trace import Trace

_POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="cagent-call")


class CallError(Exception):
    def __init__(self, name: str, reason: str, retryable: bool = True):
        super().__init__(f"{name}: {reason}")
        self.name = name
        self.reason = reason
        self.retryable = retryable


@dataclass
class Spec:
    name: str
    kind: str
    fn: Callable[[Any], Any]
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    description: str
    timeout_s: float
    retries: int


class Registry:
    def __init__(self, owner: str):
        self.owner = owner
        self._specs: dict[str, Spec] = {}

    def register(self, name, fn, input_model, output_model, *, kind, description, timeout_s=5.0, retries=1):
        if kind not in ("tool", "skill"):
            raise ValueError(f"kind 只能是 tool 或 skill: {kind}")
        if name in self._specs:
            raise ValueError(f"重复注册: {name}")
        self._specs[name] = Spec(name, kind, fn, input_model, output_model, description, timeout_s, retries)

    def has(self, name: str) -> bool:
        return name in self._specs

    def names(self, kind: str | None = None) -> list[str]:
        return [s.name for s in self._specs.values() if kind is None or s.kind == kind]

    def describe(self) -> list[dict]:
        return [{
            "owner": self.owner, "name": s.name, "kind": s.kind, "description": s.description,
            "input": s.input_model.model_json_schema(), "output": s.output_model.model_json_schema(),
            "timeout_s": s.timeout_s, "retries": s.retries,
        } for s in self._specs.values()]

    def call(self, name: str, trace: Trace, **kwargs) -> BaseModel:
        spec = self._specs.get(name)
        if spec is None:
            trace.add("call", name, "error", owner=self.owner, error="未注册")
            raise CallError(name, "未注册", retryable=False)
        try:
            args = spec.input_model(**kwargs)
        except ValidationError as e:
            trace.add("call", name, "error", owner=self.owner, error=f"参数校验失败: {e.errors()[:3]}")
            raise CallError(name, "参数校验失败", retryable=False) from e
        last = ""
        for attempt in range(1, spec.retries + 2):
            start = time.perf_counter()
            future = _POOL.submit(spec.fn, args)
            try:
                raw = future.result(timeout=spec.timeout_s)
                out = raw if isinstance(raw, spec.output_model) else spec.output_model.model_validate(raw)
                trace.add("call", name, "ok", ms=int((time.perf_counter() - start) * 1000), owner=self.owner,
                          kind=spec.kind, attempt=attempt, input=args, output=out)
                return out
            except FutureTimeout:
                last = f"超时（>{spec.timeout_s}s）"
            except ValidationError as e:
                last = f"输出校验失败: {e.errors()[:2]}"
            except Exception as e:  # noqa: BLE001 工具内部任意异常都需要被诊断并重试
                last = f"{type(e).__name__}: {e}"
            status = "retry" if attempt <= spec.retries else "error"
            trace.add("call", name, status, ms=int((time.perf_counter() - start) * 1000), owner=self.owner,
                      kind=spec.kind, attempt=attempt, error=last)
        raise CallError(name, last)
```

- [ ] **Step 6: 运行测试确认通过**

Run: `python -m pytest tests/test_core_utils.py -v`
Expected: 17 passed

- [ ] **Step 7: Commit**

```powershell
git add backend/cagent/clock.py backend/cagent/trace.py backend/cagent/registry.py backend/tests/test_core_utils.py
git commit -m "feat: time hints, trace events and tool/skill registry with timeout and retry"
```

---

### Task 3: 配置中心与全部配置文件

**Files:**
- Create: `backend/cagent/config_center.py`, `backend/config/llm.yaml`, `backend/config/checkpoints.yaml`, `backend/config/policy.yaml`, `backend/config/intents.yaml`, `backend/config/main_agent.yaml`, `backend/config/business_packs/consumer_finance.yaml`, `backend/config/business_packs/auto_loan.yaml`
- Test: `backend/tests/test_config_center.py`

**Interfaces:**
- Produces:
  - `ConfigCenter(conn, config_dir: Path)`：构造时把 YAML 中尚未入库的版本写入 `config_versions`
  - `ConfigCenter.get(kind: str, key: str = "default") -> tuple[dict, str]`（内容, 版本号）；`kind` 取 `llm / checkpoints / policy / intents / main_agent / business_pack`
  - `ConfigCenter.save(kind, key, content: dict) -> str`（管理台写入，成为最新版本）
  - `ConfigCenter.pack_ids() -> list[str]`
  - `ConfigCenter.version_of(content: dict) -> str`（静态方法）
  - `config_center.validate_pack(pack, *, main_tools: set[str], collection_actions: set[str], rule_ids: set[str], post_tasks: set[str]) -> list[str]`
  - 业务包字段（后续任务依赖）：`pack_id, name, match{loan_types, default}, pre_tools[{tool, on_failure}], gates, stages[{id, goal, next}], stage_advance, intent_actions, skill_params.installment_plan.templates, extra_rules, handoff{max_refusals, emotion{types, min_intensity}, low_confidence_turns}, speech{agent_id, wait_seconds, identity, verify, purpose, tone, forbidden_words, templates, rewrite_fallback}, report_sections, post_tasks`

- [ ] **Step 1: 写配置文件**

`backend/config/llm.yaml`：

```yaml
base_url: https://api.deepseek.com
model: deepseek-flash
timeout_s: 15
max_retries: 1
temperature: 0
max_tokens: 800
extra_body:
  thinking:
    type: disabled
```

`backend/config/checkpoints.yaml`：

```yaml
negations: ["不会", "不能", "无法", "没法", "没办法", "不可", "无权", "不得", "不予", "并非", "不是"]
semantic_min_confidence: 0.7
detectors:
  R001:
    keywords: ["傻逼", "混蛋", "废物", "去死", "不要脸", "滚蛋", "你算什么东西", "老赖"]
  R002:
    patterns: ['上门(催收|找你|拜访)?', '起诉你', '抓你', '坐牢', '曝光你', '爆你通讯录', '让你好看', '后果自负', '找(到)?你(的)?(单位|公司|家里|家人|领导)']
    semantic: true
  R003:
    patterns: ['(保证|一定|肯定|承诺|包)[^，。！？,.!?]{0,8}(减免|免息|免除|延期|通过|批下来|批准)', '(全部|全额)(减免|免除)']
    semantic: true
  R004:
    window: {start: "22:00", end: "08:00"}
  R005:
    max_per_day: 3
  R006:
    patterns: ['告诉你(的)?(家人|朋友|同事|领导)', '(你家人|你同事|你朋友|你领导)[^，。]{0,6}(欠款|欠钱|逾期)']
    semantic: true
    operator_check: export_masked
  R007:
    patterns: ['(我是|我们是|这里是)[^，。]{0,4}(公安|警察|法院|检察院|派出所)']
    semantic: true
  R008:
    patterns: ['不还(也|都)?(没事|没关系|没影响|不要紧)', '不会有(任何)?(影响|后果)']
    semantic: true
  R009:
    patterns: ['(联系|打给|通知|找)你(的)?(家人|父母|亲戚|朋友|同事|领导)']
    semantic: true
    operator_check: contact_target
  R010:
    pressure_patterns: ['尽快还款', '马上还', '立即还', '今天必须', '必须还', '赶紧还']
    max_per_turn: 2
checkpoints:
  agent_speech:
    name: 坐席话术
    rules: [R001, R002, R003, R006, R007, R008, R009, R010]
    action_overrides: {}
    max_rewrites: 2
  operator_action:
    name: 运营操作
    rules: [R004, R005, R006, R009]
    action_overrides:
      R004: 阻断外呼并提示下次可外呼时间
      R005: 停止本次联系
  customer_input:
    name: 客户输入
    signals:
      辱骂: ["傻逼", "混蛋", "滚", "去死", "骗子"]
      投诉: ["投诉", "举报", "12378", "曝光你们"]
      明确拒绝: ["不还", "不会还", "别再打", "爱咋咋地"]
      要求转人工: ["转人工", "人工客服", "真人", "不跟你们机器人", "不跟机器人"]
```

`backend/config/policy.yaml`：

```yaml
scoring:
  weights: {repayment: 0.25, severity: 0.20, behavior: 0.15, call_result: 0.15, emotion: 0.10, credit: 0.15}
  behavior: {positive: [承诺还款, 查看账单, 部分还款, APP登录, 联系客服], negative: [拒绝沟通, 修改资料], step: 10}
  call_result: {positive: [承诺还款, 部分还款, 协商分期], negative: [挂断, 拒绝沟通, 无人接听], step: 10}
  emotion: {positive: [配合], negative: [抵触, 冷漠, 愤怒], recent: 5}
  credit: {min: 350, max: 850, level_scores: {A: 100, B: 75, C: 50, D: 25}, level_weight: 0.3}
  severity: {max_days: 365, debt_income_cap: 2.0}
  dialog_adjust: {承诺还款: 10, 要求分期: 5, 要求宽限: 0, 拒绝还款: -15, 投诉: -5}
  levels: {high: 70, medium: 40}
  review: {min_confidence: 0.7, conflict_penalty: 0.15}
strategies:
  - {level: 高, max_days: 30, strategy: 温和提醒}
  - {level: 高, strategy: 协商分期}
  - {level: 中, max_days: 90, strategy: 协商分期}
  - {level: 中, strategy: 提交结清减免申请}
  - {level: 低, max_days: 60, strategy: 协商分期}
  - {level: 低, strategy: 转人工重点跟进}
installment:
  affordability_ratio: 0.3
  period_options: [3, 6, 12, 18, 24, 36]
  max_plans: 3
priority:
  sort:
    - {field: overdue_days, desc: true}
    - {field: overdue_amount, desc: true}
```

`backend/config/intents.yaml`：

```yaml
low_confidence: 0.6
speaker_markers:
  agent: ["催收专员", "工号", "我们就", "不还就", "你今天不还", "如果你不还", "您名下逾期"]
  operator: ["生成", "报告", "打电话吗", "外呼吗", "可以打电话", "能不能打电话", "可以外呼", "能否外呼", "查询客户", "查一下客户", "客户画像", "发起催收", "结束催收"]
customer_intents:
  要求转人工: ["转人工", "人工客服", "真人", "不跟你们机器人", "不跟机器人"]
  投诉: ["投诉", "举报", "烦不烦", "别再打", "再打"]
  要求减免: ["减免", "免息", "免除", "少还点"]
  要求分期: ["分期", "分几期", "分开还", "一次性还不上"]
  要求宽限: ["宽限", "缓几天", "晚几天", "下个月", "发工资", "延期"]
  询问欠款: ["几笔", "哪一笔", "欠多少", "多少钱", "逾期了多少"]
  承诺还款: ["我会还", "马上还", "今天还", "明天还", "这就还"]
  拒绝还款: ["不还", "不会还", "没钱", "还不了"]
  质疑身份: ["你是谁", "骗子", "怎么证明"]
  其他: []
fallback_emotion:
  投诉: [愤怒, 0.7]
  拒绝还款: [抵触, 0.6]
  要求宽限: [焦虑, 0.5]
  要求分期: [焦虑, 0.4]
  要求减免: [焦虑, 0.4]
  要求转人工: [抵触, 0.5]
  承诺还款: [配合, 0.5]
  询问欠款: [平静, 0.3]
  质疑身份: [抵触, 0.4]
emotions: [愤怒, 焦虑, 抵触, 配合, 冷漠, 恐惧, 羞愧, 平静]
customer_to_system:
  要求转人工: 人工接管
  投诉: 情绪感知
  要求减免: 合规检测
  要求分期: 分期方案协商
  要求宽限: 还款意愿评估
  询问欠款: 多贷款管理
  承诺还款: 还款意愿评估
  拒绝还款: 提醒催收
  质疑身份: 提醒催收
  其他: 提醒催收
operator_intents:
  生成报告: ["报告", "汇总", "统计"]
  合规问答: ["打电话吗", "外呼吗", "可以打电话", "能不能打", "可以外呼", "能否外呼", "能打吗", "违规"]
  发起催收: ["发起催收", "开始催收"]
  结束催收: ["结束催收", "结束对话"]
  查询欠款: ["欠款", "逾期", "欠多少"]
  查询画像: ["画像", "客户信息", "意愿"]
  其他: []
operator_to_system:
  生成报告: 催收报告
  合规问答: 合规检测
  发起催收: 提醒催收
  结束催收: 催收报告
  查询欠款: 多贷款管理
  查询画像: 还款意愿评估
  其他: 提醒催收
agent_speech_system: {hit: 合规检测, pass: 提醒催收}
```

`backend/config/main_agent.yaml`：

```yaml
default_pack: consumer_finance
summary_keywords: ["本月", "汇总", "全部", "所有", "月度"]
```

`backend/config/business_packs/consumer_finance.yaml`：

```yaml
pack_id: consumer_finance
name: 通用消费金融催收
match:
  loan_types: [消费贷, 现金贷, 装修贷, 教育贷, 信用卡分期]
  default: true
pre_tools:
  - {tool: get_customer_profile, on_failure: degrade}
  - {tool: get_overdues, on_failure: block}
  - {tool: get_contact_count_today, on_failure: block}
  - {tool: get_willingness, on_failure: degrade}
gates: [R004, R005]
stages:
  - {id: 核身, goal: 表明身份与工号并核验客户本人身份, next: 告知逾期}
  - {id: 告知逾期, goal: 说明逾期金额与天数并询问还款意愿, next: 了解原因}
  - {id: 了解原因, goal: 了解还款困难原因与可承受金额, next: 协商}
  - {id: 协商, goal: 提供合规的分期或还款方案并协商调整, next: 确认承诺}
  - {id: 确认承诺, goal: 确认还款金额、日期或分期方案, next: 结束}
  - {id: 结束, goal: 礼貌结束并告知后续跟进, next: null}
stage_advance:
  要求分期: 协商
  要求宽限: 了解原因
  要求减免: 协商
  承诺还款: 确认承诺
intent_actions:
  要求分期: [installment_plan]
  要求宽限: [record_grace_request, installment_plan]
  要求减免: [submit_waiver_request, installment_plan]
  询问欠款: [loan_priority]
  承诺还款: [record_promise]
  投诉: [reduce_contact]
  要求转人工: [request_handoff]
  拒绝还款: []
  质疑身份: []
  其他: []
skill_params:
  installment_plan:
    templates: [T001, T002, T003, T004, T005, T006]
extra_rules: []
handoff:
  max_refusals: 2
  emotion: {types: [愤怒], min_intensity: 0.9}
  low_confidence_turns: 2
speech:
  agent_id: AI01
  wait_seconds: 30
  identity: "您好，我是XX金融贷后服务专员，工号{agent_id}。"
  verify: "沟通账单前需要先核实身份，麻烦您提供一下身份证号后四位。"
  purpose: "核实完成后，我会跟您说明账单逾期情况，也想了解一下您这边的还款打算。"
  tone: 礼貌、克制、先共情再说明，不施压
  forbidden_words: [老赖, 欠债不还, 后果自负]
  rewrite_fallback: "这笔账单目前已经逾期，继续拖延会影响您的征信记录。如果一次性还款有困难，我们可以一起看看分期等安排。"
  templates:
    opening: "{identity}{verify}{purpose}"
    clarify: "抱歉，刚才没有听清您的意思，您可以再说一遍吗？"
    handoff: "好的，这就为您转接人工坐席。您刚才说明的情况和账户信息已同步给坐席同事，不需要再重复，请稍候，预计{wait_seconds}秒内接通。"
    default: "关于您的逾期账单，我们希望和您一起找到合适的还款安排，请问您现在方便沟通吗？"
    提醒催收: "{identity}您目前的逾期欠款共{principal}，请问您近期是否方便安排还款？"
    还款意愿评估: "理解您现在的难处，您的宽限请求我已经记录。为避免逾期天数继续增加，也可以考虑分期：{plan_brief}。请问您预计哪天能收到工资？"
    分期方案协商: "可以办理分期。按您目前的欠款{principal}，可选方案有：{plan_brief}。请问您每月大概能承受多少还款？"
    情绪感知: "打扰到您很抱歉，您的意见我已经记下，后续会控制联系频次。账单的事可以改到您方便的时候再聊，您看哪个时间段合适？"
    合规检测: "谢谢您愿意还款。减免需要按流程提交审批，我没办法直接给出结果，可以先帮您提交申请；同时也可以看看分期：{plan_brief}。您看可以吗？"
    多贷款管理: "已为您查到名下{loan_count}笔逾期：{loan_list}。{suggestion}，您看是否先处理这一笔？"
    承诺还款: "好的，已记录您的还款安排：{promise_brief}。请按约定时间还款，如有变化可以随时联系我们。"
report_sections: [逾期概况, 还款意愿评估, 情绪感知, 合规审计, 协商记录, 建议策略]
post_tasks: [write_call_record, write_behavior, write_emotion, refresh_profile, generate_report, create_todos]
```

`backend/config/business_packs/auto_loan.yaml`：与 `consumer_finance.yaml` 结构相同，仅以下字段不同（完整文件如下）：

```yaml
pack_id: auto_loan
name: 车贷催收
match:
  loan_types: [车贷]
  default: false
pre_tools:
  - {tool: get_customer_profile, on_failure: degrade}
  - {tool: get_overdues, on_failure: block}
  - {tool: get_contact_count_today, on_failure: block}
  - {tool: get_willingness, on_failure: degrade}
gates: [R004, R005]
stages:
  - {id: 核身, goal: 表明身份与工号并核验客户本人身份, next: 告知逾期}
  - {id: 告知逾期, goal: 说明车贷逾期金额与天数并询问还款意愿, next: 了解原因}
  - {id: 了解原因, goal: 了解还款困难原因与可承受金额, next: 协商}
  - {id: 协商, goal: 提供合规的分期或还款方案并协商调整, next: 确认承诺}
  - {id: 确认承诺, goal: 确认还款金额、日期或分期方案, next: 结束}
  - {id: 结束, goal: 礼貌结束并告知后续跟进, next: null}
stage_advance:
  要求分期: 协商
  要求宽限: 了解原因
  要求减免: 协商
  承诺还款: 确认承诺
intent_actions:
  要求分期: [installment_plan]
  要求宽限: [record_grace_request, installment_plan]
  要求减免: [submit_waiver_request, installment_plan]
  询问欠款: [loan_priority]
  承诺还款: [record_promise]
  投诉: [reduce_contact]
  要求转人工: [request_handoff]
  拒绝还款: []
  质疑身份: []
  其他: []
skill_params:
  installment_plan:
    templates: [T002, T003, T004, T006]
extra_rules:
  - rule_id: BR-AUTO-01
    rule_type: 威胁
    rule_content: 不得以拖车、收车相威胁
    violation_level: 严重
    action: 人工接管
    patterns: ['拖车', '收车', '把(你的)?车(拖|收)走']
    semantic: false
handoff:
  max_refusals: 2
  emotion: {types: [愤怒], min_intensity: 0.9}
  low_confidence_turns: 2
speech:
  agent_id: AI02
  wait_seconds: 30
  identity: "您好，我是XX金融车贷贷后服务专员，工号{agent_id}。"
  verify: "沟通账单前需要先核实身份，麻烦您提供一下身份证号后四位。"
  purpose: "核实完成后，我会跟您说明车贷账单的逾期情况，也想了解一下您这边的还款打算。"
  tone: 礼貌、克制、先共情再说明，不施压
  forbidden_words: [老赖, 欠债不还, 后果自负, 拖车]
  rewrite_fallback: "您的车贷账单目前已经逾期，继续拖延会影响您的征信记录。如果一次性还款有困难，我们可以一起看看分期等安排。"
  templates:
    opening: "{identity}{verify}{purpose}"
    clarify: "抱歉，刚才没有听清您的意思，您可以再说一遍吗？"
    handoff: "好的，这就为您转接人工坐席。您刚才说明的情况和账户信息已同步给坐席同事，不需要再重复，请稍候，预计{wait_seconds}秒内接通。"
    default: "关于您的车贷逾期账单，我们希望和您一起找到合适的还款安排，请问您现在方便沟通吗？"
    提醒催收: "{identity}您的车贷目前逾期欠款共{principal}，请问您近期是否方便安排还款？"
    还款意愿评估: "理解您现在的难处，您的宽限请求我已经记录。为避免逾期天数继续增加，也可以考虑分期：{plan_brief}。请问您预计哪天能收到工资？"
    分期方案协商: "可以办理分期。按您目前的车贷欠款{principal}，可选方案有：{plan_brief}。请问您每月大概能承受多少还款？"
    情绪感知: "打扰到您很抱歉，您的意见我已经记下，后续会控制联系频次。账单的事可以改到您方便的时候再聊，您看哪个时间段合适？"
    合规检测: "谢谢您愿意还款。减免需要按流程提交审批，我没办法直接给出结果，可以先帮您提交申请；同时也可以看看分期：{plan_brief}。您看可以吗？"
    多贷款管理: "已为您查到名下{loan_count}笔逾期：{loan_list}。{suggestion}，您看是否先处理这一笔？"
    承诺还款: "好的，已记录您的还款安排：{promise_brief}。请按约定时间还款，如有变化可以随时联系我们。"
report_sections: [逾期概况, 还款意愿评估, 情绪感知, 合规审计, 协商记录, 建议策略]
post_tasks: [write_call_record, write_behavior, write_emotion, refresh_profile, generate_report, create_todos]
```

- [ ] **Step 2: 写失败测试**

`backend/tests/test_config_center.py`：

```python
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
```

- [ ] **Step 3: 运行测试确认失败**

Run: `python -m pytest tests/test_config_center.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'cagent.config_center'`

- [ ] **Step 4: 实现 `config_center.py`**

```python
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import yaml

SINGLETON_KINDS = ("llm", "checkpoints", "policy", "intents", "main_agent")
REQUIRED_PACK_KEYS = ("pack_id", "name", "match", "pre_tools", "gates", "stages", "stage_advance", "intent_actions",
                      "skill_params", "handoff", "speech", "post_tasks")


class ConfigError(Exception):
    pass


class ConfigCenter:
    def __init__(self, conn: sqlite3.Connection, config_dir: Path):
        self.conn = conn
        self.dir = Path(config_dir)
        self._sync_files()

    @staticmethod
    def version_of(content: dict) -> str:
        raw = json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:10]

    def _files(self) -> list[tuple[str, str, Path]]:
        items = [(k, "default", self.dir / f"{k}.yaml") for k in SINGLETON_KINDS]
        items += [("business_pack", p.stem, p) for p in sorted((self.dir / "business_packs").glob("*.yaml"))]
        return [i for i in items if i[2].exists()]

    def _sync_files(self) -> None:
        for kind, key, path in self._files():
            content = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            self.conn.execute(
                "INSERT OR IGNORE INTO config_versions(kind, key, version, content, created_at) VALUES (?,?,?,?,?)",
                (kind, key, self.version_of(content), json.dumps(content, ensure_ascii=False),
                 datetime.now().isoformat(timespec="microseconds")),
            )
        self.conn.commit()

    def get(self, kind: str, key: str = "default") -> tuple[dict, str]:
        row = self.conn.execute(
            "SELECT content, version FROM config_versions WHERE kind=? AND key=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (kind, key),
        ).fetchone()
        if row is None:
            raise ConfigError(f"配置不存在: {kind}/{key}")
        return json.loads(row["content"]), row["version"]

    def save(self, kind: str, key: str, content: dict) -> str:
        version = self.version_of(content)
        self.conn.execute(
            "INSERT INTO config_versions(kind, key, version, content, created_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(kind, key, version) DO UPDATE SET created_at=excluded.created_at",
            (kind, key, version, json.dumps(content, ensure_ascii=False),
             datetime.now().isoformat(timespec="microseconds")),
        )
        self.conn.commit()
        return version

    def pack_ids(self) -> list[str]:
        rows = self.conn.execute("SELECT DISTINCT key FROM config_versions WHERE kind='business_pack'")
        return sorted(r["key"] for r in rows)


def validate_pack(pack: dict, *, main_tools: set[str], collection_actions: set[str], rule_ids: set[str],
                  post_tasks: set[str]) -> list[str]:
    errors = [f"缺少字段 {k}" for k in REQUIRED_PACK_KEYS if k not in pack]
    if errors:
        return errors
    for item in pack["pre_tools"]:
        if item.get("tool") not in main_tools:
            errors.append(f"前置工具不存在: {item.get('tool')}")
        if item.get("on_failure") not in ("block", "degrade", "warn"):
            errors.append(f"前置工具 {item.get('tool')} 的失败策略非法: {item.get('on_failure')}")
    for rid in pack["gates"]:
        if rid not in rule_ids:
            errors.append(f"门禁引用的规则不存在: {rid}")
    stage_ids = [s["id"] for s in pack["stages"]]
    if len(stage_ids) != len(set(stage_ids)):
        errors.append("阶段编号重复")
    for s in pack["stages"]:
        if s.get("next") is not None and s["next"] not in stage_ids:
            errors.append(f"阶段 {s['id']} 的下一阶段不存在: {s['next']}")
    for intent, target in pack["stage_advance"].items():
        if target not in stage_ids:
            errors.append(f"意图 {intent} 跳转的阶段不存在: {target}")
    for intent, actions in pack["intent_actions"].items():
        for a in actions:
            if a not in collection_actions:
                errors.append(f"意图 {intent} 的动作不存在: {a}")
    for er in pack.get("extra_rules", []):
        if er.get("rule_id") in rule_ids:
            errors.append(f"业务专属规则编号与规则库冲突: {er.get('rule_id')}")
        if not er.get("patterns"):
            errors.append(f"业务专属规则缺少 patterns: {er.get('rule_id')}")
    for name in pack["post_tasks"]:
        if name not in post_tasks:
            errors.append(f"后置任务不存在: {name}")
    for key in ("opening", "clarify", "handoff", "default"):
        if key not in pack["speech"].get("templates", {}):
            errors.append(f"话术模板缺少 {key}")
    return errors
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_config_center.py -v`
Expected: 5 passed

- [ ] **Step 6: Commit**

```powershell
git add backend/config backend/cagent/config_center.py backend/tests/test_config_center.py
git commit -m "feat: yaml config center with versions, business packs and pack validation"
```

---

### Task 4: 脱敏网关与大模型客户端

**Files:**
- Create: `backend/cagent/desensitize.py`, `backend/cagent/llm.py`, `backend/scripts/llm_smoke.py`
- Test: `backend/tests/test_llm.py`

**Interfaces:**
- Produces:
  - `desensitize.MaskMap`（`token(kind, original) -> str`、`unmask(text) -> str`）
  - `desensitize.mask(text: str, mm: MaskMap | None = None, names: Iterable[str] = ()) -> tuple[str, MaskMap]`
  - `desensitize.display_mask(text: str) -> str`
  - `llm.LLMError`
  - `llm.DeepSeekClient(cfg: dict)`、`llm.FakeLLM(responder=None, fail=False)`：都实现 `complete_json(system, user, *, max_tokens=800) -> dict` 与 `stream_text(system, user, *, max_tokens=800) -> Iterator[str]`
  - `llm.build_llm(cfg: dict) -> DeepSeekClient | None`（无 `DEEPSEEK_API_KEY` 时返回 None）
  - `llm.GuardedLLM(client, conn)`：属性 `available: bool`；`json(purpose: str, system: str, payload: dict, *, names=()) -> dict`（入参脱敏、出参还原、写 `llm_calls`）
  - `FakeLLM` 的 `responder(system, user) -> dict`，系统提示第一行固定为 `任务代号：{purpose}`，测试据此分派

- [ ] **Step 1: 写失败测试**

`backend/tests/test_llm.py`：

```python
import json

import pytest

from cagent.desensitize import display_mask, mask
from cagent.llm import FakeLLM, GuardedLLM, LLMError


def test_mask_and_unmask_roundtrip():
    text = "客户0001，身份证110101199001011234，手机13812345678，欠款12000元"
    masked, mm = mask(text)
    for raw in ("客户0001", "110101199001011234", "13812345678", "12000元"):
        assert raw not in masked
    assert mm.unmask(masked) == text


def test_mask_extra_names_and_same_token():
    masked, mm = mask("张三说张三会还", names=["张三"])
    assert masked.count("<NAME_1>") == 2


def test_display_mask():
    out = display_mask("客户0001 13812345678 110101199001011234")
    assert "客户**01" in out and "138****5678" in out and "110101********1234" in out


def test_guarded_llm_masks_input_logs_and_unmasks(db_conn):
    def responder(system, user):
        assert "客户0003" not in user
        token = json.loads(user)["text"].split("，")[0]
        return {"echo": token}

    llm = GuardedLLM(FakeLLM(responder), db_conn)
    out = llm.json("unit", "测试", {"text": "客户0003，欠款20000元"})
    assert out == {"echo": "客户0003"}
    row = db_conn.execute("SELECT purpose, masked_input, status FROM llm_calls").fetchone()
    assert row["purpose"] == "unit" and row["status"] == "ok" and "客户0003" not in row["masked_input"]


def test_guarded_llm_failure_logged_and_raised(db_conn):
    llm = GuardedLLM(FakeLLM(fail=True), db_conn)
    with pytest.raises(LLMError):
        llm.json("unit", "测试", {"text": "x"})
    assert db_conn.execute("SELECT status FROM llm_calls").fetchone()["status"] == "error"


def test_guarded_llm_unavailable(db_conn):
    llm = GuardedLLM(None, db_conn)
    assert llm.available is False
    with pytest.raises(LLMError):
        llm.json("unit", "测试", {})


def test_system_prompt_carries_purpose(db_conn):
    seen = []
    llm = GuardedLLM(FakeLLM(lambda s, u: seen.append(s) or {}), db_conn)
    llm.json("speaker", "判断说话人", {})
    assert seen[0].splitlines()[0] == "任务代号：speaker"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_llm.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'cagent.desensitize'`

- [ ] **Step 3: 实现 `desensitize.py`**

```python
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

PATTERNS = [
    ("NAME", re.compile(r"客户\d{4}")),
    ("ID", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("PHONE", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("AMOUNT", re.compile(r"(?<![\dA-Za-z_])\d+(?:,\d{3})*(?:\.\d+)?\s*元")),
]


@dataclass
class MaskMap:
    forward: dict[str, str] = field(default_factory=dict)

    def token(self, kind: str, original: str) -> str:
        if original not in self.forward:
            n = sum(1 for t in self.forward.values() if t.startswith(f"<{kind}_")) + 1
            self.forward[original] = f"<{kind}_{n}>"
        return self.forward[original]

    def unmask(self, text: str) -> str:
        for original, tok in self.forward.items():
            text = text.replace(tok, original)
        return text


def mask(text: str, mm: MaskMap | None = None, names: Iterable[str] = ()) -> tuple[str, MaskMap]:
    mm = mm or MaskMap()
    for name in names:
        if name:
            text = text.replace(name, mm.token("NAME", name))
    for kind, pattern in PATTERNS:
        text = pattern.sub(lambda m, k=kind: mm.token(k, m.group(0)), text)
    return text, mm


def display_mask(text: str) -> str:
    text = re.sub(r"(?<!\d)(\d{6})\d{8}(\d{3}[\dXx])(?!\d)", r"\1********\2", text)
    text = re.sub(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)", r"\1****\2", text)
    return re.sub(r"客户\d{2}(\d{2})", r"客户**\1", text)
```

- [ ] **Step 4: 实现 `llm.py`**

```python
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from typing import Callable, Iterable, Iterator, Protocol

from .desensitize import MaskMap, mask


class LLMError(Exception):
    pass


class LLMClient(Protocol):
    def complete_json(self, system: str, user: str, *, max_tokens: int = 800) -> dict: ...

    def stream_text(self, system: str, user: str, *, max_tokens: int = 800) -> Iterator[str]: ...


class DeepSeekClient:
    def __init__(self, cfg: dict):
        from openai import OpenAI

        key = os.getenv("DEEPSEEK_API_KEY")
        if not key:
            raise LLMError("未设置 DEEPSEEK_API_KEY")
        self.model = os.getenv("DEEPSEEK_MODEL", cfg.get("model", "deepseek-flash"))
        self.client = OpenAI(api_key=key, base_url=os.getenv("DEEPSEEK_BASE_URL", cfg.get("base_url")),
                             timeout=cfg.get("timeout_s", 15), max_retries=0)
        self.temperature = cfg.get("temperature", 0)
        self.retries = cfg.get("max_retries", 1)
        self.max_tokens = cfg.get("max_tokens", 800)
        self.extra_body = cfg.get("extra_body") or {}

    def _messages(self, system: str, user: str) -> list[dict]:
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def complete_json(self, system: str, user: str, *, max_tokens: int = 800) -> dict:
        last: Exception | None = None
        for _ in range(self.retries + 1):
            try:
                resp = self.client.chat.completions.create(
                    model=self.model, messages=self._messages(system, user), response_format={"type": "json_object"},
                    temperature=self.temperature, max_tokens=max_tokens or self.max_tokens, extra_body=self.extra_body,
                )
                content = (resp.choices[0].message.content or "").strip()
                if not content:
                    raise LLMError("模型返回空内容")
                return json.loads(content)
            except Exception as e:  # noqa: BLE001 网络、解析、空响应统一重试
                last = e
        raise LLMError(f"大模型调用失败: {last}")

    def stream_text(self, system: str, user: str, *, max_tokens: int = 800) -> Iterator[str]:
        try:
            stream = self.client.chat.completions.create(
                model=self.model, messages=self._messages(system, user), temperature=self.temperature,
                max_tokens=max_tokens or self.max_tokens, extra_body=self.extra_body, stream=True,
            )
            for chunk in stream:
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if delta:
                    yield delta
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"大模型流式调用失败: {e}") from e


class FakeLLM:
    def __init__(self, responder: Callable[[str, str], dict] | None = None, fail: bool = False):
        self.responder = responder
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, system: str, user: str, *, max_tokens: int = 800) -> dict:
        self.calls.append((system, user))
        if self.fail:
            raise LLMError("模拟失败")
        return self.responder(system, user) if self.responder else {}

    def stream_text(self, system: str, user: str, *, max_tokens: int = 800) -> Iterator[str]:
        out = self.complete_json(system, user, max_tokens=max_tokens)
        yield from str(out.get("text", ""))


def build_llm(cfg: dict) -> DeepSeekClient | None:
    try:
        return DeepSeekClient(cfg)
    except LLMError:
        return None


def _unmask(value, mm: MaskMap):
    if isinstance(value, str):
        return mm.unmask(value)
    if isinstance(value, dict):
        return {k: _unmask(v, mm) for k, v in value.items()}
    if isinstance(value, list):
        return [_unmask(v, mm) for v in value]
    return value


class GuardedLLM:
    def __init__(self, client: LLMClient | None, conn: sqlite3.Connection):
        self.client = client
        self.conn = conn
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self.client is not None

    def _log(self, purpose: str, masked_input: str, masked_output: str, status: str, start: float) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO llm_calls(ts, purpose, masked_input, masked_output, status, latency_ms) VALUES (?,?,?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), purpose, masked_input[:4000], masked_output[:4000],
                 status, int((time.perf_counter() - start) * 1000)),
            )
            self.conn.commit()

    def json(self, purpose: str, system: str, payload: dict, *, names: Iterable[str] = ()) -> dict:
        if self.client is None:
            raise LLMError("大模型不可用")
        masked_user, mm = mask(json.dumps(payload, ensure_ascii=False), names=names)
        full_system = f"任务代号：{purpose}\n{system}"
        start = time.perf_counter()
        try:
            out = self.client.complete_json(full_system, masked_user)
        except LLMError as e:
            self._log(purpose, masked_user, str(e), "error", start)
            raise
        self._log(purpose, masked_user, json.dumps(out, ensure_ascii=False), "ok", start)
        return _unmask(out, mm)
```

`backend/scripts/llm_smoke.py`（有 Key 时手动验证接口参数）：

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

from cagent import settings  # noqa: E402
from cagent.llm import build_llm  # noqa: E402

cfg = yaml.safe_load((settings.CONFIG_DIR / "llm.yaml").read_text(encoding="utf-8"))
client = build_llm(cfg)
if client is None:
    sys.exit("未设置 DEEPSEEK_API_KEY")
print(client.complete_json('输出 json，例如 {"ok": true}', "请返回 ok=true 的 json"))
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_llm.py -v`
Expected: 7 passed

可选：设置 `$env:DEEPSEEK_API_KEY="..."` 后运行 `python scripts/llm_smoke.py`，Expected: `{'ok': True}`。若接口拒绝 `thinking` 参数，删除 `llm.yaml` 中的 `extra_body` 后重试。

- [ ] **Step 6: Commit**

```powershell
git add backend/cagent/desensitize.py backend/cagent/llm.py backend/scripts/llm_smoke.py backend/tests/test_llm.py
git commit -m "feat: desensitization gateway and guarded deepseek client with call audit"
```

---

### Task 5: 合规引擎（三个检查点）与审计日志

**Files:**
- Create: `backend/cagent/compliance.py`, `backend/cagent/audit.py`
- Test: `backend/tests/test_compliance.py`

**Interfaces:**
- Consumes: `clock.in_quiet_hours / next_allowed_time`、`llm.GuardedLLM / LLMError`、`desensitize.display_mask`、`db.insert_generated`
- Produces:
  - `compliance.RuleHit`：`rule_id, rule_type, rule_content, level, action, method, evidence, confidence`
  - `compliance.CheckResult`：`checkpoint, passed, hits, block, handoff, degraded, signals, next_allowed: str | None, notes`
  - `compliance.NOT_CONTACT_METHODS = ("外呼拦截", "客户进线")`
  - `compliance.contact_count_on(conn, customer_id: str, day: date) -> int`
  - `ComplianceEngine(conn, cfg: dict, version: str, llm: GuardedLLM | None = None)`，属性 `cfg`、`rules: dict[str, dict]`、`version`
    - `check_agent_speech(text, *, extra_rules=(), pressure_so_far=0, use_semantic=True) -> CheckResult`
    - `check_operator_action(*, action: str, customer_id: str | None, now: datetime, only: list[str] | None = None, contact_target="本人", masked=True) -> CheckResult`（`action` 取 `外呼 / 发起催收 / 导出`）
    - `check_customer_input(text) -> CheckResult`
  - `audit.write_audit(conn, *, result: CheckResult, customer_id, scene, content, config_version, ts: datetime, session_id=None) -> list[str]`
  - `audit.write_event(conn, *, checkpoint, customer_id, scene, content, ts: datetime, session_id=None, config_version="-") -> str`
  - `audit.export_audit_csv(conn, path: Path) -> int`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_compliance.py`：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_compliance.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'cagent.compliance'`

- [ ] **Step 3: 实现 `compliance.py`**

```python
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime

from pydantic import BaseModel

from .clock import in_quiet_hours, next_allowed_time
from .llm import GuardedLLM, LLMError

NOT_CONTACT_METHODS = ("外呼拦截", "客户进线")
CONTACT_ACTIONS = ("外呼", "发起催收")

SEMANTIC_SYSTEM = (
    "你是金融催收合规审核员。只依据给定的规则清单判断坐席话术是否违规，不得引用清单以外的任何法规、政策或条款。\n"
    "否定表述（例如“不会上门”“无法承诺减免”）不构成违规。\n"
    '输出 json，例如 {"hits": [{"rule_id": "R002", "evidence": "话术中的原文片段", "confidence": 0.9}]}，'
    '没有违规时输出 {"hits": []}。'
)


class RuleHit(BaseModel):
    rule_id: str
    rule_type: str
    rule_content: str
    level: str
    action: str
    method: str
    evidence: str
    confidence: float = 1.0


class CheckResult(BaseModel):
    checkpoint: str
    passed: bool
    hits: list[RuleHit] = []
    block: bool = False
    handoff: bool = False
    degraded: bool = False
    signals: list[str] = []
    next_allowed: str | None = None
    notes: list[str] = []


def contact_count_on(conn: sqlite3.Connection, customer_id: str, day: date) -> int:
    sql = ("SELECT COUNT(*) FROM fact_call_history WHERE customer_id=? AND substr(call_time,1,10)=? "
           "AND COALESCE(call_method,'') NOT IN (?,?)")
    return conn.execute(sql, (customer_id, day.isoformat(), *NOT_CONTACT_METHODS)).fetchone()[0]


class ComplianceEngine:
    def __init__(self, conn: sqlite3.Connection, cfg: dict, version: str, llm: GuardedLLM | None = None):
        self.conn = conn
        self.cfg = cfg
        self.version = version
        self.llm = llm
        self.negations = cfg.get("negations", [])
        self.rules = {r["rule_id"]: dict(r) for r in conn.execute(
            "SELECT rule_id, rule_type, rule_content, violation_level, action FROM dim_compliance_rule")}

    def _hit(self, rule: dict, checkpoint: str, method: str, evidence: str, confidence: float = 1.0) -> RuleHit:
        overrides = self.cfg["checkpoints"][checkpoint].get("action_overrides") or {}
        return RuleHit(rule_id=rule["rule_id"], rule_type=rule["rule_type"], rule_content=rule["rule_content"],
                       level=rule["violation_level"], action=overrides.get(rule["rule_id"], rule["action"]),
                       method=method, evidence=evidence, confidence=confidence)

    def _match(self, text: str, det: dict) -> str | None:
        patterns = list(det.get("patterns", [])) + [re.escape(k) for k in det.get("keywords", [])]
        for p in patterns:
            for m in re.finditer(p, text):
                prefix = text[max(0, m.start() - 6):m.start()]
                if any(n in prefix for n in self.negations):
                    continue
                return m.group(0)
        return None

    def _semantic(self, text: str, rules: dict[str, dict], checkpoint: str) -> list[RuleHit]:
        payload = {"话术": text, "规则清单": [
            {"rule_id": rid, "规则类型": r["rule_type"], "规则内容": r["rule_content"]} for rid, r in rules.items()]}
        out = self.llm.json("compliance_semantic", SEMANTIC_SYSTEM, payload)
        min_conf = self.cfg.get("semantic_min_confidence", 0.7)
        hits = []
        for h in out.get("hits") or []:
            rid = h.get("rule_id")
            try:
                conf = float(h.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0.0
            if rid in rules and conf >= min_conf:
                hits.append(self._hit(rules[rid], checkpoint, "语义", str(h.get("evidence", ""))[:80], conf))
        return hits

    def check_agent_speech(self, text: str, *, extra_rules=(), pressure_so_far: int = 0,
                           use_semantic: bool = True) -> CheckResult:
        cp = "agent_speech"
        enabled = self.cfg["checkpoints"][cp]["rules"]
        candidates = [(rid, self.rules[rid], self.cfg["detectors"].get(rid, {})) for rid in enabled if rid in self.rules]
        candidates += [(er["rule_id"], er, er) for er in extra_rules]
        hits: list[RuleHit] = []
        for rid, rule, det in candidates:
            if "pressure_patterns" in det:
                current = sum(len(re.findall(p, text)) for p in det["pressure_patterns"])
                count = pressure_so_far + current
                if current and count > det.get("max_per_turn", 2):
                    hits.append(self._hit(rule, cp, "计数", f"施压表述累计{count}次"))
                continue
            evidence = self._match(text, det)
            if evidence:
                hits.append(self._hit(rule, cp, "关键词", evidence))
        degraded = False
        if use_semantic:
            hit_ids = {h.rule_id for h in hits}
            sem = {rid: rule for rid, rule, det in candidates if rid not in hit_ids and det.get("semantic")}
            if sem:
                if self.llm is not None and self.llm.available:
                    try:
                        hits += self._semantic(text, sem, cp)
                    except LLMError:
                        degraded = True
                else:
                    degraded = True
        handoff = any(h.level == "严重" or h.action == "人工接管" for h in hits)
        notes = ["语义判定不可用，仅按关键词规则判定，需人工复核"] if degraded else []
        return CheckResult(checkpoint=cp, passed=not hits, hits=hits, block=bool(hits), handoff=handoff,
                           degraded=degraded, notes=notes)

    def check_operator_action(self, *, action: str, customer_id: str | None, now: datetime,
                              only: list[str] | None = None, contact_target: str = "本人",
                              masked: bool = True) -> CheckResult:
        cp = "operator_action"
        hits: list[RuleHit] = []
        next_allowed = None
        for rid in self.cfg["checkpoints"][cp]["rules"]:
            rule = self.rules.get(rid)
            if rule is None or (only is not None and rid not in only):
                continue
            det = self.cfg["detectors"].get(rid, {})
            if "window" in det and action in CONTACT_ACTIONS:
                s, e = det["window"]["start"], det["window"]["end"]
                if in_quiet_hours(now, s, e):
                    hits.append(self._hit(rule, cp, "时段", f"{s}-次日{e}"))
                    next_allowed = next_allowed_time(now, s, e).isoformat(timespec="minutes")
            elif "max_per_day" in det and action in CONTACT_ACTIONS and customer_id:
                n = contact_count_on(self.conn, customer_id, now.date())
                if n >= det["max_per_day"]:
                    hits.append(self._hit(rule, cp, "频次", f"{customer_id} 在 {now:%Y-%m-%d} 已联系{n}次，单日上限{det['max_per_day']}次"))
            elif det.get("operator_check") == "contact_target" and action in CONTACT_ACTIONS and contact_target != "本人":
                hits.append(self._hit(rule, cp, "联系对象", f"联系对象为{contact_target}"))
            elif det.get("operator_check") == "export_masked" and action == "导出" and not masked:
                hits.append(self._hit(rule, cp, "导出脱敏", "导出文件未脱敏"))
        return CheckResult(checkpoint=cp, passed=not hits, hits=hits, block=bool(hits), next_allowed=next_allowed)

    def check_customer_input(self, text: str) -> CheckResult:
        signals = self.cfg["checkpoints"]["customer_input"]["signals"]
        found = [name for name, words in signals.items() if any(w in text for w in words)]
        return CheckResult(checkpoint="customer_input", passed=True, signals=found)
```

- [ ] **Step 4: 实现 `audit.py`**

```python
from __future__ import annotations

import csv
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from .compliance import CheckResult
from .desensitize import display_mask

CHECKPOINT_NAMES = {"agent_speech": "坐席话术", "operator_action": "运营操作", "customer_input": "客户输入"}

_INSERT = ("INSERT INTO audit_log(log_id, ts, customer_id, checkpoint, scene, content, rule_id, rule_type, level, "
           "action, handoff, config_version, session_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)")


def _id() -> str:
    return "AUD-" + uuid.uuid4().hex[:10]


def write_audit(conn: sqlite3.Connection, *, result: CheckResult, customer_id: str | None, scene: str, content: str,
                config_version: str, ts: datetime, session_id: str | None = None) -> list[str]:
    ids = []
    for hit in result.hits or [None]:
        log_id = _id()
        conn.execute(_INSERT, (
            log_id, ts.isoformat(timespec="seconds"), customer_id, CHECKPOINT_NAMES.get(result.checkpoint, result.checkpoint),
            scene, display_mask(content)[:300], hit.rule_id if hit else None, hit.rule_type if hit else None,
            hit.level if hit else None, hit.action if hit else None, int(result.handoff), config_version, session_id,
        ))
        ids.append(log_id)
    conn.commit()
    return ids


def write_event(conn: sqlite3.Connection, *, checkpoint: str, customer_id: str | None, scene: str, content: str,
                ts: datetime, session_id: str | None = None, config_version: str = "-") -> str:
    log_id = _id()
    conn.execute(_INSERT, (log_id, ts.isoformat(timespec="seconds"), customer_id, checkpoint, scene,
                           display_mask(content)[:300], None, None, None, None, 0, config_version, session_id))
    conn.commit()
    return log_id


def export_audit_csv(conn: sqlite3.Connection, path: Path) -> int:
    rows = conn.execute("SELECT * FROM audit_log ORDER BY ts, log_id").fetchall()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["日志编号", "时间戳", "客户编号", "检查点", "场景类型", "话术内容摘要", "命中规则编号", "规则类型",
                    "违规等级", "处置动作", "是否转人工", "配置版本", "会话编号"])
        for r in rows:
            w.writerow([r["log_id"], r["ts"], r["customer_id"], r["checkpoint"], r["scene"], r["content"],
                        r["rule_id"] or "", r["rule_type"] or "", r["level"] or "", r["action"] or "",
                        "是" if r["handoff"] else "否", r["config_version"], r["session_id"] or ""])
    return len(rows)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_compliance.py -v`
Expected: 15 passed

- [ ] **Step 6: Commit**

```powershell
git add backend/cagent/compliance.py backend/cagent/audit.py backend/tests/test_compliance.py
git commit -m "feat: compliance engine with three checkpoints, semantic fallback and audit log"
```

---

### Task 6: 还款意愿评分与建议策略

**Files:**
- Create: `backend/cagent/scoring.py`
- Test: `backend/tests/test_scoring.py`

**Interfaces:**
- Produces:
  - `scoring.Factor`：`name, score, weight, contribution, evidence: list[str], note`
  - `scoring.Willingness`：`customer_id, score: int, level: str（高/中/低）, factors, adjustments: list[dict], confidence: float, needs_review: bool, review_reasons: list[str]`
  - `scoring.score_customer(conn, customer_id: str, policy: dict, dialog_intents: Iterable[str] = ()) -> Willingness`（客户不存在抛 `ValueError`）
  - `scoring.max_overdue_days(conn, customer_id) -> int`
  - `scoring.choose_strategy(policy: dict, level: str, max_days: int) -> str`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_scoring.py`：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_scoring.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'cagent.scoring'`

- [ ] **Step 3: 实现 `scoring.py`**

```python
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_scoring.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```powershell
git add backend/cagent/scoring.py backend/tests/test_scoring.py
git commit -m "feat: explainable willingness score with evidence, confidence and strategy table"
```

---

### Task 7: 分期方案测算与多笔逾期优先级

**Files:**
- Create: `backend/cagent/installment.py`, `backend/cagent/loans.py`
- Test: `backend/tests/test_installment.py`

**Interfaces:**
- Produces:
  - `installment.parse_condition(text: str) -> dict`（键：`text, max_days, min_days_exclusive, min_credit, lump_sum`）
  - `installment.load_templates(conn) -> list[dict]`（每项含 `template_id, template_type, max_periods, rate, fee, condition, cond`）
  - `installment.monthly_payment(principal: float, annual_rate: float, n: int) -> float`
  - `installment.Plan`：`template_id, template_type, periods, annual_rate, fee_rate, principal, monthly_payment, fee, total_cost, lump_sum, affordable, note`
  - `installment.PlanResult`：`customer_id, principal, overdue_ids, max_overdue_days, credit_score, affordable_monthly, affordability_source, eligible_templates, plans, conflicts, message, method`
  - `installment.build_plans(conn, customer_id, policy, *, allowed_templates=None, monthly_affordable=None, requested_periods=None, lump_sum_requested=False) -> PlanResult`
  - `loans.LoanItem`：`overdue_id, loan_id, loan_type, overdue_amount, overdue_days, overdue_periods, last_repay_date, recommended_template_id, owner_conflict`
  - `loans.LoanPriority`：`customer_id, items, total_amount, suggestion, basis, conflicts`
  - `loans.prioritize(conn, customer_id, policy) -> LoanPriority`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_installment.py`：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_installment.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'cagent.installment'`

- [ ] **Step 3: 实现 `installment.py`**

```python
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
```

- [ ] **Step 4: 实现 `loans.py`**

```python
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
    rows = conn.execute(
        "SELECT o.overdue_id, o.loan_id, l.loan_type, l.customer_id AS owner, o.overdue_amount, o.overdue_days, "
        "o.overdue_periods, o.last_repay_date, o.recommended_template_id FROM fact_overdue o "
        "LEFT JOIN dim_loan l ON l.loan_id = o.loan_id WHERE o.customer_id=? ORDER BY o.overdue_id", (customer_id,))
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
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_installment.py -v`
Expected: 6 passed

- [ ] **Step 6: Commit**

```powershell
git add backend/cagent/installment.py backend/cagent/loans.py backend/tests/test_installment.py
git commit -m "feat: installment plan calculation with template eligibility and loan priority"
```

---

### Task 8: 记忆与案件存储

**Files:**
- Create: `backend/cagent/memory.py`, `backend/cagent/cases.py`
- Test: `backend/tests/test_store.py`

**Interfaces:**
- Consumes: `audit.write_event`
- Produces:
  - `MemoryStore(conn)`：`set(scope, owner_id, key, value, source, ts=None)`、`get(scope, owner_id, key, default=None)`、`all(scope, owner_id) -> dict[str, dict]`（值为 `{"value", "source", "updated_at"}`）、`history(scope, owner_id, key) -> list[dict]`、`correct(scope, owner_id, key, value, operator, ts=None)`、`clear(scope, owner_id, key=None, operator="", ts=None) -> int`。`scope` 取 `session` 或 `customer`
  - `cases.CASE_STATUSES = ["待发起", "前置检查", "对话中", "人工接管中", "后置处理中", "已完成", "已归档"]`
  - `CaseStore(conn)`：
    - `create_case(customer_id, pack_id, now) -> str`、`set_case_status(case_id, status, now)`、`get_case(case_id) -> dict | None`、`search_cases(customer_id=None, status=None) -> list[dict]`、`archive_case(case_id, now)`
    - `open_session(case_id, customer_id, mode, pack_id, config_version, channel, now) -> str`、`get_session(session_id) -> dict | None`、`update_session(session_id, **fields)`（允许 `mode, ended_at, end_reason`）
    - `add_message(session_id, role, text, meta: dict, now) -> int`、`messages(session_id) -> list[dict]`（`meta` 已解析为 dict）、`context(session_id, keep_last=6) -> {"recent": list[dict], "summary": str}`
    - `add_artifact(case_id, session_id, kind, content, now) -> str`、`artifacts(case_id=None, session_id=None, kind=None) -> list[dict]`（`content` 已解析）、`latest_artifact(customer_id, kind) -> dict | None`
    - `add_todo(case_id, session_id, kind, content, due, now, todo_id=None) -> str`（同 `todo_id` 幂等）、`todos(case_id=None, session_id=None, status=None) -> list[dict]`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_store.py`：

```python
from datetime import datetime

import pytest

from cagent.cases import CaseStore
from cagent.memory import MemoryStore

NOW = datetime(2026, 10, 24, 10, 0)


def test_memory_set_get_correct_clear(db_conn):
    m = MemoryStore(db_conn)
    m.set("session", "S1", "promise", {"amount": 3000}, "第2轮")
    m.set("session", "S1", "promise", {"amount": 5000}, "第3轮")
    assert m.get("session", "S1", "promise") == {"amount": 5000}
    assert len(m.history("session", "S1", "promise")) == 2
    m.correct("session", "S1", "promise", {"amount": 4000}, operator="运营甲", ts=NOW)
    entry = m.all("session", "S1")["promise"]
    assert entry["value"] == {"amount": 4000} and entry["source"] == "人工纠正：运营甲"
    assert m.clear("session", "S1", "promise", operator="运营甲", ts=NOW) == 1
    assert m.get("session", "S1", "promise", "无") == "无"
    ops = db_conn.execute("SELECT scene FROM audit_log WHERE checkpoint='记忆操作' ORDER BY rowid").fetchall()
    assert [r["scene"] for r in ops] == ["纠正", "清除"]


def test_case_session_messages_and_context(db_conn):
    cs = CaseStore(db_conn)
    case_id = cs.create_case("C0001", "consumer_finance", NOW)
    sid = cs.open_session(case_id, "C0001", "运营模式", "consumer_finance", "v1", "外呼", NOW)
    for i in range(8):
        cs.add_message(sid, "客户" if i % 2 == 0 else "AI坐席", f"第{i}句", {"intent": "要求分期"} if i % 2 == 0 else {}, NOW)
    msgs = cs.messages(sid)
    assert [m["turn"] for m in msgs] == list(range(1, 9))
    ctx = cs.context(sid, keep_last=6)
    assert len(ctx["recent"]) == 6 and "要求分期" in ctx["summary"]
    cs.update_session(sid, mode="催收对话模式")
    assert cs.get_session(sid)["mode"] == "催收对话模式"
    with pytest.raises(ValueError):
        cs.update_session(sid, customer_id="C0002")


def test_case_status_artifacts_todos_archive(db_conn):
    cs = CaseStore(db_conn)
    case_id = cs.create_case("C0002", "consumer_finance", NOW)
    with pytest.raises(ValueError):
        cs.set_case_status(case_id, "不存在的状态", NOW)
    cs.set_case_status(case_id, "已完成", NOW)
    aid = cs.add_artifact(case_id, "S9", "催收报告", {"score": 60}, NOW)
    assert cs.artifacts(case_id=case_id)[0]["content"] == {"score": 60}
    assert cs.latest_artifact("C0002", "催收报告")["artifact_id"] == aid
    t1 = cs.add_todo(case_id, "S9", "承诺跟进", "跟进", "2026-10-30", NOW, todo_id="TD-S9-1")
    t2 = cs.add_todo(case_id, "S9", "承诺跟进", "跟进", "2026-10-30", NOW, todo_id="TD-S9-1")
    assert t1 == t2 and len(cs.todos(case_id=case_id)) == 1
    cs.archive_case(case_id, NOW)
    assert cs.get_case(case_id)["status"] == "已归档"
    assert cs.artifacts(case_id=case_id)[0]["archived"] == 1
    assert cs.search_cases(customer_id="C0002", status="已归档")[0]["case_id"] == case_id
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_store.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'cagent.memory'`

- [ ] **Step 3: 实现 `memory.py`**

```python
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from .audit import write_event


class MemoryStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def set(self, scope: str, owner_id: str, key: str, value, source: str, ts: datetime | None = None) -> None:
        stamp = (ts or datetime.now()).isoformat(timespec="seconds")
        self.conn.execute("UPDATE memory SET status='superseded' WHERE scope=? AND owner_id=? AND key=? AND status='active'",
                          (scope, owner_id, key))
        self.conn.execute("INSERT INTO memory(scope, owner_id, key, value, source, status, updated_at) "
                          "VALUES (?,?,?,?,?,'active',?)",
                          (scope, owner_id, key, json.dumps(value, ensure_ascii=False), source, stamp))
        self.conn.commit()

    def get(self, scope: str, owner_id: str, key: str, default=None):
        row = self.conn.execute("SELECT value FROM memory WHERE scope=? AND owner_id=? AND key=? AND status='active' "
                                "ORDER BY id DESC LIMIT 1", (scope, owner_id, key)).fetchone()
        return json.loads(row["value"]) if row else default

    def all(self, scope: str, owner_id: str) -> dict[str, dict]:
        rows = self.conn.execute("SELECT key, value, source, updated_at FROM memory WHERE scope=? AND owner_id=? "
                                 "AND status='active' ORDER BY id", (scope, owner_id))
        return {r["key"]: {"value": json.loads(r["value"]), "source": r["source"], "updated_at": r["updated_at"]}
                for r in rows}

    def history(self, scope: str, owner_id: str, key: str) -> list[dict]:
        rows = self.conn.execute("SELECT value, source, status, updated_at FROM memory WHERE scope=? AND owner_id=? "
                                 "AND key=? ORDER BY id", (scope, owner_id, key))
        return [{**dict(r), "value": json.loads(r["value"])} for r in rows]

    def correct(self, scope: str, owner_id: str, key: str, value, operator: str, ts: datetime | None = None) -> None:
        ts = ts or datetime.now()
        self.set(scope, owner_id, key, value, f"人工纠正：{operator}", ts)
        write_event(self.conn, checkpoint="记忆操作", customer_id=owner_id if scope == "customer" else None, scene="纠正",
                    content=f"{scope}/{owner_id}/{key} 改为 {json.dumps(value, ensure_ascii=False)}（{operator}）", ts=ts,
                    session_id=owner_id if scope == "session" else None)

    def clear(self, scope: str, owner_id: str, key: str | None = None, operator: str = "",
              ts: datetime | None = None) -> int:
        ts = ts or datetime.now()
        sql = "UPDATE memory SET status='cleared' WHERE scope=? AND owner_id=? AND status='active'"
        params: list = [scope, owner_id]
        if key:
            sql += " AND key=?"
            params.append(key)
        n = self.conn.execute(sql, params).rowcount
        self.conn.commit()
        write_event(self.conn, checkpoint="记忆操作", customer_id=owner_id if scope == "customer" else None, scene="清除",
                    content=f"{scope}/{owner_id}/{key or '全部'}（{operator}）", ts=ts,
                    session_id=owner_id if scope == "session" else None)
        return n
```

- [ ] **Step 4: 实现 `cases.py`**

```python
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime

CASE_STATUSES = ["待发起", "前置检查", "对话中", "人工接管中", "后置处理中", "已完成", "已归档"]
SESSION_FIELDS = {"mode", "ended_at", "end_reason"}


def _iso(ts: datetime) -> str:
    return ts.isoformat(timespec="seconds")


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class CaseStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create_case(self, customer_id: str, pack_id: str, now: datetime) -> str:
        case_id = _uid("CASE")
        self.conn.execute("INSERT INTO cases VALUES (?,?,?,?,?,?)",
                          (case_id, customer_id, pack_id, "待发起", _iso(now), _iso(now)))
        self.conn.commit()
        return case_id

    def set_case_status(self, case_id: str, status: str, now: datetime) -> None:
        if status not in CASE_STATUSES:
            raise ValueError(f"非法案件状态: {status}")
        self.conn.execute("UPDATE cases SET status=?, updated_at=? WHERE case_id=?", (status, _iso(now), case_id))
        self.conn.commit()

    def get_case(self, case_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        return dict(row) if row else None

    def search_cases(self, customer_id: str | None = None, status: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM cases WHERE 1=1", []
        if customer_id:
            sql += " AND customer_id=?"
            params.append(customer_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY created_at DESC", params)]

    def archive_case(self, case_id: str, now: datetime) -> None:
        self.set_case_status(case_id, "已归档", now)
        self.conn.execute("UPDATE artifacts SET archived=1 WHERE case_id=?", (case_id,))
        self.conn.commit()

    def open_session(self, case_id: str, customer_id: str, mode: str, pack_id: str, config_version: str,
                     channel: str, now: datetime) -> str:
        sid = _uid("S")
        self.conn.execute("INSERT INTO sessions(session_id, case_id, customer_id, mode, pack_id, config_version, channel, "
                          "started_at) VALUES (?,?,?,?,?,?,?,?)",
                          (sid, case_id, customer_id, mode, pack_id, config_version, channel, _iso(now)))
        self.conn.commit()
        return sid

    def get_session(self, session_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def update_session(self, session_id: str, **fields) -> None:
        bad = set(fields) - SESSION_FIELDS
        if bad:
            raise ValueError(f"不允许修改的会话字段: {sorted(bad)}")
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            self.conn.execute(f"UPDATE sessions SET {sets} WHERE session_id=?", [*fields.values(), session_id])
            self.conn.commit()

    def add_message(self, session_id: str, role: str, text: str, meta: dict, now: datetime) -> int:
        turn = self.conn.execute("SELECT COALESCE(MAX(turn), 0) + 1 FROM messages WHERE session_id=?",
                                 (session_id,)).fetchone()[0]
        self.conn.execute("INSERT INTO messages(session_id, turn, role, text, meta, created_at) VALUES (?,?,?,?,?,?)",
                          (session_id, turn, role, text, json.dumps(meta, ensure_ascii=False), _iso(now)))
        self.conn.commit()
        return turn

    def messages(self, session_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT turn, role, text, meta, created_at FROM messages WHERE session_id=? ORDER BY turn",
                                 (session_id,))
        return [{**dict(r), "meta": json.loads(r["meta"] or "{}")} for r in rows]

    def context(self, session_id: str, keep_last: int = 6) -> dict:
        msgs = self.messages(session_id)
        recent, older = msgs[-keep_last:], msgs[:-keep_last]
        parts = []
        for m in older:
            intent = m["meta"].get("intent")
            parts.append(f"第{m['turn']}句 {m['role']}{'（' + intent + '）' if intent else ''}：{m['text'][:20]}")
        return {"recent": [{"role": m["role"], "text": m["text"]} for m in recent], "summary": "；".join(parts)}

    def add_artifact(self, case_id: str | None, session_id: str | None, kind: str, content, now: datetime) -> str:
        aid = _uid("ART")
        self.conn.execute("INSERT INTO artifacts(artifact_id, case_id, session_id, kind, content, created_at) "
                          "VALUES (?,?,?,?,?,?)",
                          (aid, case_id, session_id, kind, json.dumps(content, ensure_ascii=False), _iso(now)))
        self.conn.commit()
        return aid

    def artifacts(self, case_id: str | None = None, session_id: str | None = None, kind: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM artifacts WHERE 1=1", []
        for col, val in (("case_id", case_id), ("session_id", session_id), ("kind", kind)):
            if val:
                sql += f" AND {col}=?"
                params.append(val)
        rows = self.conn.execute(sql + " ORDER BY created_at, rowid", params)
        return [{**dict(r), "content": json.loads(r["content"])} for r in rows]

    def latest_artifact(self, customer_id: str, kind: str) -> dict | None:
        row = self.conn.execute("SELECT a.* FROM artifacts a JOIN cases c ON c.case_id = a.case_id "
                                "WHERE c.customer_id=? AND a.kind=? ORDER BY a.created_at DESC, a.rowid DESC LIMIT 1",
                                (customer_id, kind)).fetchone()
        return {**dict(row), "content": json.loads(row["content"])} if row else None

    def add_todo(self, case_id: str | None, session_id: str | None, kind: str, content: str, due: str | None,
                 now: datetime, todo_id: str | None = None) -> str:
        todo_id = todo_id or _uid("TD")
        self.conn.execute("INSERT OR IGNORE INTO todos VALUES (?,?,?,?,?,?,?,?)",
                          (todo_id, case_id, session_id, kind, content, due, "待处理", _iso(now)))
        self.conn.commit()
        return todo_id

    def todos(self, case_id: str | None = None, session_id: str | None = None, status: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM todos WHERE 1=1", []
        for col, val in (("case_id", case_id), ("session_id", session_id), ("status", status)):
            if val:
                sql += f" AND {col}=?"
                params.append(val)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY created_at", params)]
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_store.py -v`
Expected: 3 passed

- [ ] **Step 6: Commit**

```powershell
git add backend/cagent/memory.py backend/cagent/cases.py backend/tests/test_store.py
git commit -m "feat: auditable memory store and case/session/artifact/todo storage"
```

---

### Task 9: 理解层（预处理、说话人判定、客户理解、运营意图）

**Files:**
- Create: `backend/cagent/agents/__init__.py`（空文件）, `backend/cagent/agents/understanding.py`
- Test: `backend/tests/test_understanding.py`

**Interfaces:**
- Consumes: `llm.GuardedLLM / LLMError`、`clock.cn_to_int`、`intents.yaml`
- Produces:
  - `SpeakerResult`：`speaker: "customer" | "agent" | "operator", confidence, method, needs_clarify`
  - `Understanding`：`customer_intent, emotion, emotion_intensity, amount, periods, date, monthly_affordable, confidence, conflict, method`
  - `OperatorIntent`：`intent, customer_id, confidence, method`
  - `regex_entities(text) -> dict`（键 `periods, monthly_affordable, amount, date`）
  - `Understander(llm: GuardedLLM, cfg: dict)`：`preprocess(text) -> tuple[str, str | None]`（问题取 `空输入 / 乱码 / 超长已截断`）、`speaker(text, scene_type=None) -> SpeakerResult`、`customer(text, context=None) -> Understanding`、`fallback_customer(text) -> Understanding`、`operator(text) -> OperatorIntent`
  - 大模型任务代号：`speaker`、`understand`、`operator_intent`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_understanding.py`：

```python
import pytest

from cagent import settings
from cagent.agents.understanding import Understander, regex_entities
from cagent.config_center import ConfigCenter
from cagent.llm import FakeLLM, GuardedLLM


@pytest.fixture
def cfg(db_conn):
    return ConfigCenter(db_conn, settings.CONFIG_DIR).get("intents")[0]


@pytest.fixture
def offline(db_conn, cfg):
    return Understander(GuardedLLM(None, db_conn), cfg)


def test_preprocess(offline):
    assert offline.preprocess("   ") == ("", "空输入")
    assert offline.preprocess("@@##$$%%^^&&**")[1] == "乱码"
    text, issue = offline.preprocess("好" * 600)
    assert issue == "超长已截断" and len(text) == 500
    assert offline.preprocess("能不能分期？") == ("能不能分期？", None)


def test_speaker_fallback_markers(offline):
    assert offline.speaker("你好，我是XX金融催收专员，需要核实还款计划").speaker == "agent"
    op = offline.speaker("请生成本月催收报告")
    assert op.speaker == "operator" and op.method == "关键词兜底"
    cu = offline.speaker("我最近手头紧")
    assert cu.speaker == "customer" and cu.needs_clarify
    assert offline.speaker("查询C0001的欠款").speaker == "operator"


def test_speaker_llm_and_low_confidence(db_conn, cfg):
    def responder(system, user):
        assert "任务代号：speaker" in system
        return {"speaker": "operator", "confidence": 0.9} if "报告" in user else {"speaker": "customer", "confidence": 0.3}

    u = Understander(GuardedLLM(FakeLLM(responder), db_conn), cfg)
    assert u.speaker("给我一份报告").method == "大模型"
    low = u.speaker("你今天不还就等着")
    assert low.speaker == "agent" and low.method == "关键词兜底"


def test_regex_entities():
    e = regex_entities("一次性还不上，能不能分十二期？每月能还1500元，下个月发工资")
    assert e == {"periods": 12, "monthly_affordable": 1500.0, "amount": 1500.0, "date": "下个月"}
    assert regex_entities("这是第3期的账单")["periods"] is None


def test_customer_fallback(offline):
    u = offline.customer("欠款太多了我一次性还不上，能不能分12期？")
    assert (u.customer_intent, u.periods, u.method) == ("要求分期", 12, "关键词兜底")
    assert offline.customer("你们天天打电话烦不烦！再打我就投诉").emotion == "愤怒"
    assert offline.customer("我会还，但是我不还").conflict
    assert offline.customer("那改成24期呢").customer_intent == "要求分期"


def test_customer_llm_output_normalized(db_conn, cfg):
    fake = FakeLLM(lambda s, u: {"customer_intent": "乱写", "emotion": "开心", "emotion_intensity": 3,
                                 "periods": 6, "confidence": 0.9})
    u = Understander(GuardedLLM(fake, db_conn), cfg).customer("想分12期")
    assert (u.customer_intent, u.emotion, u.emotion_intensity, u.periods) == ("要求分期", "平静", 1.0, 12)


def test_operator_intents(offline):
    q = offline.operator("现在晚上11点了，能给C0003打电话吗？")
    assert (q.intent, q.customer_id) == ("合规问答", "C0003")
    assert offline.operator("请生成本月催收报告").intent == "生成报告"
    assert offline.operator("对C0001发起催收").intent == "发起催收"
    assert offline.operator("你好").intent == "其他"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_understanding.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'cagent.agents'`

- [ ] **Step 3: 实现 `agents/understanding.py`**

```python
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
    "scene_type_hint 只是参考。\n"
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

    def speaker(self, text: str, scene_type: str | None = None) -> SpeakerResult:
        if self.llm.available:
            try:
                out = self.llm.json("speaker", SPEAKER_SYSTEM, {"text": text, "scene_type_hint": scene_type or ""})
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
            '输出 json，例如 {"customer_intent": "要求分期", "emotion": "焦虑", "emotion_intensity": 0.5, '
            '"monthly_affordable": null, "periods": 12, "amount": null, "date": null, "confidence": 0.85, "conflict": false}'
        )

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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_understanding.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```powershell
git add backend/cagent/agents backend/tests/test_understanding.py
git commit -m "feat: input preprocessing, speaker routing, customer and operator understanding with fallbacks"
```

---

### Task 10: 业务子 Agent（催收对话）

**Files:**
- Create: `backend/cagent/agents/collection.py`
- Test: `backend/tests/test_collection.py`

**Interfaces:**
- Consumes: Task 2–9 全部模块
- Produces:
  - `ACTION_NAMES: set[str]`（业务包 `intent_actions` 可引用的动作全集，Task 12 用于校验业务包）
  - `speech_vars(pack) -> dict`、`format_speech(pack, key, **extra) -> str`（缺失变量替换为空串；`key` 不存在时用 `default` 模板）
  - `Snapshot`：`customer_id, profile: dict（dim_customer 一行）, overdues: list[dict]（LoanItem）, contact_count_today: int, willingness: dict | None, degraded: list[str]`
  - `TurnOutput`：`reply, customer_intent, system_intent, emotion, emotion_intensity, stage, actions, compliance: list[CheckResult], willingness, strategy, plan, loans, handoff, handoff_reason, needs_review, review_reasons`
  - `CollectionAgent(*, conn, session_id, case_id, customer_id, pack, audit_version, policy, intents_cfg, engine, llm, understander, memory, cases, snapshot, verified)`：
    - `registry: Registry`（owner 为 `collection:<pack_id>`；工具 `get_my_overdues, get_my_templates, record_promise, record_grace_request, submit_waiver_request, reduce_contact, request_handoff`；Skill `intent_emotion, willingness_update, installment_plan, loan_priority, generate_reply, dialog_summary`；所有入参都不含 `customer_id`）
    - `stage: str`、`opening(now, trace) -> TurnOutput`、`handle(text, now, trace) -> TurnOutput`、`handoff_package(reason, now) -> dict`
  - 会话记忆键（`scope=session`）：`verified, intents, emotions, refusals, low_conf_turns, monthly_affordable, promises, grace_request, waivers, negotiation, willingness, strategy, pressure, contact_preference`；客户记忆键（`scope=customer`）：`complaints`
  - 大模型任务代号：`reply`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_collection.py`：

```python
from datetime import datetime

import pytest

from cagent import settings
from cagent.agents.collection import ACTION_NAMES, CollectionAgent, Snapshot, format_speech
from cagent.agents.understanding import Understander
from cagent.cases import CaseStore
from cagent.compliance import ComplianceEngine
from cagent.config_center import ConfigCenter
from cagent.llm import FakeLLM, GuardedLLM
from cagent.loans import prioritize
from cagent.memory import MemoryStore
from cagent.trace import Trace

NOW = datetime(2026, 10, 24, 10, 0)


def make_agent(conn, pack_id="auto_loan", customer_id="C0003", llm_client=None, verified=True):
    cc = ConfigCenter(conn, settings.CONFIG_DIR)
    pack, pv = cc.get("business_pack", pack_id)
    policy = cc.get("policy")[0]
    intents = cc.get("intents")[0]
    cp, cpv = cc.get("checkpoints")
    llm = GuardedLLM(llm_client, conn)
    cases = CaseStore(conn)
    case_id = cases.create_case(customer_id, pack_id, NOW)
    sid = cases.open_session(case_id, customer_id, "催收对话模式", pack_id, pv, "客户进线", NOW)
    profile = dict(conn.execute("SELECT * FROM dim_customer WHERE customer_id=?", (customer_id,)).fetchone())
    snapshot = Snapshot(customer_id=customer_id, profile=profile,
                        overdues=[i.model_dump() for i in prioritize(conn, customer_id, policy).items])
    return CollectionAgent(conn=conn, session_id=sid, case_id=case_id, customer_id=customer_id, pack=pack,
                           audit_version=f"{pv}/{cpv}", policy=policy, intents_cfg=intents,
                           engine=ComplianceEngine(conn, cp, cpv, llm), llm=llm, understander=Understander(llm, intents),
                           memory=MemoryStore(conn), cases=cases, snapshot=snapshot, verified=verified)


def scripted(drafts):
    it = iter(drafts)

    def responder(system, user):
        purpose = system.splitlines()[0].removeprefix("任务代号：")
        if purpose == "understand":
            return {"customer_intent": "要求分期", "emotion": "焦虑", "emotion_intensity": 0.4, "confidence": 0.9}
        if purpose == "reply":
            return {"reply": next(it)}
        return {"hits": []}

    return FakeLLM(responder)


def test_registry_isolated_and_covers_actions(db_conn):
    agent = make_agent(db_conn)
    assert ACTION_NAMES <= set(agent.registry.names())
    for spec in agent.registry.describe():
        assert "customer_id" not in spec["input"].get("properties", {})
    for pid in ("auto_loan", "consumer_finance"):
        pack = ConfigCenter(db_conn, settings.CONFIG_DIR).get("business_pack", pid)[0]
        assert {a for acts in pack["intent_actions"].values() for a in acts} <= ACTION_NAMES


def test_opening_unverified_starts_at_identity(db_conn):
    agent = make_agent(db_conn, verified=False)
    out = agent.opening(NOW, Trace())
    assert agent.stage == "核身" and "工号AI02" in out.reply and out.compliance[0].passed
    assert format_speech(agent.pack, "不存在的模板") == format_speech(agent.pack, "default")


def test_installment_negotiation_multi_turn(db_conn):
    agent = make_agent(db_conn)
    out = agent.handle("欠款太多了我一次性还不上，能不能分12期？", NOW, Trace())
    assert out.system_intent == "分期方案协商" and out.stage == "协商"
    assert out.plan.plans[0].template_id == "T004" and out.plan.plans[0].periods == 12
    assert "12期" in out.reply and out.compliance[-1].passed
    out2 = agent.handle("那改成24期呢", NOW, Trace())
    assert out2.plan.plans[0].periods == 24 and "24期" in out2.reply
    assert len(agent.memory.get("session", agent.session_id, "negotiation")) == 2
    assert db_conn.execute("SELECT COUNT(*) FROM audit_log WHERE session_id=?", (agent.session_id,)).fetchone()[0] >= 2
    assert len(agent.cases.messages(agent.session_id)) == 4


def test_multi_loan_priority(db_conn):
    out = make_agent(db_conn, pack_id="consumer_finance", customer_id="C0001").handle("我到底有几笔欠款？", NOW, Trace())
    assert out.system_intent == "多贷款管理" and out.loans.items[0].overdue_id == "O0033"
    assert "5笔" in out.reply


def test_handoff_on_request_and_repeated_refusal(db_conn):
    out = make_agent(db_conn).handle("不跟机器人说了，给我转人工", NOW, Trace())
    assert out.handoff and out.system_intent == "人工接管" and "转接人工" in out.reply
    agent = make_agent(db_conn)
    assert not agent.handle("没钱，不还", NOW, Trace()).handoff
    second = agent.handle("说了没钱，不还", NOW, Trace())
    assert second.handoff and "连续拒绝" in second.handoff_reason
    pkg = agent.handoff_package(second.handoff_reason, NOW)
    assert {"客户档案快照", "对话全文", "情绪轨迹", "还款意愿", "协商记录", "命中规则", "建议下一步"} <= set(pkg)
    assert "客户0003" not in str(pkg["客户档案快照"])


def test_garbled_input_asks_to_repeat(db_conn):
    out = make_agent(db_conn).handle("@@##$$%%^^&&**", NOW, Trace())
    assert "再说一遍" in out.reply and out.needs_review and not out.handoff


def test_draft_violation_rewritten(db_conn):
    agent = make_agent(db_conn, llm_client=scripted(["不还也没事，先放着吧", "可以为您办理分期，请问每月大概能还多少？"]))
    out = agent.handle("能不能分期", NOW, Trace())
    assert out.reply == "可以为您办理分期，请问每月大概能还多少？"
    assert [c.passed for c in out.compliance] == [False, True]
    assert out.compliance[0].hits[0].rule_id == "R008" and not out.handoff


def test_severe_or_pack_rule_forces_handoff(db_conn):
    out = make_agent(db_conn, llm_client=scripted(["再不还就安排拖车"])).handle("能不能分期", NOW, Trace())
    assert out.handoff and out.compliance[0].hits[0].rule_id == "BR-AUTO-01"
    assert "转接人工" in out.reply
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_collection.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'cagent.agents.collection'`

- [ ] **Step 3: 实现 `agents/collection.py`**

```python
from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from pydantic import BaseModel

from ..audit import write_audit
from ..cases import CaseStore
from ..compliance import CheckResult, ComplianceEngine
from ..desensitize import display_mask
from ..installment import PlanResult, build_plans, load_templates
from ..llm import GuardedLLM
from ..loans import LoanPriority, prioritize
from ..memory import MemoryStore
from ..registry import CallError, Registry
from ..scoring import Willingness, choose_strategy, max_overdue_days, score_customer
from ..trace import Trace
from .understanding import Understander, Understanding

ACTION_NAMES = {"installment_plan", "record_grace_request", "submit_waiver_request", "loan_priority",
                "record_promise", "reduce_contact", "request_handoff"}
_LUMP_SUM = re.compile(r"一次性(还清|结清|还完)")


class _Safe(dict):
    def __missing__(self, key):
        return ""


def speech_vars(pack: dict) -> dict:
    sp = pack["speech"]
    base = _Safe(agent_id=sp["agent_id"], wait_seconds=sp["wait_seconds"])
    return {**base, "identity": sp["identity"].format_map(base), "verify": sp["verify"], "purpose": sp["purpose"]}


def format_speech(pack: dict, key: str, **extra) -> str:
    templates = pack["speech"]["templates"]
    return (templates.get(key) or templates["default"]).format_map(_Safe({**speech_vars(pack), **extra}))


class Snapshot(BaseModel):
    customer_id: str
    profile: dict = {}
    overdues: list[dict] = []
    contact_count_today: int = 0
    willingness: dict | None = None
    degraded: list[str] = []


class TurnOutput(BaseModel):
    reply: str
    customer_intent: str
    system_intent: str
    emotion: str
    emotion_intensity: float
    stage: str
    actions: list[str] = []
    compliance: list[CheckResult] = []
    willingness: Willingness | None = None
    strategy: str | None = None
    plan: PlanResult | None = None
    loans: LoanPriority | None = None
    handoff: bool = False
    handoff_reason: str | None = None
    needs_review: bool = False
    review_reasons: list[str] = []


class Empty(BaseModel):
    pass


class UnderstandIn(BaseModel):
    text: str
    context: dict = {}


class WillIn(BaseModel):
    intents: list[str] = []


class PlanIn(BaseModel):
    monthly_affordable: float | None = None
    requested_periods: int | None = None
    lump_sum: bool = False


class PromiseIn(BaseModel):
    amount: float | None = None
    date: str | None = None


class GraceIn(BaseModel):
    date: str | None = None
    reason: str = ""


class WaiverIn(BaseModel):
    scope: str = ""


class HandoffIn(BaseModel):
    reason: str


class ReplyIn(BaseModel):
    payload: dict
    rewrite_hint: str = ""


class ReplyOut(BaseModel):
    reply: str
    source: str = "大模型"


class AckOut(BaseModel):
    ok: bool = True
    detail: dict = {}


class TemplatesOut(BaseModel):
    templates: list[dict]


class OverduesOut(BaseModel):
    items: list[dict]


class CollectionAgent:
    def __init__(self, *, conn: sqlite3.Connection, session_id: str, case_id: str, customer_id: str, pack: dict,
                 audit_version: str, policy: dict, intents_cfg: dict, engine: ComplianceEngine, llm: GuardedLLM,
                 understander: Understander, memory: MemoryStore, cases: CaseStore, snapshot: Snapshot,
                 verified: bool):
        self.conn, self.session_id, self.case_id, self.customer_id = conn, session_id, case_id, customer_id
        self.pack, self.audit_version, self.policy, self.intents = pack, audit_version, policy, intents_cfg
        self.engine, self.llm, self.understander = engine, llm, understander
        self.memory, self.cases, self.snapshot = memory, cases, snapshot
        self.allowed_templates = (pack.get("skill_params", {}).get("installment_plan") or {}).get("templates")
        first = pack["stages"][0]
        self.stage = first["next"] if verified and first.get("next") else first["id"]
        self._set("verified", verified, "会话初始化")
        self.registry = self._build_registry()

    def _mem(self, key: str, default=None):
        return self.memory.get("session", self.session_id, key, default)

    def _set(self, key: str, value, source: str) -> None:
        self.memory.set("session", self.session_id, key, value, source)

    def _append(self, key: str, item, source: str) -> None:
        self._set(key, [*self._mem(key, []), item], source)

    def _turn(self) -> int:
        return len(self.cases.messages(self.session_id)) // 2 + 1

    def _build_registry(self) -> Registry:
        r = Registry(f"collection:{self.pack['pack_id']}")
        conn, cid, policy = self.conn, self.customer_id, self.policy
        r.register("get_my_overdues", lambda a: OverduesOut(items=[i.model_dump() for i in prioritize(conn, cid, policy).items]),
                   Empty, OverduesOut, kind="tool", description="查询当前客户的全部逾期记录")
        r.register("get_my_templates", lambda a: TemplatesOut(templates=[
            t for t in load_templates(conn) if self.allowed_templates is None or t["template_id"] in self.allowed_templates]),
                   Empty, TemplatesOut, kind="tool", description="查询本业务可用的分期模板")
        r.register("record_promise", self._record_promise, PromiseIn, AckOut, kind="tool",
                   description="记录客户的还款承诺", retries=0)
        r.register("record_grace_request", self._record_grace, GraceIn, AckOut, kind="tool",
                   description="记录客户的宽限请求", retries=0)
        r.register("submit_waiver_request", self._submit_waiver, WaiverIn, AckOut, kind="tool",
                   description="提交减免申请（只登记，不承诺结果）", retries=0)
        r.register("reduce_contact", self._reduce_contact, Empty, AckOut, kind="tool",
                   description="记录客户降低联系频次的诉求", retries=0)
        r.register("request_handoff", lambda a: AckOut(detail={"reason": a.reason}), HandoffIn, AckOut, kind="tool",
                   description="向主控申请转人工")
        r.register("intent_emotion", lambda a: self.understander.customer(a.text, a.context), UnderstandIn, Understanding,
                   kind="skill", description="识别客户意图、情绪与关键信息", timeout_s=40, retries=0)
        r.register("willingness_update", lambda a: score_customer(conn, cid, policy, a.intents), WillIn, Willingness,
                   kind="skill", description="结合本次对话表态更新还款意愿评分")
        r.register("installment_plan", lambda a: build_plans(
            conn, cid, policy, allowed_templates=self.allowed_templates, monthly_affordable=a.monthly_affordable,
            requested_periods=a.requested_periods, lump_sum_requested=a.lump_sum),
                   PlanIn, PlanResult, kind="skill", description="按适用条件生成或调整分期方案")
        r.register("loan_priority", lambda a: prioritize(conn, cid, policy), Empty, LoanPriority, kind="skill",
                   description="当前客户多笔逾期的处理优先级")
        r.register("generate_reply", self._generate_reply, ReplyIn, ReplyOut, kind="skill",
                   description="按阶段目标与事实生成坐席话术", timeout_s=40, retries=0)
        r.register("dialog_summary", lambda a: ReplyOut(reply=self.cases.context(self.session_id)["summary"], source="规则"),
                   Empty, ReplyOut, kind="skill", description="对话小结")
        return r

    def _record_promise(self, a: PromiseIn) -> AckOut:
        item = {"amount": a.amount, "date": a.date, "turn": self._turn()}
        self._append("promises", item, "record_promise")
        return AckOut(detail=item)

    def _record_grace(self, a: GraceIn) -> AckOut:
        item = {"date": a.date, "reason": a.reason, "turn": self._turn()}
        self._set("grace_request", item, "record_grace_request")
        return AckOut(detail=item)

    def _submit_waiver(self, a: WaiverIn) -> AckOut:
        item = {"request_id": f"WV-{self.session_id}-{len(self._mem('waivers', [])) + 1}", "scope": a.scope,
                "status": "已提交待审批"}
        self._append("waivers", item, "submit_waiver_request")
        return AckOut(detail=item)

    def _reduce_contact(self, a: Empty) -> AckOut:
        self._set("contact_preference", "降低联系频次", "reduce_contact")
        complaints = self.memory.get("customer", self.customer_id, "complaints", [])
        complaints.append({"session_id": self.session_id, "content": "要求降低联系频次"})
        self.memory.set("customer", self.customer_id, "complaints", complaints, "reduce_contact")
        return AckOut(detail={"contact_preference": "降低联系频次"})

    def opening(self, now: datetime, trace: Trace) -> TurnOutput:
        reply = format_speech(self.pack, "opening")
        checks = [self._check(reply, now, trace, semantic=False)]
        self.cases.add_message(self.session_id, "AI坐席", reply, {"stage": self.stage}, now)
        return TurnOutput(reply=reply, customer_intent="其他", system_intent="提醒催收", emotion="平静",
                          emotion_intensity=0.0, stage=self.stage, compliance=checks)

    def handle(self, text: str, now: datetime, trace: Trace) -> TurnOutput:
        clean, issue = self.understander.preprocess(text)
        review: list[str] = []
        if issue:
            trace.add("understand", "输入预处理", "warn", issue=issue)
            review.append(f"输入异常：{issue}")
        if issue in ("空输入", "乱码"):
            reply = format_speech(self.pack, "clarify")
            checks = [self._check(reply, now, trace, semantic=False)]
            self._log_turn(text, reply, {"issue": issue}, now)
            return TurnOutput(reply=reply, customer_intent="其他",
                              system_intent=self.intents["customer_to_system"].get("其他", "提醒催收"), emotion="平静",
                              emotion_intensity=0.0, stage=self.stage, compliance=checks, needs_review=True,
                              review_reasons=review)
        ctx = self.cases.context(self.session_id)
        try:
            u = self.registry.call("intent_emotion", trace, text=clean,
                                   context={"summary": ctx["summary"], "recent": ctx["recent"][-4:]})
        except CallError as e:
            u = self.understander.fallback_customer(clean)
            trace.add("understand", "意图识别降级", "warn", reason=e.reason)
        trace.add("understand", "意图情绪识别", intent=u.customer_intent, emotion=u.emotion,
                  intensity=u.emotion_intensity, confidence=u.confidence, method=u.method)
        cp3 = self.engine.check_customer_input(clean)
        trace.add("guard", "检查点三：客户输入", signals=cp3.signals)
        if u.confidence < self.intents.get("low_confidence", 0.6) or u.conflict:
            review.append("意图识别置信度低或存在意图冲突，需人工复核")
        self._update_memory(u, clean)
        self.stage = self._next_stage(u.customer_intent)
        actions = self.pack["intent_actions"].get(u.customer_intent, [])
        trace.add("decide", "阶段与动作", stage=self.stage, actions=actions)
        done: list[str] = []
        plan: PlanResult | None = None
        loans: LoanPriority | None = None
        for name in actions:
            try:
                out = self.registry.call(name, trace, **self._action_args(name, u, clean))
            except CallError as e:
                review.append(f"{name} 调用失败：{e.reason}")
                continue
            done.append(name)
            if isinstance(out, PlanResult):
                plan = out
                review += out.conflicts
                self._record_negotiation(out, clean)
            elif isinstance(out, LoanPriority):
                loans = out
                review += out.conflicts
        will, strategy = None, None
        try:
            will = self.registry.call("willingness_update", trace, intents=self._mem("intents", []))
            strategy = choose_strategy(self.policy, will.level, max_overdue_days(self.conn, self.customer_id))
            self._set("willingness", will.model_dump(mode="json"), "willingness_update")
            self._set("strategy", strategy, "策略表")
            review += will.review_reasons
        except CallError as e:
            review.append(f"意愿评分失败：{e.reason}")
        system_intent = self.intents["customer_to_system"].get(u.customer_intent, "提醒催收")
        handoff_reason = self._handoff_reason(u, cp3, done)
        facts = self._facts(plan, loans, will)
        if handoff_reason:
            system_intent = "人工接管"
            reply = format_speech(self.pack, "handoff")
            checks = [self._check(reply, now, trace, semantic=False)]
        else:
            reply, checks, forced = self._compose(clean, u, system_intent, facts, now, trace)
            if forced:
                handoff_reason, system_intent = forced, "人工接管"
        if any(c.degraded for c in checks):
            review.append("语义合规判定不可用，已按关键词规则判定")
        self._log_turn(text, reply, {"intent": u.customer_intent, "system_intent": system_intent, "emotion": u.emotion,
                                     "actions": done}, now)
        self._bump_pressure(reply)
        trace.add("reply", "坐席回复", system_intent=system_intent, handoff=bool(handoff_reason))
        return TurnOutput(reply=reply, customer_intent=u.customer_intent, system_intent=system_intent, emotion=u.emotion,
                          emotion_intensity=u.emotion_intensity, stage=self.stage, actions=done, compliance=checks,
                          willingness=will, strategy=strategy, plan=plan, loans=loans, handoff=bool(handoff_reason),
                          handoff_reason=handoff_reason, needs_review=bool(review),
                          review_reasons=list(dict.fromkeys(review)))

    def _update_memory(self, u: Understanding, text: str) -> None:
        intents = self._mem("intents", [])
        if u.customer_intent not in intents:
            self._set("intents", [*intents, u.customer_intent], "意图识别")
        self._append("emotions", {"turn": self._turn(), "emotion": u.emotion, "intensity": u.emotion_intensity,
                                  "text": text[:40]}, "情绪识别")
        self._set("refusals", self._mem("refusals", 0) + 1 if u.customer_intent == "拒绝还款" else 0, "意图识别")
        low = u.confidence < self.intents.get("low_confidence", 0.6)
        self._set("low_conf_turns", self._mem("low_conf_turns", 0) + 1 if low else 0, "意图识别")
        if u.monthly_affordable:
            self._set("monthly_affordable", u.monthly_affordable, "客户表述")
        if self.stage == self.pack["stages"][0]["id"] and re.search(r"(?<!\d)\d{4}(?!\d)", text):
            self._set("verified", True, "客户提供身份证后四位")

    def _next_stage(self, intent: str) -> str:
        target = self.pack["stage_advance"].get(intent)
        if target:
            return target
        first = self.pack["stages"][0]
        if self.stage == first["id"] and self._mem("verified") and first.get("next"):
            return first["next"]
        return self.stage

    def _action_args(self, name: str, u: Understanding, text: str) -> dict:
        if name == "installment_plan":
            return {"monthly_affordable": u.monthly_affordable or self._mem("monthly_affordable"),
                    "requested_periods": u.periods,
                    "lump_sum": u.customer_intent == "要求减免" or bool(_LUMP_SUM.search(text))}
        if name == "record_promise":
            return {"amount": u.amount, "date": u.date}
        if name == "record_grace_request":
            return {"date": u.date, "reason": text[:60]}
        if name == "submit_waiver_request":
            return {"scope": text[:60]}
        if name == "request_handoff":
            return {"reason": "客户要求转人工"}
        return {}

    def _record_negotiation(self, plan: PlanResult, text: str) -> None:
        self._append("negotiation", {
            "turn": self._turn(), "客户诉求": text[:60],
            "方案": [f"{p.template_id} {p.template_type} {p.periods}期 月供{p.monthly_payment:.2f}元 "
                     f"年化{p.annual_rate * 100:.2f}% 手续费{p.fee:.2f}元" for p in plan.plans],
            "结果": plan.message or ("已提供方案" if plan.plans else "无可用方案"),
        }, "installment_plan")

    def _handoff_reason(self, u: Understanding, cp3: CheckResult, done: list[str]) -> str | None:
        h = self.pack["handoff"]
        if "request_handoff" in done or "要求转人工" in cp3.signals:
            return "客户要求转人工"
        if self._mem("refusals", 0) >= h["max_refusals"]:
            return f"客户连续拒绝{self._mem('refusals')}次"
        emo = h["emotion"]
        if u.emotion in emo["types"] and u.emotion_intensity >= emo["min_intensity"]:
            return f"客户情绪{u.emotion}（强度{u.emotion_intensity:.2f}）超过阈值"
        if self._mem("low_conf_turns", 0) >= h["low_confidence_turns"]:
            return "识别置信度连续偏低"
        return None

    def _facts(self, plan: PlanResult | None, loans: LoanPriority | None, will: Willingness | None) -> dict:
        ods = self.snapshot.overdues
        f: dict = {"欠款总额": f"{sum(o.get('overdue_amount') or 0 for o in ods):.0f}元", "逾期笔数": len(ods)}
        if ods:
            f["最长逾期"] = f"{max(o.get('overdue_days') or 0 for o in ods)}天"
        if plan:
            f["欠款总额"] = f"{plan.principal:.0f}元"
            f["分期方案"] = [{"模板": p.template_id, "类型": p.template_type, "期数": p.periods,
                          "月供": f"{p.monthly_payment:.2f}元", "年化利率": f"{p.annual_rate * 100:.2f}%",
                          "手续费": f"{p.fee:.2f}元", "总成本": f"{p.total_cost:.2f}元", "说明": p.note}
                         for p in plan.plans]
            f["方案口径"] = plan.method
            if plan.message:
                f["方案提示"] = plan.message
        if loans:
            f["逾期明细"] = [{"编号": i.overdue_id, "贷款类型": i.loan_type or "未知", "逾期天数": i.overdue_days,
                          "欠款": f"{i.overdue_amount:.0f}元"} for i in loans.items]
            f["优先建议"] = loans.suggestion
        if self._mem("waivers"):
            f["减免申请"] = "已提交，结果以审批为准"
        if self._mem("grace_request"):
            f["宽限请求"] = "已记录"
        if self._mem("promises"):
            f["已确认承诺"] = [{"金额": f"{p['amount']:.0f}元" if p.get("amount") else None, "日期": p.get("date")}
                          for p in self._mem("promises")]
        if will:
            f["还款意愿等级"] = will.level
        return f

    def _compose(self, text: str, u: Understanding, system_intent: str, facts: dict, now: datetime,
                 trace: Trace) -> tuple[str, list[CheckResult], str | None]:
        max_rw = self.engine.cfg["checkpoints"]["agent_speech"].get("max_rewrites", 2)
        checks: list[CheckResult] = []
        hint = ""
        for _ in range(max_rw + 1):
            draft = self._draft(text, u, system_intent, facts, hint, trace)
            r = self._check(draft, now, trace)
            checks.append(r)
            if r.passed:
                return draft, checks, None
            if r.handoff:
                reply = format_speech(self.pack, "handoff")
                checks.append(self._check(reply, now, trace, semantic=False))
                return reply, checks, "坐席话术命中严重级规则：" + "、".join(h.rule_id for h in r.hits)
            hint = ("上一版话术违反：" + "；".join(f"{h.rule_id} {h.rule_content}（{h.evidence}）" for h in r.hits)
                    + "。请改写为合规表述，保留业务信息。")
            trace.add("guard", "话术改写", "retry", hint=hint)
            if not self.llm.available:
                break
        fallback = self.pack["speech"]["rewrite_fallback"]
        r = self._check(fallback, now, trace, semantic=False)
        checks.append(r)
        if r.passed:
            return fallback, checks, None
        reply = format_speech(self.pack, "handoff")
        checks.append(self._check(reply, now, trace, semantic=False))
        return reply, checks, "话术多次改写仍不合规"

    def _draft(self, text: str, u: Understanding, system_intent: str, facts: dict, hint: str, trace: Trace) -> str:
        ctx = self.cases.context(self.session_id)
        payload = {
            "system_intent": system_intent, "customer_intent": u.customer_intent,
            "stage_goal": next((s["goal"] for s in self.pack["stages"] if s["id"] == self.stage), ""),
            "客户说": text, "客户情绪": f"{u.emotion}（{u.emotion_intensity:.2f}）", "事实": facts,
            "对话摘要": ctx["summary"], "最近对话": ctx["recent"],
        }
        try:
            return self.registry.call("generate_reply", trace, payload=payload, rewrite_hint=hint).reply
        except CallError as e:
            trace.add("reply", "话术生成降级", "warn", reason=e.reason)
            return self._template_reply(u.customer_intent, system_intent, facts)

    def _reply_system(self, stage_goal: str) -> str:
        sp = self.pack["speech"]
        return (
            f"你是{self.pack['name']}的 AI 坐席，工号{sp['agent_id']}，正在和客户本人通话。语气：{sp['tone']}。\n"
            f"当前阶段目标：{stage_goal}。\n"
            "硬性要求：1. 金额、期数、利率、日期只能引用“事实”里的数据，不得编造；"
            "2. 不得承诺减免、免息、延期的审批结果，只能说提交申请、以审批为准；"
            "3. 不得威胁、辱骂、冒充公检法、提及联系客户亲友，不得说不还款没有后果；"
            f"4. 禁用词：{'、'.join(sp.get('forbidden_words', []))}；"
            "5. 尖括号占位符（如 <AMOUNT_1>）原样保留；6. 不超过 120 字，先回应客户，再推进阶段目标，以一个问题结尾；"
            "7. 若有“改写要求”，必须按要求修正。\n"
            '输出 json，例如 {"reply": "……"}'
        )

    def _generate_reply(self, a: ReplyIn) -> ReplyOut:
        p = a.payload
        if not self.llm.available:
            return ReplyOut(reply=self._template_reply(p["customer_intent"], p["system_intent"], p["事实"]), source="模板")
        user = {**p, "改写要求": a.rewrite_hint} if a.rewrite_hint else p
        out = self.llm.json("reply", self._reply_system(p["stage_goal"]), user,
                            names=[self.snapshot.profile.get("customer_name") or ""])
        reply = str(out.get("reply") or "").strip()
        if not reply:
            raise ValueError("模型返回空话术")
        return ReplyOut(reply=reply[:300])

    def _template_reply(self, customer_intent: str, system_intent: str, facts: dict) -> str:
        plans = facts.get("分期方案") or []
        plan_brief = "；".join(f"{p['类型']}{p['期数']}期，月供{p['月供']}，年化利率{p['年化利率']}，手续费{p['手续费']}"
                               for p in plans) or facts.get("方案提示") or "可以根据您的情况再为您测算"
        details = facts.get("逾期明细") or []
        loan_list = "；".join(f"{n}. {x['贷款类型']}逾期{x['逾期天数']}天，欠款{x['欠款']}（{x['编号']}）"
                              for n, x in enumerate(details, 1))
        promises = facts.get("已确认承诺") or []
        last = promises[-1] if promises else {}
        promise_brief = "，".join(v for v in (last.get("日期"), last.get("金额")) if v) or "按约定时间还款"
        templates = self.pack["speech"]["templates"]
        key = next((k for k in (customer_intent, system_intent) if k in templates), "default")
        return format_speech(self.pack, key, plan_brief=plan_brief, principal=facts.get("欠款总额", ""),
                             loan_count=len(details), loan_list=loan_list, suggestion=facts.get("优先建议", ""),
                             promise_brief=promise_brief)

    def _check(self, text: str, now: datetime, trace: Trace, semantic: bool = True) -> CheckResult:
        r = self.engine.check_agent_speech(text, extra_rules=self.pack.get("extra_rules", []),
                                           pressure_so_far=self._mem("pressure", 0), use_semantic=semantic)
        write_audit(self.conn, result=r, customer_id=self.customer_id, scene=self.pack["name"], content=text,
                    config_version=self.audit_version, ts=now, session_id=self.session_id)
        trace.add("guard", "检查点一：坐席话术", "ok" if r.passed else "warn",
                  hits=[f"{h.rule_id}:{h.evidence}" for h in r.hits], degraded=r.degraded)
        return r

    def _bump_pressure(self, reply: str) -> None:
        n = sum(len(re.findall(p, reply)) for det in self.engine.cfg["detectors"].values()
                for p in det.get("pressure_patterns", []))
        if n:
            self._set("pressure", self._mem("pressure", 0) + n, "施压计数")

    def _log_turn(self, text: str, reply: str, meta: dict, now: datetime) -> None:
        self.cases.add_message(self.session_id, "客户", text, meta, now)
        self.cases.add_message(self.session_id, "AI坐席", reply, {"stage": self.stage}, now)

    def handoff_package(self, reason: str, now: datetime) -> dict:
        prof = self.snapshot.profile
        hits = [dict(r) for r in self.conn.execute(
            "SELECT rule_id, rule_type, level, action, ts FROM audit_log WHERE session_id=? AND rule_id IS NOT NULL "
            "ORDER BY rowid", (self.session_id,))]
        return {
            "转接原因": reason, "转接时间": now.isoformat(timespec="seconds"), "会话编号": self.session_id,
            "业务包": self.pack["name"],
            "客户档案快照": {"客户编号": self.customer_id, "姓名": display_mask(prof.get("customer_name") or ""),
                       "职业": prof.get("occupation"), "月收入": prof.get("monthly_income"),
                       "信用评分": prof.get("credit_score"), "客户等级": prof.get("customer_level"),
                       "逾期": self.snapshot.overdues, "前置工具降级": self.snapshot.degraded},
            "对话全文": [{"轮次": m["turn"], "角色": m["role"], "内容": display_mask(m["text"])}
                     for m in self.cases.messages(self.session_id)],
            "情绪轨迹": self._mem("emotions", []), "还款意愿": self._mem("willingness"),
            "协商记录": self._mem("negotiation", []), "已确认承诺": self._mem("promises", []),
            "减免申请": self._mem("waivers", []), "宽限请求": self._mem("grace_request"), "命中规则": hits,
            "建议下一步": self._mem("strategy") or "由坐席确认还款安排",
        }
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_collection.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```powershell
git add backend/cagent/agents/collection.py backend/tests/test_collection.py
git commit -m "feat: business sub-agent with stage flow, skills, checkpoint-gated replies and handoff package"
```

---

### Task 11: 催收报告与异步后置任务

**Files:**
- Create: `backend/cagent/reports.py`, `backend/cagent/postcall.py`
- Test: `backend/tests/test_reports_postcall.py`

**Interfaces:**
- Consumes: `cases.CaseStore`、`memory.MemoryStore`、`loans.prioritize`、`scoring.*`、`config_center.ConfigCenter`、`db.insert_generated`
- Produces:
  - `reports.session_memory(conn, session_id) -> dict`（会话记忆键到值）
  - `reports.session_report(conn, session_id, policy, now=None) -> dict`、`reports.customer_report(conn, customer_id, policy, now=None) -> dict`（有最近报告时取最近报告，否则生成对话前预报告）
  - 报告字典顶层键：`报告类型, 客户编号, 会话编号, 生成时间, 逾期概况, 还款意愿评估, 情绪感知, 合规审计, 协商记录, 建议策略, 数据冲突`
  - `reports.render_report_markdown(report, sections) -> str`、`reports.render_customer_text(report) -> str`
  - `reports.summary_report(conn, now, policy, month=None) -> dict`（键 `报告类型, 统计月份, 生成时间, 说明, 本期指标, 全量指标, 实际还款, 意愿分布, 合规审计, 人工接管次数`）、`reports.render_summary_text(report) -> str`
  - `postcall.TASKS: dict[str, Callable[[PostCtx], None]]`、`postcall.STAGES: dict[str, int]`
  - `postcall.PostCallRunner(db_path, config_dir, max_retries=2)`：`enqueue(session_id, sync=False)`、`status(session_id) -> list[dict]`、`retry(session_id, name=None, sync=False)`
  - 任务状态取值：`等待 / 执行中 / 完成 / 失败`；产出物种类 `催收报告`（内容 `{"data", "markdown"}`）；待办种类 `承诺跟进 / 减免审批 / 宽限跟进 / 下次可联系 / 后置任务失败`
  - 生成记录编号：通话 `CH-<session_id>`，行为 `B-<session_id>-<n>`，情绪 `E-<session_id>-<峰值|结束>`
  - 约定：主控在会话被门禁拦截时写会话记忆 `gate = {"rule_ids": [...], "next_allowed": str | None}`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_reports_postcall.py`：

```python
from datetime import datetime

from cagent import db, postcall, settings
from cagent.cases import CaseStore
from cagent.config_center import ConfigCenter
from cagent.memory import MemoryStore
from cagent.postcall import PostCallRunner
from cagent.reports import (customer_report, render_report_markdown, render_summary_text, session_report,
                            summary_report)

NOW = datetime(2026, 10, 24, 10, 0)


def policy_of(conn):
    return ConfigCenter(conn, settings.CONFIG_DIR).get("policy")[0]


def make_session(db_path):
    conn = db.connect(db_path)
    cases, mem = CaseStore(conn), MemoryStore(conn)
    case_id = cases.create_case("C0003", "auto_loan", NOW)
    sid = cases.open_session(case_id, "C0003", "催收对话模式", "auto_loan", "v", "外呼", NOW)
    cases.add_message(sid, "AI坐席", "您好，我是贷后服务专员", {}, NOW)
    cases.add_message(sid, "客户", "下个月10号还5000元", {"intent": "承诺还款"}, NOW)
    mem.set("session", sid, "intents", ["要求分期", "承诺还款"], "测试")
    mem.set("session", sid, "emotions", [{"turn": 1, "emotion": "焦虑", "intensity": 0.6, "text": "一次性还不上"},
                                         {"turn": 2, "emotion": "配合", "intensity": 0.3, "text": "下个月10号还"}], "测试")
    mem.set("session", sid, "promises", [{"amount": 5000, "date": "下个月10号", "turn": 2}], "测试")
    mem.set("session", sid, "negotiation", [{"turn": 1, "客户诉求": "分12期", "方案": ["T004 标准分期 12期"], "结果": "已提供方案"}], "测试")
    cases.update_session(sid, ended_at=NOW.isoformat(timespec="seconds"), end_reason="正常结束")
    conn.close()
    return sid, case_id


def test_session_and_customer_reports(db_conn):
    cid_report = customer_report(db_conn, "C0003", policy_of(db_conn), NOW)
    assert cid_report["会话编号"] is None and cid_report["逾期概况"]["逾期笔数"] == 1
    md = render_report_markdown(cid_report, ["逾期概况", "还款意愿评估", "合规审计"])
    assert "## 合规审计" in md and "## 数据冲突" in md


def test_summary_report_falls_back_to_latest_month(db_conn):
    rep = summary_report(db_conn, NOW, policy_of(db_conn))
    assert rep["统计月份"] == "2025-10" and rep["本期指标"]["催收次数"] == 2 and rep["说明"]
    assert "催收次数" in render_summary_text(rep)


def test_postcall_writes_records_idempotently(db_path):
    sid, case_id = make_session(db_path)
    runner = PostCallRunner(db_path, settings.CONFIG_DIR)
    runner.enqueue(sid, sync=True)
    assert {t["status"] for t in runner.status(sid)} == {"完成"}
    for name in postcall.STAGES:
        runner.retry(sid, name=name, sync=True)
    runner.enqueue(sid, sync=True)
    conn = db.connect(db_path)
    call = conn.execute("SELECT * FROM fact_call_history WHERE session_id=?", (sid,)).fetchall()
    assert len(call) == 1 and call[0]["call_id"] == f"CH-{sid}" and call[0]["call_result"] == "承诺还款"
    assert call[0]["source"] == db.GENERATED
    assert conn.execute("SELECT COUNT(*) FROM fact_emotion_log WHERE session_id=?", (sid,)).fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM profile_snapshots WHERE session_id=?", (sid,)).fetchone()[0] == 1
    cases = CaseStore(conn)
    assert len(cases.artifacts(session_id=sid, kind="催收报告")) == 1
    assert [t["kind"] for t in cases.todos(session_id=sid)] == ["承诺跟进"]
    assert cases.get_case(case_id)["status"] == "已完成"
    rep = session_report(conn, sid, policy_of(conn), NOW)
    assert rep["协商记录"]["还款承诺"][0]["amount"] == 5000
    conn.close()


def test_postcall_single_failure_isolated(db_path, monkeypatch):
    sid, case_id = make_session(db_path)

    def boom(ctx):
        raise RuntimeError("模拟失败")

    monkeypatch.setitem(postcall.TASKS, "write_emotion", boom)
    runner = PostCallRunner(db_path, settings.CONFIG_DIR, max_retries=1)
    runner.enqueue(sid, sync=True)
    status = {t["name"]: t for t in runner.status(sid)}
    assert status["write_emotion"]["status"] == "失败" and status["write_emotion"]["attempts"] == 2
    assert status["write_call_record"]["status"] == "完成" and status["create_todos"]["status"] == "完成"
    conn = db.connect(db_path)
    cases = CaseStore(conn)
    assert "后置任务失败" in [t["kind"] for t in cases.todos(session_id=sid)]
    assert cases.get_case(case_id)["status"] == "后置处理中"
    conn.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_reports_postcall.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'cagent.postcall'`

- [ ] **Step 3: 实现 `reports.py`**

```python
from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime

from .cases import CaseStore
from .loans import prioritize
from .memory import MemoryStore
from .scoring import choose_strategy, max_overdue_days, score_customer

EMPTY = (None, "", [], {})


def session_memory(conn: sqlite3.Connection, session_id: str) -> dict:
    return {k: v["value"] for k, v in MemoryStore(conn).all("session", session_id).items()}


def _hits(conn: sqlite3.Connection, where: str, params: tuple) -> list[dict]:
    return [dict(r) for r in conn.execute(
        f"SELECT rule_id, rule_type, level, action, checkpoint, ts FROM audit_log WHERE rule_id IS NOT NULL AND {where} "
        "ORDER BY rowid", params)]


def _build(conn: sqlite3.Connection, customer_id: str, policy: dict, now: datetime, session: dict | None,
           mem: dict) -> dict:
    loans = prioritize(conn, customer_id, policy)
    days = max_overdue_days(conn, customer_id)
    will = mem.get("willingness") or score_customer(conn, customer_id, policy, mem.get("intents", [])).model_dump(mode="json")
    if session:
        emotions = mem.get("emotions", [])
        hits = _hits(conn, "session_id=?", (session["session_id"],))
        checks = conn.execute("SELECT COUNT(*) FROM audit_log WHERE session_id=? AND checkpoint IN ('坐席话术', '运营操作')",
                              (session["session_id"],)).fetchone()[0]
    else:
        emotions = [{"emotion": r["emotion_type"], "intensity": r["emotion_intensity"], "time": r["record_time"]}
                    for r in conn.execute("SELECT emotion_type, emotion_intensity, record_time FROM fact_emotion_log "
                                          "WHERE customer_id=? ORDER BY record_time DESC LIMIT 5", (customer_id,))]
        hits = _hits(conn, "customer_id=?", (customer_id,))
        checks = conn.execute("SELECT COUNT(*) FROM audit_log WHERE customer_id=?", (customer_id,)).fetchone()[0]
    history = Counter(r["call_result"] for r in conn.execute(
        "SELECT call_result FROM fact_call_history WHERE customer_id=?", (customer_id,)))
    violations = [r["violated_rule_id"] for r in conn.execute(
        "SELECT violated_rule_id FROM fact_call_history WHERE customer_id=? AND COALESCE(violated_rule_id,'')<>''",
        (customer_id,))]
    quality = [r["issue"] for r in conn.execute("SELECT issue FROM data_quality WHERE customer_id=?", (customer_id,))]
    return {
        "报告类型": "单客户催收报告" if session else "对话前预报告",
        "客户编号": customer_id,
        "会话编号": session["session_id"] if session else None,
        "生成时间": now.isoformat(timespec="seconds"),
        "逾期概况": {"逾期笔数": len(loans.items), "欠款总额": f"{loans.total_amount:.0f}元", "最长逾期天数": days,
                 "处理优先级": loans.suggestion, "排序依据": loans.basis},
        "还款意愿评估": {"评分": will["score"], "等级": will["level"], "置信度": will["confidence"],
                   "因素": [{"因素": f["name"], "得分": f["score"], "权重": f["weight"], "依据": f["note"]}
                          for f in will["factors"]],
                   "对话调整": will["adjustments"], "需人工复核": "是" if will["needs_review"] else "否"},
        "情绪感知": {"轨迹": emotions,
                 "峰值": max(emotions, key=lambda e: e.get("intensity") or 0) if emotions else None,
                 "最近": emotions[-1] if session and emotions else (emotions[0] if emotions else None)},
        "合规审计": {"审核记录数": checks, "命中规则": hits, "历史通话违规": violations, "门禁拦截": mem.get("gate")},
        "协商记录": {"方案协商": mem.get("negotiation", []), "还款承诺": mem.get("promises", []),
                 "减免申请": mem.get("waivers", []), "宽限请求": mem.get("grace_request"),
                 "历史催收结果": dict(history)},
        "建议策略": mem.get("strategy") or choose_strategy(policy, will["level"], days),
        "数据冲突": list(dict.fromkeys(loans.conflicts + quality)),
    }


def session_report(conn: sqlite3.Connection, session_id: str, policy: dict, now: datetime | None = None) -> dict:
    session = CaseStore(conn).get_session(session_id)
    if session is None:
        raise ValueError(f"会话不存在: {session_id}")
    return _build(conn, session["customer_id"], policy, now or datetime.now(), session, session_memory(conn, session_id))


def customer_report(conn: sqlite3.Connection, customer_id: str, policy: dict, now: datetime | None = None) -> dict:
    if conn.execute("SELECT 1 FROM dim_customer WHERE customer_id=?", (customer_id,)).fetchone() is None:
        raise ValueError(f"客户不存在: {customer_id}")
    latest = CaseStore(conn).latest_artifact(customer_id, "催收报告")
    if latest:
        return latest["content"]["data"]
    return _build(conn, customer_id, policy, now or datetime.now(), None, {})


def _line(value) -> str:
    if isinstance(value, dict):
        return "；".join(f"{k}：{_line(v)}" for k, v in value.items() if v not in EMPTY) or "无"
    if isinstance(value, list):
        return "、".join(_line(v) for v in value) or "无"
    return "无" if value in EMPTY else str(value)


def _md(value) -> list[str]:
    if value in EMPTY:
        return ["- 无"]
    if isinstance(value, dict):
        return [f"- {k}：{_line(v)}" for k, v in value.items()]
    if isinstance(value, list):
        return [f"- {_line(v)}" for v in value]
    return [f"- {value}"]


def render_report_markdown(report: dict, sections: list[str]) -> str:
    lines = [f"# {report['报告类型']}：{report['客户编号']}",
             f"- 会话：{report['会话编号'] or '无'}", f"- 生成时间：{report['生成时间']}"]
    for sec in [*sections, "数据冲突"]:
        lines += ["", f"## {sec}", *_md(report.get(sec))]
    return "\n".join(lines)


def render_customer_text(report: dict) -> str:
    o, w = report["逾期概况"], report["还款意愿评估"]
    text = (f"{report['客户编号']}：逾期{o['逾期笔数']}笔，欠款合计{o['欠款总额']}，最长逾期{o['最长逾期天数']}天；"
            f"还款意愿{w['评分']}分（{w['等级']}，置信度{w['置信度']}），建议策略：{report['建议策略']}。{o['处理优先级']}。")
    if report["数据冲突"]:
        text += f"注意：存在{len(report['数据冲突'])}条数据冲突，需人工复核。"
    return text


def summary_report(conn: sqlite3.Connection, now: datetime, policy: dict, month: str | None = None) -> dict:
    month = month or now.strftime("%Y-%m")
    notes: list[str] = []

    def calls_in(m: str) -> list:
        return conn.execute("SELECT * FROM fact_call_history WHERE substr(call_time,1,7)=?", (m,)).fetchall()

    rows = calls_in(month)
    if not rows:
        last = conn.execute("SELECT MAX(substr(call_time,1,7)) FROM fact_call_history").fetchone()[0]
        if last:
            notes.append(f"{month} 无催收记录，以下统计最近有记录的月份 {last}")
            month, rows = last, calls_in(last)
    positive = set(policy["scoring"]["call_result"]["positive"])
    results = Counter(r["call_result"] for r in rows)
    total = len(rows)
    all_results = Counter(r["call_result"] for r in conn.execute("SELECT call_result FROM fact_call_history"))
    repay = conn.execute("SELECT COUNT(*), COALESCE(SUM(repay_amount), 0) FROM fact_repayment "
                         "WHERE substr(repay_time,1,7)=?", (month,)).fetchone()
    if not repay[0]:
        notes.append(f"{month} 无还款记录")
    levels = Counter(score_customer(conn, r["customer_id"], policy).level
                     for r in conn.execute("SELECT customer_id FROM dim_customer ORDER BY customer_id"))
    handoffs = conn.execute("SELECT COUNT(*) FROM artifacts WHERE kind='移交包' AND substr(created_at,1,7)=?",
                            (month,)).fetchone()[0]
    return {
        "报告类型": "催收汇总报告", "统计月份": month, "生成时间": now.isoformat(timespec="seconds"), "说明": notes,
        "本期指标": {"催收次数": total, "覆盖客户数": len({r["customer_id"] for r in rows}),
                 "正向结果率": f"{sum(results[k] for k in positive) / total:.0%}" if total else "无数据",
                 "结果分布": dict(results), "渠道分布": dict(Counter(r["call_method"] for r in rows))},
        "全量指标": {"历史催收总次数": sum(all_results.values()), "结果分布": dict(all_results)},
        "实际还款": {"笔数": repay[0], "金额": f"{repay[1]:.0f}元"},
        "意愿分布": dict(levels),
        "合规审计": {"通话违规": dict(Counter(r["violated_rule_id"] for r in rows if r["violated_rule_id"])),
                 "系统审计命中": dict(Counter(h["rule_id"] for h in _hits(conn, "substr(ts,1,7)=?", (month,))))},
        "人工接管次数": handoffs,
    }


def render_summary_text(report: dict) -> str:
    lines = [f"{report['统计月份']} 催收汇总报告"]
    lines += [f"说明：{n}" for n in report["说明"]]
    for sec in ("本期指标", "全量指标", "实际还款", "意愿分布", "合规审计"):
        lines.append(f"{sec}：{_line(report[sec])}")
    lines.append(f"人工接管次数：{report['人工接管次数']}")
    return "\n".join(lines)
```

- [ ] **Step 4: 实现 `postcall.py`**

```python
from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import db
from .cases import CaseStore
from .config_center import ConfigCenter
from .loans import prioritize
from .memory import MemoryStore
from .reports import render_report_markdown, session_memory, session_report
from .scoring import score_customer

STAGES = {"write_call_record": 1, "write_behavior": 1, "write_emotion": 1,
          "refresh_profile": 2, "generate_report": 2, "create_todos": 3}
BEHAVIOR_OF_INTENT = {"承诺还款": "承诺还款", "要求宽限": "申请延期", "要求分期": "协商分期", "要求减免": "申请减免",
                      "拒绝还款": "拒绝沟通", "投诉": "投诉"}


@dataclass
class PostCtx:
    conn: sqlite3.Connection
    session: dict
    case_id: str
    customer_id: str
    pack: dict
    policy: dict
    mem: dict
    messages: list[dict]
    now: datetime

    @property
    def sid(self) -> str:
        return self.session["session_id"]


def build_ctx(conn: sqlite3.Connection, session_id: str, pack: dict, policy: dict) -> PostCtx:
    cases = CaseStore(conn)
    s = cases.get_session(session_id)
    if s is None:
        raise ValueError(f"会话不存在: {session_id}")
    return PostCtx(conn=conn, session=s, case_id=s["case_id"], customer_id=s["customer_id"], pack=pack, policy=policy,
                   mem=session_memory(conn, session_id), messages=cases.messages(session_id),
                   now=datetime.fromisoformat(s["ended_at"] or s["started_at"]))


def _replace(ctx: PostCtx, table: str, rows: list[dict]) -> None:
    ctx.conn.execute(f"DELETE FROM {db.q(table)} WHERE session_id=? AND source=?", (ctx.sid, db.GENERATED))
    for row in rows:
        db.insert_generated(ctx.conn, table, row, ctx.sid)
    ctx.conn.commit()


def _call_result(mem: dict) -> str:
    if mem.get("gate"):
        return "未外呼"
    if mem.get("promises"):
        return "承诺还款"
    if any(n.get("方案") for n in mem.get("negotiation", [])):
        return "协商分期"
    if "拒绝还款" in mem.get("intents", []):
        return "拒绝沟通"
    return "已沟通"


def write_call_record(ctx: PostCtx) -> None:
    method = "外呼拦截" if ctx.mem.get("gate") else "客户进线" if ctx.session["channel"] == "客户进线" else "AI对话"
    human = any(m["role"] == "人工坐席" for m in ctx.messages)
    hits = [r["rule_id"] for r in ctx.conn.execute(
        "SELECT DISTINCT rule_id FROM audit_log WHERE session_id=? AND checkpoint='坐席话术' AND rule_id IS NOT NULL",
        (ctx.sid,))]
    summary = (f"{ctx.pack['name']}；客户诉求：{'、'.join(ctx.mem.get('intents', [])) or '无'}；"
               f"结束原因：{ctx.session['end_reason'] or '正常结束'}")
    _replace(ctx, "fact_call_history", [{
        "call_id": f"CH-{ctx.sid}", "customer_id": ctx.customer_id, "call_time": ctx.now.strftime("%Y-%m-%d %H:%M"),
        "call_method": method, "talk_summary": summary[:120], "call_result": _call_result(ctx.mem),
        "agent_id": ctx.pack["speech"]["agent_id"] + ("+人工" if human else ""),
        "violated_rule_id": ",".join(hits) or None,
    }])


def write_behavior(ctx: PostCtx) -> None:
    kinds = [(i, BEHAVIOR_OF_INTENT[i]) for i in ctx.mem.get("intents", []) if i in BEHAVIOR_OF_INTENT]
    _replace(ctx, "fact_user_behavior", [{
        "behavior_id": f"B-{ctx.sid}-{n}", "customer_id": ctx.customer_id, "behavior_type": kind,
        "behavior_time": ctx.now.strftime("%Y-%m-%d %H:%M"), "behavior_detail": f"本次通话中客户{intent}",
    } for n, (intent, kind) in enumerate(kinds, 1)])


def write_emotion(ctx: PostCtx) -> None:
    emotions = ctx.mem.get("emotions", [])
    rows = []
    if emotions:
        picks = {"峰值": max(emotions, key=lambda e: e.get("intensity") or 0), "结束": emotions[-1]}
        for tag, e in picks.items():
            if tag == "结束" and e is picks["峰值"]:
                continue
            rows.append({"emotion_id": f"E-{ctx.sid}-{tag}", "customer_id": ctx.customer_id,
                         "emotion_type": e["emotion"], "emotion_intensity": e.get("intensity"),
                         "trigger_talk": e.get("text", ""), "record_time": ctx.now.strftime("%Y-%m-%d %H:%M")})
    _replace(ctx, "fact_emotion_log", rows)


def refresh_profile(ctx: PostCtx) -> None:
    will = score_customer(ctx.conn, ctx.customer_id, ctx.policy, ctx.mem.get("intents", []))
    ctx.conn.execute("DELETE FROM profile_snapshots WHERE customer_id=? AND session_id=?", (ctx.customer_id, ctx.sid))
    version = ctx.conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM profile_snapshots WHERE customer_id=?",
                               (ctx.customer_id,)).fetchone()[0]
    content = {"willingness": will.model_dump(mode="json"),
               "loans": prioritize(ctx.conn, ctx.customer_id, ctx.policy).model_dump(mode="json")}
    ctx.conn.execute("INSERT INTO profile_snapshots VALUES (?,?,?,?,?)",
                     (ctx.customer_id, version, db.json_dumps(content), ctx.sid, db.now_iso()))
    ctx.conn.commit()
    mem = MemoryStore(ctx.conn)
    mem.set("customer", ctx.customer_id, "last_willingness",
            {"score": will.score, "level": will.level, "session_id": ctx.sid}, f"会话{ctx.sid}")
    mem.set("customer", ctx.customer_id, "last_contact",
            {"time": ctx.now.isoformat(timespec="minutes"), "result": _call_result(ctx.mem)}, f"会话{ctx.sid}")
    if ctx.mem.get("promises"):
        mem.set("customer", ctx.customer_id, "open_promises", ctx.mem["promises"], f"会话{ctx.sid}")


def generate_report(ctx: PostCtx) -> None:
    rep = session_report(ctx.conn, ctx.sid, ctx.policy, ctx.now)
    ctx.conn.execute("DELETE FROM artifacts WHERE session_id=? AND kind='催收报告'", (ctx.sid,))
    ctx.conn.commit()
    CaseStore(ctx.conn).add_artifact(ctx.case_id, ctx.sid, "催收报告",
                                     {"data": rep, "markdown": render_report_markdown(rep, ctx.pack["report_sections"])},
                                     ctx.now)


def create_todos(ctx: PostCtx) -> None:
    cs, sid = CaseStore(ctx.conn), ctx.sid
    for n, p in enumerate(ctx.mem.get("promises", []), 1):
        amount = f"{p['amount']:.0f}元" if p.get("amount") else "欠款"
        cs.add_todo(ctx.case_id, sid, "承诺跟进", f"客户承诺{p.get('date') or '近期'}还款{amount}，到期核实",
                    p.get("date"), ctx.now, todo_id=f"TD-{sid}-promise-{n}")
    for w in ctx.mem.get("waivers", []):
        cs.add_todo(ctx.case_id, sid, "减免审批", f"减免申请 {w['request_id']} 待审批：{w['scope']}", None, ctx.now,
                    todo_id=f"TD-{sid}-{w['request_id']}")
    if grace := ctx.mem.get("grace_request"):
        cs.add_todo(ctx.case_id, sid, "宽限跟进", f"客户申请宽限至{grace.get('date') or '待确认'}", grace.get("date"),
                    ctx.now, todo_id=f"TD-{sid}-grace")
    if (gate := ctx.mem.get("gate")) and gate.get("next_allowed"):
        cs.add_todo(ctx.case_id, sid, "下次可联系", f"外呼被 {'、'.join(gate['rule_ids'])} 拦截，下次可联系时间到后再发起",
                    gate["next_allowed"], ctx.now, todo_id=f"TD-{sid}-next-contact")


TASKS: dict[str, Callable[[PostCtx], None]] = {
    "write_call_record": write_call_record, "write_behavior": write_behavior, "write_emotion": write_emotion,
    "refresh_profile": refresh_profile, "generate_report": generate_report, "create_todos": create_todos,
}


class PostCallRunner:
    def __init__(self, db_path: Path | str, config_dir: Path | str, max_retries: int = 2):
        self.db_path, self.config_dir, self.max_retries = db_path, Path(config_dir), max_retries

    def _load(self, conn: sqlite3.Connection, session_id: str) -> tuple[dict, dict, dict]:
        s = CaseStore(conn).get_session(session_id)
        if s is None:
            raise ValueError(f"会话不存在: {session_id}")
        cc = ConfigCenter(conn, self.config_dir)
        return s, cc.get("business_pack", s["pack_id"])[0], cc.get("policy")[0]

    def enqueue(self, session_id: str, sync: bool = False) -> None:
        conn = db.connect(self.db_path)
        try:
            s, pack, _ = self._load(conn, session_id)
            for name in pack["post_tasks"]:
                conn.execute("INSERT OR IGNORE INTO post_tasks VALUES (?,?,?,?,?,?,?)",
                             (session_id, name, STAGES[name], "等待", 0, None, db.now_iso()))
            conn.commit()
            CaseStore(conn).set_case_status(s["case_id"], "后置处理中", datetime.now())
        finally:
            conn.close()
        if sync:
            self._run(session_id)
        else:
            threading.Thread(target=self._run, args=(session_id,), daemon=True).start()

    def _run(self, session_id: str) -> None:
        conn = db.connect(self.db_path)
        try:
            s, pack, policy = self._load(conn, session_id)
            pending = [dict(r) for r in conn.execute(
                "SELECT name, stage FROM post_tasks WHERE session_id=? AND status<>'完成' ORDER BY stage, name", (session_id,))]
        finally:
            conn.close()
        for stage in sorted({p["stage"] for p in pending}):
            names = [p["name"] for p in pending if p["stage"] == stage]
            with ThreadPoolExecutor(max_workers=len(names)) as ex:
                list(ex.map(lambda n: self._run_task(session_id, n, pack, policy), names))
        conn = db.connect(self.db_path)
        try:
            if all(t["status"] == "完成" for t in self._status(conn, session_id)):
                CaseStore(conn).set_case_status(s["case_id"], "已完成", datetime.now())
        finally:
            conn.close()

    @staticmethod
    def _mark(conn: sqlite3.Connection, session_id: str, name: str, status: str, inc: int, error: str | None) -> None:
        conn.execute("UPDATE post_tasks SET status=?, attempts=attempts+?, error=?, updated_at=? WHERE session_id=? AND name=?",
                     (status, inc, error, db.now_iso(), session_id, name))
        conn.commit()

    def _run_task(self, session_id: str, name: str, pack: dict, policy: dict) -> bool:
        error = ""
        for _ in range(self.max_retries + 1):
            conn = db.connect(self.db_path)
            try:
                self._mark(conn, session_id, name, "执行中", 1, None)
                TASKS[name](build_ctx(conn, session_id, pack, policy))
                self._mark(conn, session_id, name, "完成", 0, None)
                return True
            except Exception as e:  # noqa: BLE001 单个任务的任意失败都要隔离并记录
                conn.rollback()
                error = f"{type(e).__name__}: {e}"
                self._mark(conn, session_id, name, "失败", 0, error)
            finally:
                conn.close()
        conn = db.connect(self.db_path)
        try:
            s = CaseStore(conn).get_session(session_id)
            CaseStore(conn).add_todo(s["case_id"], session_id, "后置任务失败", f"{name} 重试后仍失败：{error}", None,
                                     datetime.now(), todo_id=f"TD-{session_id}-post-{name}")
        finally:
            conn.close()
        return False

    @staticmethod
    def _status(conn: sqlite3.Connection, session_id: str) -> list[dict]:
        return [dict(r) for r in conn.execute("SELECT * FROM post_tasks WHERE session_id=? ORDER BY stage, name",
                                              (session_id,))]

    def status(self, session_id: str) -> list[dict]:
        conn = db.connect(self.db_path)
        try:
            return self._status(conn, session_id)
        finally:
            conn.close()

    def retry(self, session_id: str, name: str | None = None, sync: bool = False) -> None:
        conn = db.connect(self.db_path)
        try:
            sql = "UPDATE post_tasks SET status='等待', error=NULL WHERE session_id=?"
            params: list = [session_id]
            if name:
                sql += " AND name=?"
                params.append(name)
            else:
                sql += " AND status='失败'"
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()
        if sync:
            self._run(session_id)
        else:
            threading.Thread(target=self._run, args=(session_id,), daemon=True).start()
```

`db.py` 追加一个序列化辅助函数（供 `postcall.refresh_profile` 使用）：

```python
import json


def json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python -m pytest tests/test_reports_postcall.py -v`
Expected: 4 passed

- [ ] **Step 6: Commit**

```powershell
git add backend/cagent/reports.py backend/cagent/postcall.py backend/cagent/db.py backend/tests/test_reports_postcall.py
git commit -m "feat: session/customer/summary reports and idempotent staged post-call tasks"
```

---

### Task 12: 主控 Agent（路由、运营能力、发起与结束催收、人工接管）

**Files:**
- Create: `backend/cagent/agents/main.py`
- Test: `backend/tests/test_main_agent.py`

**Interfaces:**
- Consumes: Task 1–11 全部模块
- Produces:
  - `MODE_OPERATOR = "运营模式"`、`MODE_COLLECTION = "催收对话模式"`、`MODE_HANDOFF = "人工接管"`
  - `MainTurn`：`session_id, mode, speaker（customer / agent / operator / unknown）, speaker_method, system_intent, reply, customer_id, hit_rules, compliance, willingness_score, willingness_level, strategy, handoff, handoff_package_id, needs_review, review_reasons, key_points, trace_id, trace`
  - `Started`：`session_id, blocked: str | None, gate: CheckResult | None`
  - `MainAgent(conn, *, db_path, config_dir, llm_client=None, offline=False, sync_post=False)`、`MainAgent.from_settings(**kw)`：
    - `registry`（owner `main`；工具 `get_customer_profile, get_overdues, get_contact_count_today, get_willingness`；Skill `customer_report, summary_report`）
    - `handle(text, *, session_id=None, role=None, scene_type=None, customer_id=None, now=None, interactive=True, sink=None) -> MainTurn`
    - `start_collection(customer_id, now, trace, channel="外呼") -> Started`、`end_collection(session_id, now, reason, sync_post=None)`、`finish_handoff(session_id, conclusion, now)`
    - `match_pack(customer_id, trace) -> tuple[dict, str]`、`validate(pack) -> list[str]`、`reload()`
  - `role` 取值：`customer / 客户`、`agent / 坐席 / AI坐席 / 人工坐席`、`operator / 运营`
  - 大模型任务代号：`rewrite`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_main_agent.py`（全部离线运行，不使用公开题原文）：

```python
from datetime import datetime

import pytest

from cagent import settings
from cagent.agents.main import MODE_COLLECTION, MODE_HANDOFF, MODE_OPERATOR, MainAgent
from cagent.cases import CaseStore
from cagent.config_center import ConfigCenter

NOW = datetime(2026, 10, 24, 10, 0)


@pytest.fixture
def main(db_conn, db_path):
    return MainAgent(db_conn, db_path=db_path, config_dir=settings.CONFIG_DIR, offline=True, sync_post=True)


def test_shipped_packs_valid_against_real_registries(main, db_conn):
    cc = ConfigCenter(db_conn, settings.CONFIG_DIR)
    for pid in cc.pack_ids():
        assert main.validate(cc.get("business_pack", pid)[0]) == []


def test_quiet_hours_question(main):
    t = main.handle("现在晚上11点了，能给C0003打电话吗？", now=NOW)
    assert t.speaker == "operator" and t.system_intent == "合规检测"
    assert "R004" in t.hit_rules and "不可以" in t.reply and "08:00" in t.reply


def test_agent_speech_audit(main, db_conn):
    t = main.handle("你今天不还就上门找你", now=NOW)
    assert t.speaker == "agent" and t.reply.startswith("[合规拦截]")
    assert "R002" in t.hit_rules and t.handoff
    assert any(r["kind"] == "主管复核" for r in CaseStore(db_conn).todos())


def test_customer_flow_handoff_and_postcall(main, db_conn):
    t = main.handle("能不能分12期", role="customer", customer_id="C0003", now=NOW)
    assert t.mode == MODE_COLLECTION and t.system_intent == "分期方案协商" and "12期" in t.reply
    sid = t.session_id
    t2 = main.handle("给我转人工", session_id=sid, now=NOW)
    assert t2.mode == MODE_HANDOFF and t2.handoff and t2.handoff_package_id
    assert CaseStore(db_conn).artifacts(session_id=sid, kind="移交包")
    t3 = main.handle("我们会联系你的家人", session_id=sid, now=NOW)
    assert t3.reply.startswith("[合规拦截]") and "R009" in t3.hit_rules
    main.finish_handoff(sid, "客户同意下月10号还款", NOW)
    call = db_conn.execute("SELECT * FROM fact_call_history WHERE call_id=?", (f"CH-{sid}",)).fetchone()
    assert call is not None and call["agent_id"] == "AI02+人工"
    assert CaseStore(db_conn).get_session(sid)["mode"] == MODE_OPERATOR


def test_summary_report(main):
    t = main.handle("生成本月催收报告", now=NOW)
    assert t.system_intent == "催收报告" and "催收次数" in t.reply


def test_outbound_blocked_at_night(main, db_conn):
    t = main.handle("对C0001发起催收", now=datetime(2026, 10, 24, 23, 30))
    assert "R004" in t.hit_rules and "拦截" in t.reply
    kinds = [r["kind"] for r in CaseStore(db_conn).todos(session_id=t.session_id)]
    assert "下次可联系" in kinds
    call = db_conn.execute("SELECT call_method FROM fact_call_history WHERE call_id=?", (f"CH-{t.session_id}",)).fetchone()
    assert call["call_method"] == "外呼拦截"


def test_outbound_daytime_then_operator_ends(main, db_conn):
    t = main.handle("对C0001发起催收", now=NOW)
    assert t.mode == MODE_COLLECTION and "工号AI01" in t.reply
    end = main.handle("结束催收", role="operator", session_id=t.session_id, now=NOW)
    assert "已结束" in end.reply
    call = db_conn.execute("SELECT call_method FROM fact_call_history WHERE call_id=?", (f"CH-{t.session_id}",)).fetchone()
    assert call["call_method"] == "AI对话"


def test_operator_queries_and_bad_input(main):
    assert "O0033" in main.handle("查询C0001的欠款", now=NOW).reply
    assert "还款意愿" in main.handle("看下C0002的客户画像", now=NOW).reply
    empty = main.handle("", now=NOW)
    assert empty.needs_review and empty.speaker == "unknown"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_main_agent.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'cagent.agents.main'`

- [ ] **Step 3: 实现 `agents/main.py`**

```python
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from .. import db, importer, settings
from ..audit import write_audit, write_event
from ..cases import CaseStore
from ..clock import parse_time_hint
from ..compliance import CheckResult, ComplianceEngine, contact_count_on
from ..config_center import ConfigCenter, validate_pack
from ..desensitize import display_mask
from ..llm import GuardedLLM, LLMError, build_llm
from ..loans import LoanPriority, prioritize
from ..memory import MemoryStore
from ..postcall import STAGES, PostCallRunner
from ..registry import CallError, Registry
from ..reports import (customer_report, render_customer_text, render_report_markdown, render_summary_text,
                       summary_report)
from ..scoring import Willingness, score_customer
from ..trace import Trace, TraceEvent
from .collection import ACTION_NAMES, CollectionAgent, Snapshot, TurnOutput, format_speech
from .understanding import SpeakerResult, Understander

MODE_OPERATOR, MODE_COLLECTION, MODE_HANDOFF = "运营模式", "催收对话模式", "人工接管"
ROLE_MAP = {"customer": "customer", "客户": "customer", "agent": "agent", "坐席": "agent", "AI坐席": "agent",
            "人工坐席": "agent", "operator": "operator", "运营": "operator"}
HELP_TEXT = ("我可以帮您：查询客户画像或欠款、判断当前能否外呼、生成单客户或本月汇总催收报告、对客户发起或结束催收。"
             "请带上客户编号，例如「查询C0001的欠款」。")
CLARIFY_SPEAKER = "请问这句话是客户说的、坐席说的，还是您（运营）的指令？"
_THIRD_PARTY = re.compile(r"家人|父母|亲戚|朋友|同事|领导")
REWRITE_SYSTEM = (
    "你是催收合规质检员，把违规话术改写为合规表述：保留提醒还款的业务意图，后果只能客观说明（如影响征信），"
    "去掉威胁、辱骂、虚假承诺、冒充身份、骚扰亲友、误导等内容；不超过 80 字；尖括号占位符原样保留。\n"
    '输出 json，例如 {"reply": "……"}'
)


class MainTurn(BaseModel):
    session_id: str | None = None
    mode: str = MODE_OPERATOR
    speaker: str
    speaker_method: str = ""
    system_intent: str
    reply: str
    customer_id: str | None = None
    hit_rules: list[str] = []
    compliance: list[CheckResult] = []
    willingness_score: int | None = None
    willingness_level: str | None = None
    strategy: str | None = None
    handoff: bool = False
    handoff_package_id: str | None = None
    needs_review: bool = False
    review_reasons: list[str] = []
    key_points: list[str] = []
    trace_id: str = ""
    trace: list[dict] = []


@dataclass
class Started:
    session_id: str
    blocked: str | None = None
    gate: CheckResult | None = None


class CidIn(BaseModel):
    customer_id: str


class CountIn(BaseModel):
    customer_id: str
    day: str


class CountOut(BaseModel):
    customer_id: str
    day: str
    count: int


class ProfileOut(BaseModel):
    customer_id: str
    profile: dict
    behaviors: list[dict]
    emotions: list[dict]
    repayments: list[dict]
    memory: dict


class ReportIn(BaseModel):
    customer_id: str
    now: str


class SummaryIn(BaseModel):
    now: str
    month: str | None = None


class ReportOut(BaseModel):
    report: dict
    text: str


def _ids(results: list[CheckResult]) -> list[str]:
    return list(dict.fromkeys(h.rule_id for r in results for h in r.hits))


def _violation_text(r: CheckResult) -> str:
    return "\n".join(f"[合规拦截] 命中 {h.rule_id}（{h.rule_type}，{h.level}）：{h.rule_content}；"
                     f"证据「{h.evidence}」；处置：{h.action}" for h in r.hits)


class MainAgent:
    def __init__(self, conn: sqlite3.Connection, *, db_path: Path | str, config_dir: Path | str, llm_client=None,
                 offline: bool = False, sync_post: bool = False):
        self.conn, self.db_path, self.config_dir, self.sync_post = conn, db_path, Path(config_dir), sync_post
        self.cc = ConfigCenter(conn, self.config_dir)
        client = None if offline else (llm_client or build_llm(self.cc.get("llm")[0]))
        self.llm = GuardedLLM(client, conn)
        self.cases, self.memory = CaseStore(conn), MemoryStore(conn)
        self.post = PostCallRunner(db_path, self.config_dir)
        self.agents: dict[str, CollectionAgent] = {}
        self.reload()
        self.registry = self._build_registry()

    @classmethod
    def from_settings(cls, **kw) -> "MainAgent":
        conn = db.connect(settings.DB_PATH)
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dim_customer'").fetchone() is None:
            importer.import_all(conn, settings.DATA_DIR)
        db.init_runtime(conn)
        return cls(conn, db_path=settings.DB_PATH, config_dir=settings.CONFIG_DIR, **kw)

    def reload(self) -> None:
        self.policy = self.cc.get("policy")[0]
        self.intents = self.cc.get("intents")[0]
        self.main_cfg = self.cc.get("main_agent")[0]
        cp, self.cp_version = self.cc.get("checkpoints")
        self.engine = ComplianceEngine(self.conn, cp, self.cp_version, self.llm)
        self.understander = Understander(self.llm, self.intents)

    def _build_registry(self) -> Registry:
        r = Registry("main")
        r.register("get_customer_profile", self._profile, CidIn, ProfileOut, kind="tool",
                   description="客户画像：基本信息、近期行为、情绪、还款记录与长期记忆")
        r.register("get_overdues", lambda a: prioritize(self.conn, a.customer_id, self.policy), CidIn, LoanPriority,
                   kind="tool", description="客户全部逾期记录及处理优先级")
        r.register("get_contact_count_today", lambda a: CountOut(
            customer_id=a.customer_id, day=a.day,
            count=contact_count_on(self.conn, a.customer_id, datetime.fromisoformat(a.day).date())),
                   CountIn, CountOut, kind="tool", description="客户指定日期已被联系次数（不含外呼拦截与客户进线）")
        r.register("get_willingness", lambda a: score_customer(self.conn, a.customer_id, self.policy), CidIn,
                   Willingness, kind="tool", description="对话前还款意愿评分")
        r.register("customer_report", self._customer_report, ReportIn, ReportOut, kind="skill",
                   description="单客户催收报告")
        r.register("summary_report", lambda a: self._summary(a), SummaryIn, ReportOut, kind="skill",
                   description="月度催收汇总报告")
        return r

    def _profile(self, a: CidIn) -> ProfileOut:
        cols = [c for c, _ in db.BUSINESS_TABLES["dim_customer"]]
        row = self.conn.execute(f"SELECT {', '.join(cols)} FROM dim_customer WHERE customer_id=?",
                                (a.customer_id,)).fetchone()
        if row is None:
            raise ValueError(f"客户不存在: {a.customer_id}")

        def rows(sql: str) -> list[dict]:
            return [dict(x) for x in self.conn.execute(sql, (a.customer_id,))]

        return ProfileOut(
            customer_id=a.customer_id, profile=dict(row),
            behaviors=rows("SELECT behavior_id, behavior_type, behavior_time, behavior_detail, source FROM "
                           "fact_user_behavior WHERE customer_id=? ORDER BY behavior_time DESC LIMIT 10"),
            emotions=rows("SELECT emotion_id, emotion_type, emotion_intensity, record_time, source FROM fact_emotion_log "
                          "WHERE customer_id=? ORDER BY record_time DESC LIMIT 5"),
            repayments=rows("SELECT repay_id, repay_amount, repay_time, is_on_time FROM fact_repayment "
                            "WHERE customer_id=? ORDER BY repay_time DESC LIMIT 10"),
            memory={k: v["value"] for k, v in self.memory.all("customer", a.customer_id).items()},
        )

    def _default_pack(self) -> dict:
        return self.cc.get("business_pack", self.main_cfg["default_pack"])[0]

    def _customer_report(self, a: ReportIn) -> ReportOut:
        rep = customer_report(self.conn, a.customer_id, self.policy, datetime.fromisoformat(a.now))
        sections = self._default_pack()["report_sections"]
        return ReportOut(report=rep, text=render_customer_text(rep) + "\n\n" + render_report_markdown(rep, sections))

    def _summary(self, a: SummaryIn) -> ReportOut:
        rep = summary_report(self.conn, datetime.fromisoformat(a.now), self.policy, a.month)
        return ReportOut(report=rep, text=render_summary_text(rep))

    def validate(self, pack: dict) -> list[str]:
        return validate_pack(pack, main_tools=set(self.registry.names("tool")), collection_actions=ACTION_NAMES,
                             rule_ids=set(self.engine.rules), post_tasks=set(STAGES))

    def _finish(self, trace: Trace, **kw) -> MainTurn:
        return MainTurn(**kw, trace_id=trace.trace_id, trace=trace.to_list())

    def handle(self, text: str | None, *, session_id: str | None = None, role: str | None = None,
               scene_type: str | None = None, customer_id: str | None = None, now: datetime | None = None,
               interactive: bool = True, sink: Callable[[TraceEvent], None] | None = None) -> MainTurn:
        self.reload()
        trace = Trace(sink)
        base = now or datetime.now()
        hinted = parse_time_hint(text or "", base)
        now = hinted or base
        if hinted:
            trace.add("understand", "题面时间", time=now.isoformat(timespec="minutes"))
        role = ROLE_MAP.get(role or "")
        sess = self.cases.get_session(session_id) if session_id else None
        if sess and not sess["ended_at"]:
            if sess["mode"] == MODE_HANDOFF:
                return self._handoff_turn(session_id, text or "", role, now, trace)
            if sess["mode"] == MODE_COLLECTION and session_id in self.agents and role in (None, "customer"):
                trace.add("route", "说话人判定", speaker="customer", method="会话模式")
                return self._customer_turn(session_id, text or "", now, trace, "会话模式")
        clean, issue = self.understander.preprocess(text)
        if issue in ("空输入", "乱码"):
            trace.add("understand", "输入预处理", "warn", issue=issue)
            return self._finish(trace, session_id=session_id, speaker="unknown", system_intent="提醒催收",
                                reply="没有识别到有效内容，请重新输入。", customer_id=customer_id, needs_review=True,
                                review_reasons=[f"输入异常：{issue}"])
        sp = (SpeakerResult(speaker=role, confidence=1.0, method="显式角色") if role
              else self.understander.speaker(clean, scene_type))
        trace.add("route", "说话人判定", speaker=sp.speaker, method=sp.method, confidence=sp.confidence)
        if sp.needs_clarify and interactive and not customer_id:
            return self._finish(trace, session_id=session_id, speaker="unknown", speaker_method=sp.method,
                                system_intent="提醒催收", reply=CLARIFY_SPEAKER, needs_review=True,
                                review_reasons=["说话人无法判定"])
        if sp.speaker == "operator":
            return self._operator_turn(clean, customer_id, session_id, now, trace, sp)
        if sp.speaker == "agent":
            return self._audit_turn(clean, customer_id, now, trace, sp)
        cid = customer_id or (sess["customer_id"] if sess else None)
        if not cid:
            return self._finish(trace, speaker="customer", speaker_method=sp.method, system_intent="提醒催收",
                                reply="请先提供客户编号（如 C0001），再开始对话。", needs_review=True,
                                review_reasons=["缺少客户编号"])
        started = self.start_collection(cid, now, trace, channel="客户进线")
        if started.blocked:
            return self._finish(trace, session_id=started.session_id, speaker="customer", speaker_method=sp.method,
                                system_intent="提醒催收", reply=started.blocked, customer_id=cid, needs_review=True,
                                review_reasons=[started.blocked])
        return self._customer_turn(started.session_id, text or "", now, trace, sp.method)

    def match_pack(self, customer_id: str, trace: Trace) -> tuple[dict, str]:
        items = prioritize(self.conn, customer_id, self.policy).items
        loan_type = items[0].loan_type if items else None
        chosen = self.main_cfg["default_pack"]
        for pid in self.cc.pack_ids():
            if loan_type and loan_type in self.cc.get("business_pack", pid)[0]["match"]["loan_types"]:
                chosen = pid
                break
        pack, version = self.cc.get("business_pack", chosen)
        trace.add("decide", "匹配业务包", loan_type=loan_type, pack=chosen, version=version)
        return pack, version

    def start_collection(self, customer_id: str, now: datetime, trace: Trace, channel: str = "外呼") -> Started:
        if self.conn.execute("SELECT 1 FROM dim_customer WHERE customer_id=?", (customer_id,)).fetchone() is None:
            raise ValueError(f"客户不存在: {customer_id}")
        pack, pv = self.match_pack(customer_id, trace)
        case_id = self.cases.create_case(customer_id, pack["pack_id"], now)
        sid = self.cases.open_session(case_id, customer_id, MODE_OPERATOR, pack["pack_id"], f"{pv}/{self.cp_version}",
                                      channel, now)
        self.cases.set_case_status(case_id, "前置检查", now)
        errors = self.validate(pack)
        if errors:
            msg = "业务包配置有误，无法发起：" + "；".join(errors)
            self.end_collection(sid, now, msg)
            return Started(sid, msg)
        if channel == "外呼":
            gate = self.engine.check_operator_action(action="外呼", customer_id=customer_id, now=now, only=pack["gates"])
            write_audit(self.conn, result=gate, customer_id=customer_id, scene=pack["name"],
                        content=f"发起外呼 {customer_id}", config_version=self.cp_version, ts=now, session_id=sid)
            trace.add("guard", "检查点二：外呼门禁", "ok" if gate.passed else "warn", hits=_ids([gate]),
                      next_allowed=gate.next_allowed)
            if not gate.passed:
                self.memory.set("session", sid, "gate", {"rule_ids": _ids([gate]), "next_allowed": gate.next_allowed},
                                "外呼门禁")
                msg = "外呼已拦截：" + "；".join(f"命中 {h.rule_id}（{h.rule_type}）{h.rule_content}，处置：{h.action}"
                                            for h in gate.hits)
                if gate.next_allowed:
                    msg += f"。下次可外呼时间：{gate.next_allowed.replace('T', ' ')}，已生成待办"
                self.end_collection(sid, now, "外呼门禁拦截")
                return Started(sid, msg, gate)
        snap: dict = {"customer_id": customer_id, "degraded": []}
        for item in pack["pre_tools"]:
            tool = item["tool"]
            args = {"customer_id": customer_id, **({"day": now.date().isoformat()} if tool == "get_contact_count_today" else {})}
            try:
                out = self.registry.call(tool, trace, **args)
            except CallError as e:
                if item["on_failure"] == "block":
                    msg = f"前置工具 {tool} 调用失败（{e.reason}），已停止发起"
                    write_event(self.conn, checkpoint="前置检查", customer_id=customer_id, scene=pack["name"],
                                content=msg, ts=now, session_id=sid)
                    self.end_collection(sid, now, msg)
                    return Started(sid, msg)
                snap["degraded"].append(tool)
                trace.add("call", f"{tool} 失败按 {item['on_failure']} 处理", "warn", reason=e.reason)
                continue
            if tool == "get_customer_profile":
                snap["profile"] = out.profile
            elif tool == "get_overdues":
                snap["overdues"] = [i.model_dump() for i in out.items]
            elif tool == "get_contact_count_today":
                snap["contact_count_today"] = out.count
            elif tool == "get_willingness":
                snap["willingness"] = out.model_dump(mode="json")
        self.agents[sid] = CollectionAgent(
            conn=self.conn, session_id=sid, case_id=case_id, customer_id=customer_id, pack=pack,
            audit_version=f"{pv}/{self.cp_version}", policy=self.policy, intents_cfg=self.intents, engine=self.engine,
            llm=self.llm, understander=self.understander, memory=self.memory, cases=self.cases,
            snapshot=Snapshot(**snap), verified=channel == "客户进线")
        self.cases.update_session(sid, mode=MODE_COLLECTION)
        self.cases.set_case_status(case_id, "对话中", now)
        trace.add("route", "切换模式", mode=MODE_COLLECTION, pack=pack["pack_id"], session_id=sid)
        return Started(sid)

    def end_collection(self, session_id: str, now: datetime, reason: str, sync_post: bool | None = None) -> None:
        s = self.cases.get_session(session_id)
        if s is None or s["ended_at"]:
            return
        self.cases.update_session(session_id, mode=MODE_OPERATOR, ended_at=now.isoformat(timespec="seconds"),
                                  end_reason=reason)
        self.agents.pop(session_id, None)
        self.post.enqueue(session_id, sync=self.sync_post if sync_post is None else sync_post)

    def finish_handoff(self, session_id: str, conclusion: str, now: datetime) -> None:
        self.cases.add_message(session_id, "人工坐席", f"[处理结论] {conclusion}", {"conclusion": True}, now)
        self.memory.set("session", session_id, "handoff_conclusion", conclusion, "人工坐席")
        self.end_collection(session_id, now, "人工处理完成")

    def _key_points(self, out: TurnOutput) -> list[str]:
        pts = [f"客户意图：{out.customer_intent}", f"情绪：{out.emotion}（强度{out.emotion_intensity:.2f}）"]
        if out.plan and out.plan.plans:
            p = out.plan.plans[0]
            pts.append(f"推荐方案：{p.template_id} {p.template_type} {p.periods}期，月供{p.monthly_payment:.2f}元")
        if out.plan and out.plan.message:
            pts.append(out.plan.message)
        if out.loans and out.loans.items:
            pts.append(out.loans.suggestion)
        if out.willingness:
            pts.append(f"还款意愿：{out.willingness.score}分（{out.willingness.level}）")
        if out.strategy:
            pts.append(f"建议策略：{out.strategy}")
        if out.handoff:
            pts.append(f"转人工：{out.handoff_reason}")
        return pts

    def _customer_turn(self, sid: str, text: str, now: datetime, trace: Trace, method: str) -> MainTurn:
        agent = self.agents[sid]
        out = agent.handle(text, now, trace)
        pkg = self._handoff(sid, out.handoff_reason or "", now, trace) if out.handoff else None
        return self._finish(
            trace, session_id=sid, mode=MODE_HANDOFF if out.handoff else MODE_COLLECTION, speaker="customer",
            speaker_method=method, system_intent=out.system_intent, reply=out.reply, customer_id=agent.customer_id,
            hit_rules=_ids(out.compliance), compliance=out.compliance,
            willingness_score=out.willingness.score if out.willingness else None,
            willingness_level=out.willingness.level if out.willingness else None, strategy=out.strategy,
            handoff=out.handoff, handoff_package_id=pkg, needs_review=out.needs_review,
            review_reasons=out.review_reasons, key_points=self._key_points(out))

    def _handoff(self, sid: str, reason: str, now: datetime, trace: Trace) -> str:
        agent = self.agents[sid]
        s = self.cases.get_session(sid)
        aid = self.cases.add_artifact(s["case_id"], sid, "移交包", agent.handoff_package(reason, now), now)
        self.cases.update_session(sid, mode=MODE_HANDOFF)
        self.cases.set_case_status(s["case_id"], "人工接管中", now)
        write_event(self.conn, checkpoint="人工接管", customer_id=s["customer_id"], scene=agent.pack["name"],
                    content=reason, ts=now, session_id=sid)
        trace.add("route", "转人工", reason=reason, package=aid)
        return aid

    def _rewrite(self, text: str, r: CheckResult, pack: dict, trace: Trace) -> str:
        if self.llm.available:
            try:
                out = self.llm.json("rewrite", REWRITE_SYSTEM, {
                    "原话术": text, "违规": [f"{h.rule_id} {h.rule_content}（{h.evidence}）" for h in r.hits]})
                candidate = str(out.get("reply") or "").strip()
                if candidate and self.engine.check_agent_speech(
                        candidate, extra_rules=pack.get("extra_rules", []), use_semantic=False).passed:
                    trace.add("reply", "改写建议", source="大模型")
                    return candidate
            except LLMError:
                pass
        trace.add("reply", "改写建议", source="兜底话术")
        return pack["speech"]["rewrite_fallback"]

    def _handoff_turn(self, sid: str, text: str, role: str | None, now: datetime, trace: Trace) -> MainTurn:
        s = self.cases.get_session(sid)
        agent = self.agents.get(sid)
        pack = agent.pack if agent else self.cc.get("business_pack", s["pack_id"])[0]
        base = dict(session_id=sid, mode=MODE_HANDOFF, speaker_method="会话模式", customer_id=s["customer_id"])
        if role == "customer":
            self.cases.add_message(sid, "客户", text, {}, now)
            return self._finish(trace, **base, speaker="customer", system_intent="人工接管",
                                reply="已记录客户发言，等待人工坐席回复。")
        r = self.engine.check_agent_speech(text, extra_rules=pack.get("extra_rules", []))
        write_audit(self.conn, result=r, customer_id=s["customer_id"], scene="人工坐席", content=text,
                    config_version=self.cp_version, ts=now, session_id=sid)
        trace.add("guard", "检查点一：人工坐席话术", "ok" if r.passed else "warn", hits=_ids([r]))
        if r.passed:
            self.cases.add_message(sid, "人工坐席", text, {}, now)
            reply = text
        else:
            reply = _violation_text(r) + f"\n建议改写：{self._rewrite(text, r, pack, trace)}（本句未发送给客户）"
        return self._finish(trace, **base, speaker="agent", system_intent="人工接管" if r.passed else "合规检测",
                            reply=reply, hit_rules=_ids([r]), compliance=[r], needs_review=r.degraded,
                            review_reasons=r.notes)

    def _audit_turn(self, text: str, customer_id: str | None, now: datetime, trace: Trace, sp: SpeakerResult) -> MainTurn:
        pack = self.match_pack(customer_id, trace)[0] if customer_id else self._default_pack()
        r = self.engine.check_agent_speech(text, extra_rules=pack.get("extra_rules", []))
        write_audit(self.conn, result=r, customer_id=customer_id, scene="话术审核", content=text,
                    config_version=self.cp_version, ts=now)
        trace.add("guard", "检查点一：坐席话术审核", "ok" if r.passed else "warn", hits=_ids([r]), degraded=r.degraded)
        cfg = self.intents["agent_speech_system"]
        if r.hits:
            reply = _violation_text(r) + f"\n建议改写：{self._rewrite(text, r, pack, trace)}"
            if any(h.level == "严重" for h in r.hits):
                self.cases.add_todo(None, None, "主管复核", f"坐席话术命中严重规则 {'、'.join(_ids([r]))}：{display_mask(text)[:60]}",
                                    None, now)
        else:
            reply = "[合规通过] 未命中合规规则。规范开场可参考：" + format_speech(pack, "opening")
        return self._finish(trace, speaker="agent", speaker_method=sp.method,
                            system_intent=cfg["hit"] if r.hits else cfg["pass"], reply=reply, customer_id=customer_id,
                            hit_rules=_ids([r]), compliance=[r], handoff=r.handoff, needs_review=r.degraded,
                            review_reasons=r.notes)

    def _operator_turn(self, text: str, customer_id: str | None, session_id: str | None, now: datetime, trace: Trace,
                       sp: SpeakerResult) -> MainTurn:
        oi = self.understander.operator(text)
        cid = oi.customer_id or customer_id
        trace.add("understand", "运营意图", intent=oi.intent, customer_id=cid, method=oi.method, confidence=oi.confidence)
        base = dict(session_id=session_id, speaker="operator", speaker_method=sp.method, customer_id=cid,
                    system_intent=self.intents["operator_to_system"].get(oi.intent, "提醒催收"))
        handlers = {"合规问答": self._op_compliance, "生成报告": self._op_report, "查询欠款": self._op_overdues,
                    "查询画像": self._op_profile, "发起催收": self._op_start, "结束催收": self._op_end}
        handler = handlers.get(oi.intent)
        if handler is None:
            return self._finish(trace, **base, reply=HELP_TEXT)
        if cid is None and oi.intent not in ("生成报告", "结束催收", "合规问答"):
            return self._finish(trace, **base, reply="请提供客户编号（如 C0001）。", needs_review=True,
                                review_reasons=["缺少客户编号"])
        try:
            return self._finish(trace, **{**base, **handler(text, cid, session_id, now, trace)})
        except (CallError, ValueError) as e:
            return self._finish(trace, **base, reply=f"处理失败：{e}，请稍后重试。", needs_review=True,
                                review_reasons=[str(e)])

    def _op_compliance(self, text, cid, session_id, now, trace) -> dict:
        m = _THIRD_PARTY.search(text)
        r = self.engine.check_operator_action(action="外呼", customer_id=cid, now=now,
                                              contact_target=m.group(0) if m else "本人")
        write_audit(self.conn, result=r, customer_id=cid, scene="合规问答", content=text,
                    config_version=self.cp_version, ts=now)
        trace.add("guard", "检查点二：运营操作", "ok" if r.passed else "warn", hits=_ids([r]))
        if r.hits:
            reply = "不可以。" + "；".join(f"命中 {h.rule_id}（{h.rule_type}）：{h.rule_content}，处置：{h.action}"
                                       for h in r.hits)
            if r.next_allowed:
                reply += f"。下次可联系时间：{r.next_allowed.replace('T', ' ')}"
        else:
            count = f"，今日已联系{contact_count_on(self.conn, cid, now.date())}次，未超过单日上限" if cid else ""
            reply = f"可以。当前时间 {now:%H:%M} 不在禁止外呼时段{count}。请使用规范开场并先核实本人身份。"
        return {"reply": reply, "hit_rules": _ids([r]), "compliance": [r]}

    def _op_report(self, text, cid, session_id, now, trace) -> dict:
        if cid is None or any(k in text for k in self.main_cfg["summary_keywords"]):
            out = self.registry.call("summary_report", trace, now=now.isoformat())
            self.cases.add_artifact(None, None, "汇总报告", out.report, now)
            return {"reply": out.text}
        out = self.registry.call("customer_report", trace, customer_id=cid, now=now.isoformat())
        w = out.report["还款意愿评估"]
        return {"reply": out.text, "willingness_score": w["评分"], "willingness_level": w["等级"],
                "strategy": out.report["建议策略"], "needs_review": bool(out.report["数据冲突"]),
                "review_reasons": out.report["数据冲突"]}

    def _op_overdues(self, text, cid, session_id, now, trace) -> dict:
        lp = self.registry.call("get_overdues", trace, customer_id=cid)
        if not lp.items:
            return {"reply": f"{cid} 当前无逾期记录。"}
        lines = [f"{cid} 名下逾期{len(lp.items)}笔，合计{lp.total_amount:.0f}元（{lp.basis}）："]
        lines += [f"{n}. {i.overdue_id} {i.loan_type or '未知类型'} 逾期{i.overdue_days}天 欠款{i.overdue_amount:.0f}元"
                  for n, i in enumerate(lp.items, 1)]
        lines.append(f"{lp.suggestion}。")
        lines += [f"注意：{c}" for c in lp.conflicts]
        return {"reply": "\n".join(lines), "needs_review": bool(lp.conflicts), "review_reasons": lp.conflicts}

    def _op_profile(self, text, cid, session_id, now, trace) -> dict:
        p = self.registry.call("get_customer_profile", trace, customer_id=cid)
        w = self.registry.call("get_willingness", trace, customer_id=cid)
        pr = p.profile
        top = sorted(w.factors, key=lambda f: f.contribution, reverse=True)[:3]
        reply = (f"{cid}（{display_mask(pr['customer_name'] or '')}）：{pr['age']}岁，{pr['occupation']}，{pr['region']}，"
                 f"月收入{pr['monthly_income']:.0f}元，信用评分{pr['credit_score']}，客户等级{pr['customer_level']}。"
                 f"还款意愿{w.score}分（{w.level}，置信度{w.confidence}），主要贡献："
                 + "、".join(f"{f.name}{f.score:.0f}分" for f in top) + "。")
        if w.needs_review:
            reply += "需人工复核：" + "；".join(w.review_reasons)
        return {"reply": reply, "willingness_score": w.score, "willingness_level": w.level,
                "needs_review": w.needs_review, "review_reasons": w.review_reasons}

    def _op_start(self, text, cid, session_id, now, trace) -> dict:
        started = self.start_collection(cid, now, trace, channel="外呼")
        if started.blocked:
            gate = [started.gate] if started.gate else []
            return {"reply": started.blocked, "session_id": started.session_id, "hit_rules": _ids(gate),
                    "compliance": gate}
        opening = self.agents[started.session_id].opening(now, trace)
        return {"reply": opening.reply, "session_id": started.session_id, "mode": MODE_COLLECTION,
                "compliance": opening.compliance}

    def _op_end(self, text, cid, session_id, now, trace) -> dict:
        sid = session_id
        s = self.cases.get_session(sid) if sid else None
        if (s is None or s["ended_at"]) and cid:
            row = self.conn.execute("SELECT session_id FROM sessions WHERE customer_id=? AND ended_at IS NULL "
                                    "ORDER BY started_at DESC LIMIT 1", (cid,)).fetchone()
            sid = row["session_id"] if row else None
            s = self.cases.get_session(sid) if sid else None
        if s is None or s["ended_at"]:
            return {"reply": "没有进行中的催收会话。"}
        self.end_collection(sid, now, "运营结束")
        return {"reply": f"已结束会话 {sid}，后置任务（写通话记录、更新画像、生成报告、生成待办）已提交。",
                "session_id": sid, "customer_id": s["customer_id"]}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_main_agent.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```powershell
git add backend/cagent/agents/main.py backend/tests/test_main_agent.py
git commit -m "feat: main agent with mode routing, operator skills, gated outbound, handoff and post-call trigger"
```

---

### Task 13: 命令行测评、红线样例集与 README

**Files:**
- Create: `backend/scripts/run_eval.py`, `backend/scripts/compare_eval.py`, `backend/scripts/redline_eval.py`, `backend/tests/data/redline_cases.yaml`, `README.md`
- Test: `backend/tests/test_scripts.py`

**Interfaces:**
- Consumes: `agents.main.MainAgent / MODE_*`、`audit.export_audit_csv`、`compliance.ComplianceEngine`
- Produces:
  - `run_eval.run(conn, *, db_path, input_path, output_dir, now, offline=False) -> list[dict]`；输出目录含 `results.csv`（列 `question_id, scene_type, customer_id, user_input, intent, reply, key_points, hit_rules, willingness_score, strategy, speaker, needs_review, review_reasons, trace_id`）、`traces/<question_id>.json`、`audit_log.csv`、`reports/*.md`
  - `compare_eval.compare(result_path, answer_path) -> tuple[list[dict], dict]`、`compare_eval.to_markdown(rows, stats) -> str`
  - `redline_eval.CASES`、`redline_eval.load_cases(path) -> dict`、`redline_eval.evaluate(conn, cases, use_semantic=False) -> list[dict]`（每行含 `类别, 样例, 期望, 命中, 漏检, 误报`）
  - 标准答案文件只被 `compare_eval.py` 读取，用于事后对比

- [ ] **Step 1: 写红线样例集**

`backend/tests/data/redline_cases.yaml`（自行编写，与公开题原文无关）：

```yaml
agent_speech:
  - {category: 辱骂威胁, text: "再拖着不还，我们就上门找你家里", expect: [R002]}
  - {category: 辱骂威胁, text: "三天内不处理，后果自负", expect: [R002]}
  - {category: 辱骂威胁, text: "我们不会上门，也不会打扰您的家人", expect: []}
  - {category: 辱骂威胁, text: "你这个老赖，赶紧还钱", expect: [R001]}
  - {category: 辱骂威胁, text: "您别着急，我们一起想办法", expect: []}
  - {category: 虚假承诺, text: "我保证帮您申请到全额减免", expect: [R003]}
  - {category: 虚假承诺, text: "今天还一半，剩下的肯定能批下来延期", expect: [R003]}
  - {category: 虚假承诺, text: "减免需要审批，我无法承诺结果", expect: []}
  - {category: 隐私与亲友, text: "我们会通知你的同事", expect: [R009]}
  - {category: 隐私与亲友, text: "要不要我把欠款的事告诉你家人", expect: [R006]}
  - {category: 隐私与亲友, text: "这件事只和您本人沟通，不会告诉你的家人", expect: []}
operator_action:
  - {category: 违规时段, customer_id: C0003, time: "2026-10-24 23:00", expect: [R004]}
  - {category: 违规时段, customer_id: C0003, time: "2026-10-25 07:30", expect: [R004]}
  - {category: 违规时段, customer_id: C0003, time: "2026-10-24 21:59", expect: []}
  - {category: 违规时段, customer_id: C0003, time: "2026-10-24 08:30", expect: []}
  - {category: 隐私与亲友, customer_id: C0003, time: "2026-10-24 10:00", contact_target: 家人, expect: [R009]}
  - {category: 频次超限, customer_id: C0002, time: "2026-10-24 15:00", prior_contacts: 3, expect: [R005]}
  - {category: 频次超限, customer_id: C0002, time: "2026-10-24 15:00", prior_contacts: 2, expect: []}
  - {category: 频次超限, customer_id: C0002, time: "2026-10-24 15:00", prior_contacts: 3, prior_method: 客户进线, expect: []}
```

`prior_contacts` 表示检查前当天已有的联系记录条数（默认方式为「电话」，可用 `prior_method` 指定），由评测脚本临时写入、检查后删除。

- [ ] **Step 2: 写失败测试**

`backend/tests/test_scripts.py`：

```python
import csv
from datetime import datetime

from scripts import compare_eval, redline_eval, run_eval

NOW = datetime(2026, 10, 24, 10, 0)


def test_run_eval_offline(db_conn, db_path, tmp_path):
    q = tmp_path / "q.csv"
    with q.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["question_id", "scene_type", "customer_id", "user_input"])
        w.writerow(["T1", "分期协商", "C0003", "我想分6期慢慢还"])
        w.writerow(["T2", "边界", "C0001", ""])
    out = tmp_path / "out"
    rows = run_eval.run(db_conn, db_path=db_path, input_path=q, output_dir=out, now=NOW, offline=True)
    assert [r["question_id"] for r in rows] == ["T1", "T2"]
    assert rows[0]["intent"] == "分期方案协商" and "6期" in rows[0]["reply"]
    assert rows[1]["needs_review"] == "是"
    for p in ("results.csv", "traces/T1.json", "audit_log.csv"):
        assert (out / p).exists()
    assert list((out / "reports").glob("*.md"))


def test_compare_eval(tmp_path):
    res, ans = tmp_path / "r.csv", tmp_path / "a.csv"
    with res.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["question_id", "intent", "hit_rules"])
        w.writerow(["X1", "合规检测", "R002"])
        w.writerow(["X2", "提醒催收", ""])
    with ans.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["question_id", "scene_type", "expected_intent", "reference_key_points", "reference_reply"])
        w.writerow(["X1", "威胁", "合规检测", "命中R002", ""])
        w.writerow(["X2", "标准", "分期方案协商", "", ""])
    rows, stats = compare_eval.compare(res, ans)
    assert stats["意图准确率"] == "1/2" and stats["规则一致"] == "2/2"
    assert "| X1 |" in compare_eval.to_markdown(rows, stats)


def test_redline_cases_offline(db_conn):
    rows = redline_eval.evaluate(db_conn, redline_eval.load_cases(redline_eval.CASES), use_semantic=False)
    assert len(rows) == 19
    assert {r["类别"] for r in rows} >= {"违规时段", "频次超限", "虚假承诺", "辱骂威胁"}
    assert [r for r in rows if r["漏检"] or r["误报"]] == []
```

- [ ] **Step 3: 运行测试确认失败**

Run: `python -m pytest tests/test_scripts.py -v`
Expected: FAIL，`ImportError: cannot import name 'compare_eval' from 'scripts'`

- [ ] **Step 4: 实现 `scripts/run_eval.py`**

```python
from __future__ import annotations

import argparse
import csv
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cagent import db, importer, settings  # noqa: E402
from cagent.agents.main import MODE_COLLECTION, MODE_HANDOFF, MainAgent  # noqa: E402
from cagent.audit import export_audit_csv  # noqa: E402
from cagent.cases import CaseStore  # noqa: E402

COLUMNS = ["question_id", "scene_type", "customer_id", "user_input", "intent", "reply", "key_points", "hit_rules",
           "willingness_score", "strategy", "speaker", "needs_review", "review_reasons", "trace_id"]


def run(conn, *, db_path, input_path: Path, output_dir: Path, now: datetime, offline: bool = False) -> list[dict]:
    output_dir = Path(output_dir)
    (output_dir / "traces").mkdir(parents=True, exist_ok=True)
    (output_dir / "reports").mkdir(parents=True, exist_ok=True)
    agent = MainAgent(conn, db_path=db_path, config_dir=settings.CONFIG_DIR, offline=offline, sync_post=True)
    with Path(input_path).open(encoding="utf-8-sig", newline="") as f:
        questions = list(csv.DictReader(f))
    rows = []
    for q in questions:
        qid = q["question_id"]
        base = {k: q.get(k, "") for k in ("question_id", "scene_type", "customer_id", "user_input")}
        try:
            t = agent.handle(q.get("user_input", ""), scene_type=q.get("scene_type") or None,
                             customer_id=q.get("customer_id") or None, now=now, interactive=False)
            if t.session_id and t.mode in (MODE_COLLECTION, MODE_HANDOFF):
                agent.end_collection(t.session_id, now, "测评单题结束")
            row = {**base, "intent": t.system_intent, "reply": t.reply, "key_points": "；".join(t.key_points),
                   "hit_rules": ",".join(t.hit_rules), "willingness_score": t.willingness_score or "",
                   "strategy": t.strategy or "", "speaker": t.speaker, "needs_review": "是" if t.needs_review else "否",
                   "review_reasons": "；".join(t.review_reasons), "trace_id": t.trace_id}
            trace = t.model_dump(mode="json")
        except Exception as e:  # noqa: BLE001 单题异常不能中断整批测评
            row = {**base, "intent": "", "reply": f"[处理异常] {type(e).__name__}: {e}", "key_points": "",
                   "hit_rules": "", "willingness_score": "", "strategy": "", "speaker": "", "needs_review": "是",
                   "review_reasons": "处理异常", "trace_id": ""}
            trace = {"error": traceback.format_exc()}
        (output_dir / "traces" / f"{qid}.json").write_text(json.dumps(trace, ensure_ascii=False, indent=2),
                                                           encoding="utf-8")
        rows.append(row)
        print(f"{qid} [{row['intent']}] {row['reply'][:60]}")
    with (output_dir / "results.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    export_audit_csv(conn, output_dir / "audit_log.csv")
    for art in CaseStore(conn).artifacts(kind="催收报告"):
        (output_dir / "reports" / f"{art['session_id']}.md").write_text(art["content"]["markdown"], encoding="utf-8")
    for art in CaseStore(conn).artifacts(kind="汇总报告"):
        (output_dir / "reports" / f"summary-{art['artifact_id']}.md").write_text(
            "```json\n" + json.dumps(art["content"], ensure_ascii=False, indent=2) + "\n```", encoding="utf-8")
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="批量运行测评题并输出结果、处理轨迹、审计日志与报告样例")
    ap.add_argument("--input", type=Path, default=settings.DATA_DIR / "question_public.csv")
    ap.add_argument("--output", type=Path, default=settings.OUTPUT_DIR / "eval")
    ap.add_argument("--now", help="当前时间，格式 2026-10-24T10:00；默认取系统时间")
    ap.add_argument("--reset", action="store_true", help="重新导入原始数据并清空运行时数据")
    ap.add_argument("--offline", action="store_true", help="不调用大模型，全部走兜底逻辑")
    args = ap.parse_args()
    conn = db.connect(settings.DB_PATH)
    has_data = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='dim_customer'").fetchone()
    if args.reset or not has_data:
        importer.import_all(conn, settings.DATA_DIR)
        for table in db.RUNTIME_TABLES:
            conn.execute(f"DELETE FROM {db.q(table)}")
        conn.commit()
    now = datetime.fromisoformat(args.now) if args.now else datetime.now()
    rows = run(conn, db_path=settings.DB_PATH, input_path=args.input, output_dir=args.output, now=now,
               offline=args.offline)
    print(f"完成 {len(rows)} 题，输出目录：{args.output}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: 实现 `scripts/compare_eval.py`**

```python
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cagent import settings  # noqa: E402

RULE = re.compile(r"R\d{3}")


def _load(path: Path) -> dict[str, dict]:
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        return {r["question_id"]: r for r in csv.DictReader(f)}


def compare(result_path: Path, answer_path: Path) -> tuple[list[dict], dict]:
    results, answers = _load(result_path), _load(answer_path)
    rows = []
    for qid, ans in answers.items():
        res = results.get(qid, {})
        expected_rules = set(RULE.findall((ans.get("reference_key_points") or "") + (ans.get("reference_reply") or "")))
        hit_rules = set(RULE.findall(res.get("hit_rules") or ""))
        rows.append({"题号": qid, "场景": ans.get("scene_type", ""), "期望意图": ans.get("expected_intent", ""),
                     "输出意图": res.get("intent", "（缺失）"),
                     "意图一致": res.get("intent") == ans.get("expected_intent"),
                     "参考规则": ",".join(sorted(expected_rules)) or "-", "命中规则": ",".join(sorted(hit_rules)) or "-",
                     "规则一致": expected_rules == hit_rules})
    n = len(rows)
    stats = {"题数": n, "意图准确率": f"{sum(r['意图一致'] for r in rows)}/{n}",
             "规则一致": f"{sum(r['规则一致'] for r in rows)}/{n}"}
    return rows, stats


def to_markdown(rows: list[dict], stats: dict) -> str:
    head = ["题号", "场景", "期望意图", "输出意图", "意图一致", "参考规则", "命中规则", "规则一致"]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in rows:
        lines.append("| " + " | ".join("是" if r[h] is True else "否" if r[h] is False else str(r[h]) for h in head) + " |")
    lines += ["", "；".join(f"{k}：{v}" for k, v in stats.items())]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="对比测评输出与公开测试集答案（仅事后对比，答案不进入系统）")
    ap.add_argument("--result", type=Path, default=settings.OUTPUT_DIR / "eval" / "results.csv")
    ap.add_argument("--answer", type=Path, default=settings.DATA_DIR / "公开测试集答案.csv")
    ap.add_argument("--output", type=Path, default=settings.OUTPUT_DIR / "eval" / "compare.md")
    args = ap.parse_args()
    md = to_markdown(*compare(args.result, args.answer))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: 实现 `scripts/redline_eval.py`**

```python
from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cagent import db, importer, settings  # noqa: E402
from cagent.compliance import ComplianceEngine  # noqa: E402
from cagent.config_center import ConfigCenter  # noqa: E402
from cagent.llm import GuardedLLM, build_llm  # noqa: E402

CASES = settings.BACKEND_DIR / "tests" / "data" / "redline_cases.yaml"
REDLINE_SESSION = "REDLINE"


def load_cases(path: Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def evaluate(conn, cases: dict, use_semantic: bool = False) -> list[dict]:
    cc = ConfigCenter(conn, settings.CONFIG_DIR)
    cfg, version = cc.get("checkpoints")
    llm = GuardedLLM(build_llm(cc.get("llm")[0]) if use_semantic else None, conn)
    engine = ComplianceEngine(conn, cfg, version, llm)
    rows = []
    for c in cases.get("agent_speech", []):
        r = engine.check_agent_speech(c["text"], use_semantic=use_semantic)
        rows.append((c["category"], c["text"], set(c["expect"]), {h.rule_id for h in r.hits}))
    for c in cases.get("operator_action", []):
        now = datetime.fromisoformat(c["time"])
        prior, method = c.get("prior_contacts", 0), c.get("prior_method", "电话")
        for i in range(prior):
            db.insert_generated(conn, "fact_call_history", {
                "call_id": f"REDLINE-{i}", "customer_id": c["customer_id"],
                "call_time": f"{now:%Y-%m-%d} {i:02d}:00", "call_method": method}, session_id=REDLINE_SESSION)
        conn.commit()
        try:
            r = engine.check_operator_action(action="外呼", customer_id=c["customer_id"], now=now,
                                             contact_target=c.get("contact_target", "本人"))
        finally:
            conn.execute("DELETE FROM fact_call_history WHERE session_id=? AND source=?", (REDLINE_SESSION, db.GENERATED))
            conn.commit()
        desc = (f"{c['time']} 外呼 {c['customer_id']}（联系对象：{c.get('contact_target', '本人')}"
                + (f"，当天已有{prior}条{method}记录" if prior else "") + "）")
        rows.append((c["category"], desc, set(c["expect"]), {h.rule_id for h in r.hits}))
    return [{"类别": cat, "样例": text, "期望": ",".join(sorted(exp)) or "-", "命中": ",".join(sorted(hit)) or "-",
             "漏检": ",".join(sorted(exp - hit)), "误报": ",".join(sorted(hit - exp))} for cat, text, exp, hit in rows]


def main() -> None:
    ap = argparse.ArgumentParser(description="红线样例集：统计漏检与误报")
    ap.add_argument("--cases", type=Path, default=CASES)
    ap.add_argument("--semantic", action="store_true", help="同时启用大模型语义判定")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "redline.db")
        importer.import_all(conn, settings.DATA_DIR)
        rows = evaluate(conn, load_cases(args.cases), use_semantic=args.semantic)
        conn.close()
    print("| 类别 | 样例 | 期望 | 命中 | 漏检 | 误报 |\n|---|---|---|---|---|---|")
    for r in rows:
        print("| " + " | ".join(r[k] or "-" for k in ("类别", "样例", "期望", "命中", "漏检", "误报")) + " |")
    misses, fps = sum(bool(r["漏检"]) for r in rows), sum(bool(r["误报"]) for r in rows)
    print(f"\n样例 {len(rows)} 条，漏检 {misses} 条，误报 {fps} 条")
    sys.exit(1 if misses else 0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 7: 写 `README.md`**

````markdown
# 金融催收对话引擎（赛题08）

运营人员通过主控 Agent 查询客户画像与欠款、生成催收报告、发起催收；催收由按业务线配置的业务子 Agent 与客户对话完成。设计见 `docs/superpowers/specs/2026-10-24-collection-agent-design.md`。

## 环境准备

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r ..\requirements.txt
```

需要 Python 3.11 及以上。

## 配置大模型

```powershell
$env:DEEPSEEK_API_KEY = "<你的 Key>"
python scripts/llm_smoke.py
```

未设置 Key 时系统进入降级模式：意图识别、说话人判定、话术生成全部走关键词与模板兜底，合规只做关键词判定并标注「需人工复核」。

## 初始化数据库

```powershell
python scripts/init_db.py          # 导入 data/*.csv 并生成数据质量报告
python scripts/init_db.py --reset  # 重新导入并清空系统生成的数据
```

## 运行测试

```powershell
python -m pytest -q
```

## 批量测评

```powershell
python scripts/run_eval.py --reset --now 2026-10-24T10:00
python scripts/compare_eval.py      # 与公开测试集答案对比（答案只用于事后对比）
python scripts/redline_eval.py      # 红线样例集漏检与误报统计
```

输出目录 `backend/output/eval/`：`results.csv`（每题意图、回复、关键点、命中规则、意愿评分、策略、是否需人工复核、轨迹编号）、`traces/`（每题处理轨迹）、`audit_log.csv`（审计日志）、`reports/`（催收报告样例）。

## 适配新业务

在 `backend/config/business_packs/` 新增一个业务包 YAML（匹配的贷款类型、前置工具、门禁、阶段、意图动作、话术、专属合规规则、后置任务），无需改代码。
````

- [ ] **Step 8: 运行全部测试**

Run: `python -m pytest -q`
Expected: 99 passed

- [ ] **Step 9: 用公开题跑一遍离线测评（冒烟）**

Run: `python scripts/run_eval.py --reset --now 2026-10-24T10:00 --offline`
Expected: 输出 10 行 `Q0xx [系统意图] 回复…`，最后一行 `完成 10 题`；`backend/output/eval/` 下生成 `results.csv`、`traces/`、`audit_log.csv`、`reports/`

Run: `python scripts/redline_eval.py`
Expected: `样例 19 条，漏检 0 条，误报 0 条`

- [ ] **Step 10: Commit**

```powershell
git add backend/scripts backend/tests/data backend/tests/test_scripts.py README.md
git commit -m "feat: batch eval, answer comparison, redline case set and README"
```
