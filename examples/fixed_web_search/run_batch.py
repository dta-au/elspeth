"""Run a bounded, hermetic search-page batch through ELSPETH."""

import argparse
import csv
import json
import os
import pathlib
import resource
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import Counter

import yaml


def _report(*parts: object) -> None:
    sys.stdout.write(" ".join(str(part) for part in parts) + "\n")
    sys.stdout.flush()


def main(count: int) -> int:
    repo = pathlib.Path(__file__).resolve().parents[2]
    batch = pathlib.Path(tempfile.mkdtemp(prefix=f"elspeth-web-batch-{count}-"))
    environment = {**os.environ, "PYTHONPATH": f"{repo / 'src'}:{repo / 'elspeth-lints/src'}"}
    queries = ("Australian Taxation Office", "Commonwealth Bank", "No such agency")
    with (batch / "input.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("id", "search_text"))
        writer.writerows((index, queries[index % len(queries)]) for index in range(count))

    settings = yaml.safe_load((repo / "examples/fixed_web_search/settings_fixture.yaml").read_text())
    settings["sources"]["primary"]["options"]["path"] = str(batch / "input.csv")
    settings["sinks"]["results"]["options"]["path"] = str(batch / "results.jsonl")
    settings["sinks"]["search_failures"]["options"]["path"] = str(batch / "failures.jsonl")
    settings["landscape"]["url"] = f"sqlite:///{batch / 'audit.db'}"
    settings["payload_store"]["base_path"] = str(batch / "payloads")
    settings["concurrency"]["max_workers"] = 1
    # This rate is solely for the loopback fixture; public pages retain the
    # normal configured limit and must not inherit this batch setting.
    settings["rate_limit"] = {"enabled": True, "services": {"web_scrape": {"requests_per_minute": 60000}}}
    (batch / "settings.yaml").write_text(yaml.safe_dump(settings, sort_keys=False))
    _report("batch_dir", batch, "count", count)

    with (batch / "server.log").open("w") as server_log:
        server = subprocess.Popen(
            [sys.executable, str(repo / "examples/fixed_web_search/serve_fixture.py"), "--access-log", str(batch / "access.log")],
            stdout=server_log,
            stderr=subprocess.STDOUT,
            cwd=repo,
            env=environment,
        )
        try:
            for _ in range(50):
                if server.poll() is not None:
                    raise RuntimeError("fixture server exited before ready")
                try:
                    with urllib.request.urlopen("http://127.0.0.1:8213/health", timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("fixture server did not become ready")

            started = time.monotonic()
            with (batch / "run.out").open("w") as run_out, (batch / "run.err").open("w") as run_err:
                run = subprocess.Popen(
                    [str(repo / ".venv/bin/elspeth"), "run", "--settings", str(batch / "settings.yaml"), "--execute"],
                    stdout=run_out,
                    stderr=run_err,
                    cwd=repo,
                    env=environment,
                )
                last_report = started
                try:
                    while run.poll() is None:
                        time.sleep(2)
                        now = time.monotonic()
                        if now - last_report >= 30:
                            access_path = batch / "access.log"
                            requests = sum(1 for _ in access_path.open()) if access_path.exists() else 0
                            _report("progress_requests", requests, "elapsed_seconds", round(now - started, 1))
                            last_report = now
                except BaseException:
                    run.terminate()
                    try:
                        run.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        run.kill()
                        run.wait()
                    raise
            elapsed = time.monotonic() - started
            _report("run_exit", run.returncode, "elapsed_seconds", round(elapsed, 2))
            _report("max_child_rss_kb", resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
            if run.returncode != 0:
                sys.stderr.write((batch / "run.err").read_text()[-3000:] + "\n")
                return run.returncode
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()

    outcome_counts: Counter[int] = Counter()
    rows = 0
    ids: set[int] = set()
    with (batch / "results.jsonl").open() as handle:
        for line in handle:
            row = json.loads(line)
            outcome_counts[len(row["candidates"])] += 1
            ids.add(row["id"])
            rows += 1
    requests = sum(1 for _ in (batch / "access.log").open())
    conn = sqlite3.connect(batch / "audit.db")
    run_statuses = conn.execute("SELECT status, COUNT(*) FROM runs GROUP BY status").fetchall()
    node_statuses = conn.execute("SELECT status, COUNT(*) FROM node_states GROUP BY status").fetchall()
    call_counts = conn.execute("SELECT call_type, status, COUNT(*) FROM calls GROUP BY call_type, status").fetchall()
    conn.close()
    _report("rows", rows, "requests", requests, "candidate_counts", dict(sorted(outcome_counts.items())))
    _report("runs", run_statuses, "node_states", node_statuses, "calls", call_counts)
    expected_outcomes = {1: (count + 2) // 3, 2: (count + 1) // 3, 0: count // 3}
    if (
        rows != count
        or requests != count
        or ids != set(range(count))
        or dict(outcome_counts) != expected_outcomes
        or run_statuses != [("completed", 1)]
        or node_statuses != [("completed", count * 3)]
        or ("http", "success", count) not in call_counts
        or (batch / "failures.jsonl").exists()
    ):
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=10000)
    args = parser.parse_args()
    if not 3 <= args.count <= 10000:
        parser.error("--count must be between 3 and 10000")
    raise SystemExit(main(args.count))
