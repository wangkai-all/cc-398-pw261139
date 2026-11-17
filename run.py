#!/usr/bin/env python3
"""Run the three services locally: intake, merger, query, and the web app.

The query service also serves `web/`, so the browser only needs one origin.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from app.common import port_open  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=os.path.join(ROOT, "data", "runtime"))
    parser.add_argument("--intake-port", type=int, default=8301)
    parser.add_argument("--merger-port", type=int, default=8302)
    parser.add_argument("--query-port", type=int, default=8303)
    parser.add_argument("--no-seed", action="store_true")
    args = parser.parse_args()
    os.makedirs(args.data_dir, exist_ok=True)

    env = dict(os.environ)
    env["LEDGER_DATA_DIR"] = args.data_dir
    env["INTAKE_URL"] = f"http://127.0.0.1:{args.intake_port}"
    env["MERGER_URL"] = f"http://127.0.0.1:{args.merger_port}"

    for port in (args.intake_port, args.merger_port, args.query_port):
        if port_open(port):
            print(f"port {port} is already in use", file=sys.stderr)
            raise SystemExit(1)

    procs: list[subprocess.Popen] = []

    def start(module: str, port: int, extra: list[str] | None = None) -> subprocess.Popen:
        cmd = [sys.executable, "-E", "-s", "-m", module, "--port", str(port), "--data-dir", args.data_dir]
        cmd += extra or []
        proc = subprocess.Popen(cmd, cwd=ROOT, env=env)
        procs.append(proc)
        return proc

    def stop_all(*_: object) -> None:
        for proc in procs:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        deadline = time.time() + 5
        for proc in procs:
            try:
                proc.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                proc.kill()

    signal.signal(signal.SIGTERM, stop_all)
    signal.signal(signal.SIGINT, stop_all)

    try:
        start("app.intake", args.intake_port)
        _wait(args.intake_port, "intake")
        start("app.merger", args.merger_port)
        _wait(args.merger_port, "merger")
        start("app.query", args.query_port, ["--web-dir", os.path.join(ROOT, "web")])
        _wait(args.query_port, "query")
        if not args.no_seed:
            seed = subprocess.run(
                [sys.executable, "-E", "-s", "-m", "app.seed", "--source", "demand-meter"],
                cwd=ROOT,
                env=env,
            )
            if seed.returncode != 0:
                print("seed failed; continuing with an empty ledger", file=sys.stderr)
        print(f"\n  Open http://127.0.0.1:{args.query_port}/  (Ctrl-C to stop)\n", file=sys.stderr)
        while all(proc.poll() is None for proc in procs):
            time.sleep(0.4)
        if any(proc.poll() is not None for proc in procs):
            print("a service exited; shutting the rest down", file=sys.stderr)
    finally:
        stop_all()


def _wait(port: int, name: str) -> None:
    for _ in range(100):
        if port_open(port):
            return
        time.sleep(0.1)
    raise SystemExit(f"{name} did not start on port {port}")


if __name__ == "__main__":
    main()
