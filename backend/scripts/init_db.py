import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cagent import db, importer, settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="导入赛题数据或清空系统生成数据")
    parser.add_argument("--reset", action="store_true", help="只删除系统生成数据与运行时记录")
    args = parser.parse_args()
    conn = db.connect(settings.DB_PATH)
    if args.reset:
        importer.reset_generated(conn)
        print("已清空系统生成数据")
    else:
        result = importer.import_all(conn, settings.DATA_DIR)
        print(f"导入完成：{result['counts']}；数据质量问题 {result['issues']} 条；数据库 {settings.DB_PATH}")
    conn.close()


if __name__ == "__main__":
    main()
