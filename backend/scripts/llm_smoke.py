import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

from cagent import settings  # noqa: E402
from cagent.llm import build_llm  # noqa: E402

cfg = yaml.safe_load((settings.CONFIG_DIR / "llm.yaml").read_text(encoding="utf-8"))
client = build_llm(cfg)
if client is None:
    sys.exit("未设置 DEEPSEEK_API_KEY")
print(client.complete_json('输出 json，例如 {"ok": true}', "请返回 ok=true 的 json"))
