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
    ap.add_argument("--result", type=Path, default=settings.OUTPUT_DIR / "eval_public" / "results.csv")
    ap.add_argument("--answer", type=Path, default=settings.DATA_DIR / "公开测试集答案.csv")
    ap.add_argument("--output", type=Path, default=settings.OUTPUT_DIR / "eval_public" / "compare.md")
    args = ap.parse_args()
    md = to_markdown(*compare(args.result, args.answer))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
