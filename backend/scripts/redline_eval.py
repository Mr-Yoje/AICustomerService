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
