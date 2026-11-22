"""Safety properties of scripts/staging/direct_bedrock_canary.py and its remote program.

No container or provider: a FakeProcess stands in for ``docker exec``.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from scripts.staging import _bedrock_canary_remote as remote
from scripts.staging import direct_bedrock_canary as canary
from scripts.staging.opslib import run_tool

KEY = "synthetic-probe-key-value-0001"
ARGS = ["--container", "web-a", "--region", "xx-east-1", "--quick-model", "quick-profile", "--long-model", "long-profile"]


class CapturingStdin(io.BytesIO):
    def __init__(self) -> None:
        super().__init__()
        self.sent = b""

    def close(self) -> None:
        self.sent = self.getvalue()
        super().close()


@dataclass
class FakeProcess:
    stdout_lines: list[bytes]
    stderr_text: bytes = b""
    code: int = 0
    stdin: CapturingStdin = field(default_factory=CapturingStdin)
    stdout: io.BytesIO = field(init=False)
    stderr: io.BytesIO = field(init=False)

    def __post_init__(self) -> None:
        self.stdout = io.BytesIO(b"".join(self.stdout_lines))
        self.stderr = io.BytesIO(self.stderr_text)

    def wait(self) -> int:
        return self.code


@dataclass
class Starter:
    process: FakeProcess
    argv: list[str] = field(default_factory=list)

    def __call__(self, argv: Sequence[str]) -> FakeProcess:
        self.argv = list(argv)
        return self.process


def test_error_text_has_the_key_removed() -> None:
    summary = remote.safe_error_summary(f"request failed for key {KEY} expired", KEY)
    assert KEY not in summary
    assert "<redacted>" in summary


@pytest.mark.parametrize("marker", ["bedrock-api-key-", "X-Amz-Signature=abc", "Authorization: x", "Bearer abc"])
def test_error_text_with_signed_or_key_material_is_withheld(marker: str) -> None:
    assert remote.safe_error_summary(f"upstream said {marker} here", "unrelated") == remote.WITHHELD


def test_error_text_is_bounded() -> None:
    assert len(remote.safe_error_summary("x" * 5000, "k")) == remote.ERROR_LIMIT


def test_requests_are_pinned_to_the_regional_streaming_endpoint() -> None:
    host = remote.bedrock_host("xx-east-1")
    remote.check_request(host, "/model/m/converse-stream", expected_host=host)
    with pytest.raises(RuntimeError, match="pinned"):
        remote.check_request("example.com", "/model/m/converse-stream", expected_host=host)
    with pytest.raises(RuntimeError, match="pinned"):
        remote.check_request(host, "/model/m/converse", expected_host=host)
    with pytest.raises(ValueError, match="region"):
        remote.bedrock_host("not a region")


def test_stream_accounting_records_delivery_after_five_minutes() -> None:
    times = iter([10.0, 301.0, 302.0])
    chunks = [
        {"choices": [{"delta": {"content": "a"}}]},
        {"choices": [{"delta": {"reasoning_content": "bb"}}]},
        {"choices": [{"delta": {"content": "c"}, "finish_reason": "stop"}], "usage": {"total_tokens": 3}},
    ]
    result = remote.consume_stream(chunks, started=0.0, observe_seconds=660, clock=lambda: next(times))
    assert result["status"] == "completed"
    assert result["delivered_after_300_seconds"] is True
    assert (result["chunks"], result["text_characters"], result["reasoning_characters"]) == (3, 2, 2)
    assert result["finish_reason"] == "stop"


def test_stream_stops_at_the_observation_window() -> None:
    times = iter([1.0, 700.0, 701.0])
    chunks = [{"choices": [{"delta": {"content": "a"}}]}] * 3
    result = remote.consume_stream(chunks, started=0.0, observe_seconds=660, clock=lambda: next(times))
    assert result["status"] == "stopped_after_observation_window"
    assert result["chunks"] == 2


def test_key_travels_only_on_stdin_and_never_reaches_output(capsys: pytest.CaptureFixture[str]) -> None:
    process = FakeProcess(
        stdout_lines=[b'{"event": "call_result", "status": "completed"}\n', b"not json " + KEY.encode() + b"\n"],
        stderr_text=b"Traceback with " + KEY.encode(),
    )
    starter = Starter(process)
    assert canary.main([*ARGS, "--execute"], prompt_secret=lambda prompt: KEY, start=starter) == 0

    assert all(KEY not in part for part in starter.argv)
    sent = json.loads(process.stdin.sent)
    assert sent["key"] == KEY and sent["region"] == "xx-east-1"
    output = capsys.readouterr()
    assert KEY not in output.out + output.err
    assert '"call_result"' in output.out


def test_probe_failure_is_reported_with_stderr_withheld(capsys: pytest.CaptureFixture[str]) -> None:
    process = FakeProcess(stdout_lines=[], stderr_text=KEY.encode(), code=3)
    assert canary.main([*ARGS, "--execute"], prompt_secret=lambda prompt: KEY, start=Starter(process)) == 1
    output = capsys.readouterr().out
    assert '"stderr_withheld": true' in output
    assert KEY not in output


def test_dry_run_asks_for_no_key_and_starts_nothing() -> None:
    asked: list[str] = []
    starter = Starter(FakeProcess(stdout_lines=[]))
    assert canary.main(ARGS, prompt_secret=lambda prompt: asked.append(prompt) or KEY, start=starter) == 0
    assert asked == [] and starter.argv == []


def test_missing_region_is_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELSPETH_OPS_BEDROCK_REGION", raising=False)
    assert run_tool(canary.main, ["--container", "web-a", "--quick-model", "q", "--long-model", "l"]) == 2


def test_remote_program_source_is_what_is_sent() -> None:
    sent = canary.REMOTE_SOURCE
    assert Path(remote.__file__).read_text(encoding="utf-8") == sent
    compile(canary.REMOTE_SOURCE, "<remote>", "exec")


def test_help_needs_no_environment() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "scripts.staging.direct_bedrock_canary", "--help"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(canary.__file__).resolve().parents[2])},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--execute" in result.stdout
