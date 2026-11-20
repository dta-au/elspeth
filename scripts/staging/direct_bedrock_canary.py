"""Probe direct Bedrock streaming from inside a running web container.

It makes real provider calls (one short call per model and one long call that
may stream for more than five minutes) without changing the deployment: no
configuration, gateway or secret is altered. Results are written to a private
directory inside the container and progress is printed as JSON lines.

Safety properties (named by tests in
``tests/unit/scripts/staging/test_direct_bedrock_canary.py``):

* The key is read from a hidden prompt and sent once on the probe's stdin; it is
  never in argv, the environment, a file or this process's output.
* The probe's stderr is withheld; only the remote program's own JSON events are
  relayed, and those carry key-redacted error summaries.
* Every request must go to ``bedrock-runtime.<region>.amazonaws.com`` and a
  ``/converse-stream`` path.
* The probe spends provider quota, so it is dry run unless ``--execute``.
"""

from __future__ import annotations

import argparse
import getpass
import json
import subprocess
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import IO, Protocol

from scripts.staging import _bedrock_canary_remote as remote
from scripts.staging.opslib import UsageError, add_execute_flag, emit, run_tool, setting

REMOTE_SOURCE = Path(remote.__file__).read_text(encoding="utf-8")


class ProbeProcess(Protocol):
    stdin: IO[bytes] | None
    stdout: IO[bytes] | None
    stderr: IO[bytes] | None

    def wait(self) -> int: ...


def popen(argv: Sequence[str]) -> ProbeProcess:
    return subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.staging.direct_bedrock_canary",
        description=(
            "Probe direct Bedrock streaming (including a call that streams beyond five minutes) from inside a "
            "running web container, without changing it. The key is read from a hidden prompt. Real provider "
            "calls cost money: dry run unless --execute."
        ),
    )
    parser.add_argument("--container", help="running web container to run the probe in (env ELSPETH_OPS_WEB_CONTAINER)")
    parser.add_argument("--region", help="AWS region of the Bedrock runtime endpoint (env ELSPETH_OPS_BEDROCK_REGION)")
    parser.add_argument(
        "--quick-model", help="model or inference profile for the short authentication call (env ELSPETH_OPS_BEDROCK_QUICK_MODEL)"
    )
    parser.add_argument("--long-model", help="model or inference profile for the long streaming call (env ELSPETH_OPS_BEDROCK_LONG_MODEL)")
    parser.add_argument(
        "--container-python", default="/opt/venv/bin/python", help="interpreter inside the container (default /opt/venv/bin/python)"
    )
    parser.add_argument(
        "--result-root",
        default="/app/state/data/operator_tasks/direct-bedrock-canary",
        help="private result directory inside the container (default under /app/state/data/operator_tasks)",
    )
    parser.add_argument("--observe-seconds", type=float, default=660.0, help="stop reading a stream after this long (default 660)")
    parser.add_argument("--long-max-tokens", type=int, default=60000)
    parser.add_argument("--no-sudo", action="store_true", help="run docker without sudo -n")
    add_execute_flag(parser)
    return parser


def probe_argv(args: argparse.Namespace, container: str) -> list[str]:
    return [
        *([] if args.no_sudo else ["sudo", "-n"]),
        *("docker", "exec", "-i", container, args.container_python, "-B", "-u", "-c", REMOTE_SOURCE),
    ]


def main(
    argv: Sequence[str] | None = None,
    *,
    prompt_secret: Callable[[str], str] = getpass.getpass,
    start: Callable[[Sequence[str]], ProbeProcess] = popen,
) -> int:
    args = build_parser().parse_args(argv)
    container = setting(args.container, flag="--container", env="ELSPETH_OPS_WEB_CONTAINER")
    region = setting(args.region, flag="--region", env="ELSPETH_OPS_BEDROCK_REGION")
    try:
        host = remote.bedrock_host(region)
    except ValueError:
        raise UsageError("--region must be an AWS region name such as xx-east-1") from None
    quick = setting(args.quick_model, flag="--quick-model", env="ELSPETH_OPS_BEDROCK_QUICK_MODEL")
    long = setting(args.long_model, flag="--long-model", env="ELSPETH_OPS_BEDROCK_LONG_MODEL")
    if not args.execute:
        emit(
            "dry_run",
            container=container,
            endpoint=host,
            calls=[f"{quick}: short", f"{long}: long stream", f"{long}: short credential check after the long call"],
            result_root=args.result_root,
            note="no key was requested and nothing was run; pass --execute to probe",
        )
        return 0
    key = prompt_secret("Bedrock key (hidden input): ")
    if key == "":
        raise UsageError("no key entered")
    config = {
        "key": key,
        "region": region,
        "quick_model": quick,
        "long_model": long,
        "result_root": args.result_root,
        "observe_seconds": args.observe_seconds,
        "long_max_tokens": args.long_max_tokens,
    }
    process = start(probe_argv(args, container))
    if process.stdin is None or process.stdout is None or process.stderr is None:
        raise UsageError("probe process has no pipes")
    process.stdin.write(json.dumps(config).encode())
    process.stdin.close()
    del config, key
    stderr = process.stderr
    withheld: list[bytes] = []
    reader = threading.Thread(target=lambda: withheld.append(stderr.read()), daemon=True)
    reader.start()
    for line in process.stdout:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if type(value) is dict and "event" in value:
            print(json.dumps(value), flush=True)
    code = process.wait()
    reader.join(timeout=10)
    if code:
        emit("probe_process_failed", exit_code=code, stderr_withheld=True)
    return 1 if code else 0


if __name__ == "__main__":
    raise SystemExit(run_tool(main))
