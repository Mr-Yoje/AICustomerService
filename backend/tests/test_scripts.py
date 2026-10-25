import csv
from datetime import datetime

from cagent import db, settings
from scripts import compare_eval, redline_eval, run_eval

NOW = datetime(2026, 10, 24, 10, 0)


def test_prepare_syncs_collection_raw_data(tmp_path, monkeypatch):
    from cagent import db, importer, settings

    collection = tmp_path / "collection.db"
    src = db.connect(collection)
    importer.import_all(src, settings.DATA_DIR)
    src.execute(
        "INSERT INTO sessions(session_id, customer_id, mode, started_at) VALUES ('S-live', 'C0001', '催收对话模式', '2026-10-24T20:00:00')"
    )
    src.commit()
    src.close()
    eval_db = tmp_path / "eval.db"
    monkeypatch.setattr(settings, "COLLECTION_DB_PATH", collection)
    monkeypatch.setattr(settings, "DB_PATH", eval_db)
    dest = db.connect(eval_db)
    run_eval.prepare(dest, settings.COLLECTION_DB_PATH)
    assert dest.execute("SELECT COUNT(*) FROM dim_customer").fetchone()[0] == 10
    assert dest.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    src = db.connect(collection)
    assert src.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    src.close()
    dest.close()


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


def test_prepare_suite_separates_public_and_private_data(tmp_path):
    public, private = db.connect(tmp_path / "public.db"), db.connect(tmp_path / "private.db")
    run_eval.prepare_suite(public, "public")
    run_eval.prepare_suite(private, "private")
    assert public.execute("SELECT COUNT(*) FROM dim_customer").fetchone()[0] == 10
    assert public.execute("SELECT COUNT(*) FROM dim_customer WHERE customer_id='C0011'").fetchone()[0] == 0
    assert private.execute("SELECT COUNT(*) FROM dim_customer WHERE customer_id='C0011'").fetchone()[0] == 1
    assert public.execute("SELECT customer_id FROM dim_loan WHERE loan_id='L0001'").fetchone()[0] == (
        private.execute("SELECT customer_id FROM dim_loan WHERE loan_id='L0001'").fetchone()[0]
    )
    public.close()
    private.close()


def test_public_questions_decode_gbk():
    rows = run_eval.read_questions(settings.DATA_DIR / "question_public.csv")
    assert [r["question_id"] for r in rows] == [f"Q{i:03d}" for i in range(1, 11)]
    assert "催收" in rows[0]["scene_type"]


def test_public_and_private_suites_write_separate_results():
    public, private = run_eval.suite_layout("public"), run_eval.suite_layout("private")
    assert public["db"] != private["db"]
    assert public["output"] != private["output"]
    assert public["output"].name == "eval_public"
    assert private["output"].name == "eval_private"
    assert public["input"].name == "question_public.csv"
    assert "非公开" in private["input"].name


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
