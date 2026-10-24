import os

from cagent.settings import load_env


def test_load_env_sets_missing_and_keeps_existing(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# 注释\nCAGENT_T1=abc\nexport CAGENT_T2="x y"\nCAGENT_T3=new\nbad line\n', encoding="utf-8")
    monkeypatch.delenv("CAGENT_T1", raising=False)
    monkeypatch.delenv("CAGENT_T2", raising=False)
    monkeypatch.setenv("CAGENT_T3", "old")
    load_env(env)
    assert os.environ["CAGENT_T1"] == "abc"
    assert os.environ["CAGENT_T2"] == "x y"
    assert os.environ["CAGENT_T3"] == "old"
    monkeypatch.delenv("CAGENT_T1")
    monkeypatch.delenv("CAGENT_T2")


def test_load_env_missing_file_is_noop(tmp_path):
    load_env(tmp_path / "none.env")
