"""临时演示启动脚本(用完即删):用指定临时库在指定端口起工作台。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from app.config import AppSettings  # noqa: E402
from app.main import create_app  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True, help="临时 DuckDB 路径")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()
    if not args.db.exists():
        raise SystemExit(f"演示库不存在: {args.db}")
    app = create_app(AppSettings(database_path=args.db))
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
