"""监管冻结解冻服务端入口。

用法：
    python -m reg_freeze.server --host 127.0.0.1 --port 8080 --db ./data/freeze.db
不传 --db 时使用内存库（仅演示/测试）。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reg_freeze.api import create_server
from reg_freeze.repository import Repository
from reg_freeze.service import FreezeService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="监管冻结解冻服务端")
    parser.add_argument("--host", default=os.environ.get("FREEZE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("FREEZE_PORT", "8080")))
    parser.add_argument(
        "--db",
        default=os.environ.get("FREEZE_DB", ":memory:"),
        help="SQLite 路径，默认内存库",
    )
    args = parser.parse_args(argv)

    repo = Repository(args.db)
    service = FreezeService(repo)
    httpd = create_server(service, host=args.host, port=args.port)
    print(f"监管冻结服务已启动：http://{args.host}:{args.port} （DB={args.db}）", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭服务……", flush=True)
    finally:
        httpd.server_close()
        repo.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
