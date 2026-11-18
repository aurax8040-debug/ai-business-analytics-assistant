#!/usr/bin/env python3
"""全国经营驾驶舱 V1 — 唯一启动入口。

    python3 run.py                    # http://127.0.0.1:8765/
    python3 run.py --port 9000        # 指定端口
    python3 run.py --check            # 只做数据源/接口自检，不启动服务

数据源固定为 ETL V1 的 bi_data_layer_v1.db，以 SQLite mode=ro 只读打开。
"""
from __future__ import annotations

import argparse
import sys

from server import config
from server.api import dispatch
from server.app import configure_logging, make_server


def self_check() -> int:
    ok = True
    print(f"ETL 数据库: {config.ETL_DB}")
    print(f"存在: {config.ETL_DB.exists()}")
    for endpoint in ("/api/health", "/api/meta", "/api/dashboard", "/api/detail"):
        status, payload = dispatch(endpoint)
        flag = "OK " if status == 200 and payload.get("ok") else "FAIL"
        print(f"  [{flag}] {endpoint:<18} HTTP {status}")
        if status != 200:
            print("        ", payload.get("error"))
            ok = False
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="全国经营驾驶舱 V1")
    ap.add_argument("--host", default=config.HOST)
    ap.add_argument("--port", type=int, default=config.PORT)
    ap.add_argument("--check", action="store_true", help="只做自检，不启动服务")
    args = ap.parse_args()

    configure_logging()
    if args.check:
        return self_check()

    httpd = make_server(args.host, args.port)
    url = f"http://{args.host}:{args.port}/"
    print(f"全国经营驾驶舱 V1 已启动: {url}")
    print(f"数据源（只读）: {config.ETL_DB}")
    try:
        httpd.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
