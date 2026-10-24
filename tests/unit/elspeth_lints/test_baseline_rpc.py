"""Bounded maintenance data never supplies a signed-gate receipt."""

from __future__ import annotations

import base64
import hashlib
import os
from copy import deepcopy
from pathlib import Path

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


def _up_link_census(escape_target: bytes) -> list[dict[str, str]]:
    # p/q/d points at the SHALLOWER directory a, so ".." taken after following
    # it climbs from a, not from p/q.
    return records(
        [
            record("a/keep.txt", b"k"),
            record("p/q/d", b"../../a", "120000"),
            record("p/q/e", escape_target, "120000"),
            record("p/x", b"lexical"),
            record("x", b"inside"),
        ]
    )


def test_snapshot_refuses_up_link_that_climbs_through_a_shallower_symlink() -> None:
    # Lexically "p/q/d/../../x" is the in-census p/x; the filesystem follows d
    # to a first, so the link resolves to <root>/../x.
    with pytest.raises(JudgeContractError, match="escapes"):
        rpc.validate_files(_up_link_census(b"d/../../x"))


def test_snapshot_refuses_plain_up_link_out_of_the_root() -> None:
    with pytest.raises(JudgeContractError, match="escapes"):
        rpc.validate_files(_up_link_census(b"../../../x"))


@pytest.mark.parametrize("target", [b"d/keep.txt", b"d/../x", b"../x"])
def test_snapshot_accepts_up_links_that_resolve_inside_the_root(target: bytes) -> None:
    # d/../x is root/a/../x = root/x on disk (not the lexical p/q/x).
    files = _up_link_census(target)
    assert rpc.validate_files(files) == files


@pytest.mark.parametrize(
    "links",
    [
        [record("a", b"b/..", "120000"), record("b", b"a/..", "120000")],
        [record("a", b"b/c", "120000"), record("b", b"a/c", "120000")],
    ],
)
def test_snapshot_refuses_symlink_cycles_through_parent_links(links: list[dict[str, str]]) -> None:
    with pytest.raises(JudgeContractError):
        rpc.validate_files(records(links))


def test_refused_up_link_census_really_escapes_when_materialised(tmp_path: Path) -> None:
    # Negative control for the refusal above: on a real filesystem the same
    # layout reads the file one level above the snapshot root.
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    (root / "p" / "q").mkdir(parents=True)
    (root / "p" / "x").write_text("lexical", encoding="utf-8")
    (tmp_path / "x").write_text("OUTSIDE-ROOT", encoding="utf-8")
    os.symlink("../../a", root / "p" / "q" / "d")
    os.symlink("d/../../x", root / "p" / "q" / "e")
    assert (root / "p" / "q" / "e").read_text(encoding="utf-8") == "OUTSIDE-ROOT"
    with pytest.raises(JudgeContractError, match="escapes"):
        rpc.validate_files(_up_link_census(b"d/../../x"))


def test_snapshot_refuses_a_link_that_steps_through_a_regular_file(tmp_path: Path) -> None:
    # The kernel fails "x/../y" with ENOTDIR when x is a file; lexically it is y.
    files = records([record("x", b"file"), record("y", b"target"), record("e", b"x/../y", "120000")])
    with pytest.raises(JudgeContractError, match="unavailable target"):
        rpc.validate_files(files)
    (tmp_path / "x").write_bytes(b"file")
    (tmp_path / "y").write_bytes(b"target")
    os.symlink("x/../y", tmp_path / "e")
    with pytest.raises(NotADirectoryError):
        (tmp_path / "e").read_bytes()


def _link_chain(length: int) -> list[dict[str, str]]:
    links = [record(f"l{index:02d}", f"l{index + 1:02d}".encode(), "120000") for index in range(length - 1)]
    return records([*links, record(f"l{length - 1:02d}", b"f", "120000"), record("f", b"end")])


def test_snapshot_follow_budget_matches_the_kernel_symlink_limit(tmp_path: Path) -> None:
    # Linux allows 40 follows in one lookup; the 41st is ELOOP.
    assert rpc.validate_files(_link_chain(40)) == _link_chain(40)
    with pytest.raises(JudgeContractError, match="cycle is unsupported"):
        rpc.validate_files(_link_chain(41))
    for length in (40, 41):
        root = tmp_path / str(length)
        root.mkdir()
        (root / "f").write_bytes(b"end")
        for index in range(length):
            os.symlink(f"l{index + 1:02d}" if index < length - 1 else "f", root / f"l{index:02d}")
    assert (tmp_path / "40" / "l00").read_bytes() == b"end"
    with pytest.raises(OSError, match="Too many levels of symbolic links"):
        (tmp_path / "41" / "l00").read_bytes()


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
