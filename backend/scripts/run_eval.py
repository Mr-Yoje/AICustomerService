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
