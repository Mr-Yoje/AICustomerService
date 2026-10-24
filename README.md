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
