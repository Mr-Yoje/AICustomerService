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
from cagent.desensitize import export_mask, redact  # noqa: E402

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
            row = {**base, "intent": t.system_intent, "reply": export_mask(t.reply),
                   "key_points": export_mask("；".join(t.key_points)),
                   "hit_rules": ",".join(t.hit_rules), "willingness_score": t.willingness_score or "",
                   "strategy": t.strategy or "", "speaker": t.speaker, "needs_review": "是" if t.needs_review else "否",
                   "review_reasons": export_mask("；".join(t.review_reasons)), "trace_id": t.trace_id}
            trace = redact(t.model_dump(mode="json"))
        except Exception as e:  # noqa: BLE001 单题异常不能中断整批测评
            row = {**base, "intent": "", "reply": f"[处理异常] {type(e).__name__}: {e}", "key_points": "",
                   "hit_rules": "", "willingness_score": "", "strategy": "", "speaker": "", "needs_review": "是",
                   "review_reasons": "处理异常", "trace_id": ""}
            trace = redact({"error": traceback.format_exc()})
        (output_dir / "traces" / f"{qid}.json").write_text(json.dumps(trace, ensure_ascii=False, indent=2),
                                                           encoding="utf-8")
        rows.append(row)
        print(f"{qid} [{row['intent']}] {row['reply'][:60]}")
    with (output_dir / "results.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    export_audit_csv(conn, output_dir / "audit_log.csv")
    report_dir = output_dir / "reports"
    for old in report_dir.glob("*.md"):
        old.unlink()
    for art in CaseStore(conn).artifacts():
        content = art["content"]
        if art["kind"] == "汇总报告":
            text = "```json\n" + json.dumps(redact(content), ensure_ascii=False, indent=2) + "\n```"
            name = f"summary-{art['artifact_id']}.md"
        elif isinstance(content, dict) and content.get("markdown"):
            text = export_mask(content["markdown"])
            name = f"{art['session_id']}.md" if art["kind"] == "催收报告" and art["session_id"] else f"{art['artifact_id']}.md"
        else:
            continue
        (report_dir / name).write_text(text, encoding="utf-8")
    return rows


def prepare(conn, source_path: Path) -> dict:
    """每次测评前，用 collection.db 的原始导入数据覆盖当前测评库。"""
    source_path = Path(source_path)
    if not source_path.is_file():
        raise FileNotFoundError(f"找不到原始数据库: {source_path}")
    current = conn.execute("PRAGMA database_list").fetchone()["file"]
    if current and Path(current).resolve() == source_path.resolve():
        raise RuntimeError("测评库与 collection.db 是同一个文件，已停止，避免改动界面数据")
    source = db.connect(source_path)
    try:
        return importer.sync_raw(conn, source)
    finally:
        source.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="批量运行测评题并输出结果、处理轨迹、审计日志与报告样例")
    ap.add_argument("--input", type=Path, default=settings.DATA_DIR / "question_public.csv")
    ap.add_argument("--output", type=Path, default=settings.OUTPUT_DIR / "eval")
    ap.add_argument("--now", help="当前时间，格式 2026-10-24T10:00；默认取系统时间")
    ap.add_argument("--reset", action="store_true", help="兼容旧命令；每次运行都会从 collection.db 同步原始数据")
    ap.add_argument("--offline", action="store_true", help="不调用大模型，全部走兜底逻辑")
    args = ap.parse_args()
    conn = db.connect(settings.DB_PATH)
    synced = prepare(conn, settings.COLLECTION_DB_PATH)
    print(f"已从 {settings.COLLECTION_DB_PATH} 同步原始数据：客户 {synced['counts'].get('dim_customer', 0)} 位")
    now = datetime.fromisoformat(args.now) if args.now else datetime.now()
    rows = run(conn, db_path=settings.DB_PATH, input_path=args.input, output_dir=args.output, now=now,
               offline=args.offline)
    print(f"完成 {len(rows)} 题，输出目录：{args.output}")


if __name__ == "__main__":
    main()
