import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent


def load_env(path: Path) -> None:
    """读取 KEY=VALUE 形式的 .env；已存在的环境变量优先，不被覆盖。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip().removeprefix("export ").strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env(Path(os.getenv("CAGENT_ENV_FILE", REPO_DIR / ".env")))

DATA_DIR = Path(os.getenv("CAGENT_DATA_DIR", REPO_DIR / "赛题" / "赛题08_金融催收对话引擎" / "data"))
CONFIG_DIR = Path(os.getenv("CAGENT_CONFIG_DIR", BACKEND_DIR / "config"))
COLLECTION_DB_PATH = BACKEND_DIR / "runtime" / "collection.db"
DB_PATH = Path(os.getenv("CAGENT_DB_PATH", COLLECTION_DB_PATH))
OUTPUT_DIR = Path(os.getenv("CAGENT_OUTPUT_DIR", BACKEND_DIR / "output"))
