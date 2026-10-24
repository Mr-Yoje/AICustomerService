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

复制仓库根目录的 `.env.example` 为 `.env`，填入 Key（`.env` 已被 git 忽略，不要提交）：

```text
DEEPSEEK_API_KEY=<你的 Key>
```

```powershell
python scripts/llm_smoke.py
```

已设置的同名环境变量优先于 `.env`；可用 `CAGENT_ENV_FILE` 指定其他文件。未设置 Key 时系统进入降级模式：意图识别、说话人判定、话术生成全部走关键词与模板兜底，合规只做关键词判定并标注「需人工复核」。

## 初始化数据库

```powershell
python scripts/init_db.py          # 导入 data/*.csv 并生成数据质量报告
python scripts/init_db.py --reset  # 重新导入并清空系统生成的数据
```

## 启动界面

前端在独立仓库 [AICustomerService-web](https://github.com/Mr-Yoje/AICustomerService-web)，克隆到本仓库的同级目录后，在本仓库根目录执行：

```powershell
.\start.ps1            # 打包前端并启动后端，访问 http://127.0.0.1:8000
.\start.ps1 -Offline   # 不调用大模型（降级模式）
.\start.ps1 -SkipWebBuild
```

也可只启动后端：`python backend/scripts/serve.py --port 8000 [--offline] [--web-dist <前端 dist 目录>]`。接口文档见 `http://127.0.0.1:8000/docs`，接口与页面设计见 `docs/superpowers/specs/2026-10-24-api-frontend-design.md`。

- 对话台：选择客户后以运营身份查询画像、欠款、报告或发起催收；可切换为客户进线、坐席话术审核、人工坐席；右栏实时展示处理轨迹与本轮结论。
- 案件与审计：案件的对话记录、催收报告、移交包、后置任务（失败可重试）、待办、会话记忆纠正与清除；审计日志筛选与 CSV 导出。
- 运营管理台：业务包（表单与 YAML 编辑、保存前校验、版本历史、复制新建）、评分与策略、合规检查点、意图映射；合规规则库、分期模板、工具注册表只读展示。

## 运行测试

```powershell
python -m pytest -q
```

## 批量测评

```powershell
python scripts/run_eval.py --reset
python scripts/compare_eval.py      # 与公开测试集答案对比（答案只用于事后对比）
python scripts/redline_eval.py      # 红线样例集漏检与误报统计
```

输出目录 `backend/output/eval/`：`results.csv`（每题意图、回复、关键点、命中规则、意愿评分、策略、是否需人工复核、轨迹编号）、`traces/`（每题处理轨迹）、`audit_log.csv`（审计日志）、`reports/`（催收报告样例）。

## 适配新业务

在运营管理台「业务包」中复制一个现有业务包，再调整适用贷款类型、前置工具、门禁、阶段、意图动作、话术、专属合规规则与后置任务，保存校验通过即生效，无需改代码；也可在 `backend/config/business_packs/` 新增业务包 YAML 作为初始配置。
