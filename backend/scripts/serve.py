import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402

from api.app import create_app  # noqa: E402
from api.service import Service  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description="启动催收对话引擎接口服务（同时托管前端静态文件）")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--offline", action="store_true", help="不调用大模型，全部走规则兜底")
    p.add_argument("--web-dist", type=Path, default=None, help="前端打包目录，默认读取 WEB_DIST 或同级前端仓库的 dist")
    a = p.parse_args()
    app = create_app(Service.from_settings(offline=a.offline), web_dist=a.web_dist)
    uvicorn.run(app, host=a.host, port=a.port)


if __name__ == "__main__":
    main()
