# 金融催收对话引擎（赛题08）

运营人员通过主控 Agent 查询客户画像与欠款、生成催收报告、发起催收；催收由按业务线配置的业务子 Agent 与客户对话完成。设计见 `docs/superpowers/specs/2026-10-24-collection-agent-design.md`。

需要 Python 3.11 及以上。下面的命令都在 PowerShell 里执行。两个仓库要放在同级目录：

```text
<工作目录>/
  AICustomerService/          # 本仓库
  AICustomerService-web/      # 前端，https://github.com/Mr-Yoje/AICustomerService-web
```

## 环境准备

在本仓库根目录：

```powershell
python -m venv backend\.venv
.\backend\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

打开 `.env`，填入 `DEEPSEEK_API_KEY`。这个文件已被 git 忽略，不要提交。已设置的同名环境变量优先于 `.env`。未设置 Key 时自动走降级模式：意图、说话人和话术用关键词与模板，合规只做关键词判定并标注「需人工复核」。

确认大模型能连通（在 `backend` 目录）：

```powershell
cd backend
python scripts/llm_smoke.py
```

## 初始化数据库

仍在 `backend` 目录。数据默认读 `赛题/赛题08_金融催收对话引擎/data/`，库文件写到 `backend/runtime/collection.db`。

```powershell
python scripts/init_db.py
```

要清空界面上产生的会话、报告等系统数据并重新导入原始表：

```powershell
python scripts/init_db.py --reset
```

## 启动

在本仓库根目录执行。如果终端还停在 `backend`，先 `cd ..`。`start.ps1` 会在同级目录找到前端仓库，需要时执行 `npm install` 和 `npm run build`，然后启动后端，由后端托管页面。

```powershell
.\start.ps1
```

浏览器打开 http://127.0.0.1:8000 。同一 WiFi 下的手机或电脑用启动时打印的局域网地址，例如 http://192.168.1.125:8000 。接口文档在相同地址的 `/docs`。当前 WiFi 若被 Windows 标成公用网络，需允许入站 TCP 8000，否则只有本机能打开。

```powershell
.\start.ps1 -Offline        # 不调用大模型
.\start.ps1 -SkipWebBuild   # 前端 dist 已打好，不再打包
.\start.ps1 -Port 8000
```

只启动后端、不打包前端时，在 `backend` 目录执行：

```powershell
python scripts/serve.py --port 8000
python scripts/serve.py --port 8000 --offline
python scripts/serve.py --port 8000 --web-dist D:\path\to\AICustomerService-web\dist
```

前端单独热更新时，先让后端在 8000 端口运行，再到前端仓库执行 `npm run dev`，打开 http://127.0.0.1:5173 。

页面：

- 对话台：选择客户后查询画像、欠款、报告或发起催收。程序先判断说话人；判断不清时才按底部选中的角色处理。
- 案件与审计：对话记录、催收报告、移交包、后置任务、待办、会话记忆；审计日志可导出 CSV。
- 成本：按会话汇总 token 和人民币费用，可按日期筛选。
- 运营管理台：业务包、评分与策略、合规检查点、意图映射；规则库、分期模板、工具注册表只读。

## 运行测试

在 `backend` 目录：

```powershell
python -m pytest -q
```

## 跑测评

公开测试题和非公开测试题分开跑，结果各一份。在 `backend` 目录执行。时钟用运行当时的系统时间，不要再传 `--now`。题目原文里写了时刻的（例如「晚上11点」「现在是22:30」）仍以题面为准。

公开题使用 `backend/runtime/eval_public.db`，非公开题使用 `backend/runtime/eval_private.db`。每次运行都从公开 CSV 重建对应测评库，并清空该库上次留下的会话和报告，不从 `collection.db` 复制，也不修改它。非公开测评库还会追加 `非公开加试/03_非公开题所需业务数据_现场提供.xlsx`：客户编号保持原号，主键冲突的记录改为 `P-` 前缀。公开答案只在对比时读取，不进入系统。

```powershell
cd backend
python scripts/run_eval.py --suite both
python scripts/compare_eval.py
```

只跑其中一套时把 `--suite both` 换成 `--suite public` 或 `--suite private`。

仓库里保留的评测结果是下面两个目录，不再提交 `backend/output/eval/`。

| 测评 | 启动 | 结果 |
| --- | --- | --- |
| 公开测试题 | `python scripts/run_eval.py --suite public` | `backend/output/eval_public/` |
| 非公开测试题 | `python scripts/run_eval.py --suite private` | `backend/output/eval_private/` |
| 两套一起 | `python scripts/run_eval.py --suite both` | 上面两个目录各一份 |

`compare.md` 只由 `python scripts/compare_eval.py` 写到公开结果目录。两个目录里其余文件相同：

| 文件 | 内容 |
| --- | --- |
| `results.csv` | 每题的意图、回复、要点、命中规则、意愿评分、策略、是否需复核、轨迹编号 |
| `compare.md` | 与 `赛题/赛题08_金融催收对话引擎/data/公开测试集答案.csv` 的意图、规则对照 |
| `traces/` | 每题处理轨迹 |
| `audit_log.csv` | 审计日志 |
| `reports/` | 催收报告样例 |

红线样例用临时库，不改 `collection.db` 和两套测评库。在 `backend` 目录执行，结束码 0 表示没有漏检：

```powershell
python scripts/redline_eval.py
python scripts/redline_eval.py --semantic   # 额外启用大模型语义判定
```

## 适配新业务

在运营管理台「业务包」中复制一个现有业务包，再调整适用贷款类型、前置工具、门禁、阶段、意图动作、话术、专属合规规则与后置任务，保存校验通过即生效，无需改代码；也可在 `backend/config/business_packs/` 新增业务包 YAML 作为初始配置。
