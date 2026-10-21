"""Bounded maintenance data never supplies a signed-gate receipt."""

from __future__ import annotations

import base64
import hashlib
from copy import deepcopy

import pytest

from elspeth_lints.core import baseline_rpc as rpc
from elspeth_lints.core.judge import JudgeContractError


def records(extra: list[dict[str, str]] | None = None) -> list[dict[str, str]]:
    result = [record(rpc.BASELINE_PATH, b"{}\n"), record(rpc.CAPTURE_MODULE, b"# candidate capture\n")]
    return sorted(result + ([] if extra is None else extra), key=lambda entry: entry["path"])


def record(path: str, raw: bytes, mode: str = "100644") -> dict[str, str]:
    return {"path": path, "mode": mode, "sha256": hashlib.sha256(raw).hexdigest(), "content": base64.b64encode(raw).decode("ascii")}


def request() -> dict[str, object]:
    files = records()
    return rpc.make_request(files, hashlib.sha256(rpc.canonical_json(files)).hexdigest(), {"demo.rule": []})


def terminal() -> dict[str, object]:
    return {"nodeid": rpc.TEST_NODE, "passed": 1, "failed": 0, "errors": 0, "skipped": 0, "xfailed": 0, "xpassed": 0}


def test_full_snapshot_positive_control_preserves_binary_exec_and_safe_symlink() -> None:
    files = records(
        [
            record("binary.dat", b"\x00\xff"),
            record("bin/tool", b"#!/bin/true\n", "100755"),
            record(".claude/skills/test", b"../../.agents/skills/test", "120000"),
            record(".agents/skills/test/SKILL.md", b"fixture\n"),
        ]
    )
    assert rpc.validate_files(files) == files
    req = rpc.make_request(files, hashlib.sha256(rpc.canonical_json(files)).hexdigest(), {"demo.rule": []})
    rpc.validate_response(rpc.response(req, baseline={"demo.rule": []}, terminal=terminal()), req)


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a/../escape", ".git/config", "src/.venv/payload", "a//b", "a/./b"])
def test_snapshot_refuses_unsafe_and_reserved_paths(path: str) -> None:
    with pytest.raises(JudgeContractError):
        rpc.validate_files(records([record(path, b"data")]))


@pytest.mark.parametrize("target", [b"/etc/private", b"../../private", b"missing", b"loop"])
def test_snapshot_refuses_external_missing_or_cyclic_symlinks(target: bytes) -> None:
    with pytest.raises(JudgeContractError):
        rpc.validate_files(records([record("loop", target, "120000")]))


def test_snapshot_refuses_duplicate_parent_conflict_and_content_mismatch() -> None:
    cases = [records([record("a", b"x"), record("a", b"x")]), records([record("a", b"x"), record("a/b", b"x")])]
    corrupted = records()
    corrupted[0]["sha256"] = "0" * 64
    cases.append(corrupted)
    for files in cases:
        with pytest.raises(JudgeContractError):
            rpc.validate_files(files)


@pytest.mark.parametrize("mode", ["100600", "040000", "160000", True])
def test_snapshot_refuses_unsupported_modes(mode: object) -> None:
    files = records()
    files[0]["mode"] = mode
    with pytest.raises(JudgeContractError):
        rpc.validate_files(files)


@pytest.mark.parametrize("content", ["YQ", "YQ===", "YQ==\n", "!!!!", True])
def test_snapshot_requires_canonical_base64(content: object) -> None:
    files = records()
    files[0]["content"] = content
    with pytest.raises(JudgeContractError):
        rpc.validate_files(files)


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}', rb'{"x":"\ud800"}', b"[]"])
def test_maintenance_json_refuses_ambiguity_and_invalid_unicode(raw: bytes) -> None:
    with pytest.raises(JudgeContractError):
        rpc.decode_json(raw)


def test_response_refuses_lying_capture_and_authority_fields() -> None:
    req = request()
    wrong = rpc.response(req, baseline={"demo.rule": [{"file_path": "a.py", "fingerprint": "wrong"}]}, terminal=terminal())
    with pytest.raises(JudgeContractError, match="independently"):
        rpc.validate_response(wrong, req)
    correct = rpc.response(req, baseline=req["expected_baseline"], terminal=terminal())
    for field in ("signed_gate_passed", "hmac", "source_verified", "executable"):
        reply = {**correct, field: True}
        with pytest.raises(JudgeContractError):
            rpc.validate_response(reply, req)


@pytest.mark.parametrize(
    "field,value",
    [("passed", True), ("passed", 0), ("skipped", 1), ("xfailed", 1), ("xpassed", 1), ("errors", 1), ("nodeid", "another_test")],
)
def test_confirmation_requires_exact_passing_terminal_result(field: str, value: object) -> None:
    req = request()
    result = terminal()
    result[field] = value
    with pytest.raises(JudgeContractError):
        rpc.validate_response(rpc.response(req, baseline=req["expected_baseline"], terminal=result), req)


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_id", "0" * 32),
        ("snapshot_sha256", "0" * 64),
        ("action", "sign"),
        ("version", True),
        ("python_version", [True, 13]),
        ("python_version", [3, 12]),
    ],
)
def test_confirmation_refuses_replay_wrong_action_or_wrong_python(field: str, value: object) -> None:
    req = request()
    reply = rpc.response(req, baseline=req["expected_baseline"], terminal=terminal())
    reply[field] = value
    with pytest.raises(JudgeContractError):
        rpc.validate_response(reply, req)


def test_snapshot_and_request_digest_bind_every_raw_record() -> None:
    req = request()
    mutated = deepcopy(req)
    mutated["files"][0]["content"] = base64.b64encode(b"changed").decode()
    with pytest.raises(JudgeContractError):
        rpc.validate_request(mutated)
