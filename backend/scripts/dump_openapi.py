import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.app import create_app  # noqa: E402
from api.service import Service  # noqa: E402

p = argparse.ArgumentParser(description="导出接口 OpenAPI 文档，供前端生成类型")
p.add_argument("output", type=Path)
a = p.parse_args()
spec = create_app(Service.from_settings(offline=True)).openapi()
a.output.parent.mkdir(parents=True, exist_ok=True)
a.output.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"已导出 {len(spec['paths'])} 个接口到 {a.output}")
