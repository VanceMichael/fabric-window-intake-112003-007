#!/usr/bin/env python3
"""端到端演练：拨动资料端时钟，跑通收件、核对、停料、追回与退回重核。

用法：
    python3 run_scenario.py                 # 进程内临时起资料端，拨钟 0..6
    python3 run_scenario.py --base URL      # 连接已运行的资料端
    python3 run_scenario.py --store DIR     # 证据库落盘目录（默认临时目录）
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from threading import Thread

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cutter.client import SourceClient
from cutter.monitor import Monitor
from cutter.report import format_turn
from cutter.store import EvidenceStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="已运行的资料端地址；缺省则进程内临时起一个")
    parser.add_argument("--store", help="证据库目录；缺省用临时目录")
    parser.add_argument("--turns", type=int, default=7, help="演练轮次数（默认 7）")
    args = parser.parse_args()

    server = None
    base = args.base
    if not base:
        from source_service import create_source

        server = create_source()
        thread = Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
    client = SourceClient(base)
    store_dir = args.store or tempfile.mkdtemp(prefix="evidence-")
    monitor = Monitor(client, EvidenceStore(store_dir))
    print(f"资料端 {base}，证据库 {store_dir}")
    try:
        for turn in range(args.turns):
            client.advance(turn)
            print(format_turn(monitor.run_turn()))
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
