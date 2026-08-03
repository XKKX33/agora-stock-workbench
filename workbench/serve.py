"""本地工作台启动入口。

一条命令启动:

    python serve.py

做了三件事,顺序不能换:

1. 把 workbench/ 根目录放进 sys.path。`app.main` 里 `from engine.db import Store`
   是绝对导入,不这么做就只能在 workbench/ 目录里启动,换个目录立刻 ImportError。
2. 启动前把数据库状态打印出来。库文件不存在时**不建空库**,只明确告警——
   凭空造一个空库会把"还没采过数据"伪装成"有库但全空",这是最难查的一类问题。
3. 交给 uvicorn。表结构迁移和调度线程由 app.main 的 lifespan 负责,不在这里重复。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

WORKBENCH_ROOT = Path(__file__).resolve().parent
if str(WORKBENCH_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKBENCH_ROOT))

# --reload 会另起子进程重新导入 app.main,子进程不继承上面这行 sys.path。
# 把根目录写进 PYTHONPATH,子进程才找得到 engine/ 与 app/。
_py_path = os.environ.get("PYTHONPATH", "")
if str(WORKBENCH_ROOT) not in _py_path.split(os.pathsep):
    os.environ["PYTHONPATH"] = (
        f"{WORKBENCH_ROOT}{os.pathsep}{_py_path}" if _py_path else str(WORKBENCH_ROOT)
    )

from app.config import AppSettings  # noqa: E402


def _report_database(settings: AppSettings) -> None:
    """启动前如实报告数据库状态。缺库是警告而不是静默继续。"""
    path = settings.db_path
    if not path.exists():
        print(f"[警告] 数据库文件不存在: {path}")
        print("       页面会以「无数据」状态打开,盘后调度不会启动。")
        print("       先执行一次数据采集:python -m engine.run_scan --offline")
        return
    size_mb = path.stat().st_size / (1024 * 1024)
    print(f"[数据库] {path} ({size_mb:.1f} MB)")


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 Hermes 股票量化工作台")
    defaults = AppSettings()
    parser.add_argument("--host", default=defaults.host, help="监听地址")
    parser.add_argument("--port", type=int, default=defaults.port, help="监听端口")
    parser.add_argument(
        "--reload", action="store_true", help="改代码自动重启(开发用)"
    )
    args = parser.parse_args()

    _report_database(defaults)

    # 延迟导入:让 --help 不必等 uvicorn 加载
    import uvicorn

    print(f"[工作台] http://{args.host}:{args.port}")
    print(f"[接口文档] http://{args.host}:{args.port}/docs")
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
