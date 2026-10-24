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
