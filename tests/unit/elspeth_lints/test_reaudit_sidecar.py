"""Round-trip tests for reaudit sidecar entry serialization.

Covers ``_entry_to_dict`` / ``_entry_from_dict`` — the AllowlistEntry
serialization boundary used to persist reaudit sidecars. v1 entries bind via
``file_fingerprint``; v2 entries bind via ``scope_fingerprint`` and carry a
``judge_signature_version``. Both must survive the round trip intact.

Also covers the strict-JSON decoding boundary: the sidecar is Tier-1 audit
evidence, so the loader must reject the non-JSON numeric constants CPython's
``json`` accepts by default rather than coerce them into ``float('nan')``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from elspeth_lints.core.allowlist import AllowlistEntry, JudgeVerdict
from elspeth_lints.core.reaudit_sidecar import (
    SidecarCorruptError,
    SidecarHeader,
    SidecarWriter,
    _entry_from_dict,
    _entry_to_dict,
    _header_to_dict,
    load_sidecar,
)


def _roundtrip(entry: AllowlistEntry) -> AllowlistEntry:
    payload = _entry_to_dict(entry)
    return _entry_from_dict(payload, sidecar_path=Path("test.sidecar"), line_no=1)


def test_entry_dict_roundtrip_preserves_v2_scope_binding() -> None:
    """A v2 entry round-trips with scope_fingerprint and judge_signature_version."""
    scope_fp = "a" * 64
    entry = AllowlistEntry(
        key="web/x.py:R1:fn:fp=aa",
        owner="alice",
        reason="permitted boundary",
        safety="contained",
        expires=None,
        file_fingerprint=None,
        scope_fingerprint=scope_fp,
        judge_signature_version=2,
        ast_path="Module.body[0]",
        pattern=None,
        source_file="test.yaml",
        judge_verdict=JudgeVerdict.ACCEPTED,
        judge_recorded_at=datetime(2026, 5, 23, tzinfo=UTC),
        judge_model="some-model",
        judge_rationale="rationale",
        judge_confidence=None,
        judge_model_verdict=JudgeVerdict.ACCEPTED,
        judge_policy_hash="policyhash",
        judge_metadata_signature="hmac-sha256:v2:" + "0" * 64,
    )

    payload = _entry_to_dict(entry)
    assert payload["scope_fingerprint"] == scope_fp
    assert payload["judge_signature_version"] == 2
    assert payload["file_fingerprint"] is None

    restored = _entry_from_dict(payload, sidecar_path=Path("test.sidecar"), line_no=1)
    assert restored.scope_fingerprint == scope_fp
    assert restored.judge_signature_version == 2
    assert restored.file_fingerprint is None


def test_entry_dict_roundtrip_preserves_v1_file_binding() -> None:
    """A v1 entry (file_fingerprint, no scope_fingerprint/version) round-trips intact."""
    file_fp = "b" * 64
    entry = AllowlistEntry(
        key="web/x.py:R1:fn:fp=bb",
        owner="alice",
        reason="permitted boundary",
        safety="contained",
        expires=None,
        file_fingerprint=file_fp,
        scope_fingerprint=None,
        judge_signature_version=None,
        ast_path="Module.body[0]",
        pattern=None,
        source_file="test.yaml",
        judge_verdict=JudgeVerdict.ACCEPTED,
        judge_recorded_at=datetime(2026, 5, 23, tzinfo=UTC),
        judge_model="some-model",
        judge_rationale="rationale",
        judge_confidence=None,
        judge_model_verdict=JudgeVerdict.ACCEPTED,
        judge_policy_hash="policyhash",
        judge_metadata_signature="hmac-sha256:v1:" + "0" * 64,
    )

    restored = _roundtrip(entry)
    assert restored.file_fingerprint == file_fp
    assert restored.scope_fingerprint is None
    assert restored.judge_signature_version is None


def test_sidecar_round_trips_judge_transport() -> None:
    """The additive judge_transport field survives the sidecar round trip."""
    entry = AllowlistEntry(
        key="web/x.py:R1:fn:fp=aa",
        owner="alice",
        reason="permitted boundary",
        safety="contained",
        expires=None,
        file_fingerprint=None,
        scope_fingerprint="a" * 64,
        judge_signature_version=2,
        judge_transport="claude_agent_sdk",
        ast_path="Module.body[0]",
        pattern=None,
        source_file="test.yaml",
        judge_verdict=JudgeVerdict.ACCEPTED,
        judge_recorded_at=datetime(2026, 5, 23, tzinfo=UTC),
        judge_model="some-model",
        judge_rationale="rationale",
        judge_confidence=None,
        judge_model_verdict=JudgeVerdict.ACCEPTED,
        judge_policy_hash="policyhash",
        judge_metadata_signature="hmac-sha256:v2:" + "0" * 64,
    )

    restored = _roundtrip(entry)
    assert restored.judge_transport == "claude_agent_sdk"


def _write_sidecar(path: Path, *lines: str) -> None:
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def _valid_header() -> SidecarHeader:
    return SidecarHeader(
        run_id="a" * 32,
        started_at=datetime(2026, 7, 31, tzinfo=UTC),
        total_entries=1,
        allowlist_path="allowlist",
        allowlist_hash="c" * 64,
        judge_transport="agent",
        rule_filter="trust_tier.tier_model",
        since_iso=None,
        limit=None,
        include_pre_judge=False,
    )


def _valid_header_line() -> str:
    return json.dumps(_header_to_dict(_valid_header()), sort_keys=True)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_load_sidecar_rejects_non_json_numeric_constants(tmp_path: Path, constant: str) -> None:
    """A durable sidecar line carrying NaN/Infinity is corruption, not data.

    The line is newline-terminated, so the T6c truncated-final-line
    recovery does not apply: the loader must crash rather than admit a
    non-finite judge_confidence into the audit reconstruction.
    """
    sidecar = tmp_path / "sweep.jsonl"
    _write_sidecar(
        sidecar,
        _valid_header_line(),
        '{"type": "outcome", "judge_confidence": ' + constant + "}",
    )

    with pytest.raises(SidecarCorruptError) as exc_info:
        load_sidecar(sidecar)
    assert "line 2" in str(exc_info.value)
    assert "non-JSON numeric constant" in str(exc_info.value)


def test_load_sidecar_rejects_duplicate_object_keys(tmp_path: Path) -> None:
    """Duplicate keys are ambiguous evidence; last-wins coercion is not allowed."""
    sidecar = tmp_path / "sweep.jsonl"
    _write_sidecar(
        sidecar,
        _valid_header_line(),
        '{"type": "outcome", "key": "a", "key": "b"}',
    )

    with pytest.raises(SidecarCorruptError) as exc_info:
        load_sidecar(sidecar)
    assert "duplicate JSON object key" in str(exc_info.value)


@pytest.mark.parametrize(
    ("corrupt_tail", "expected_error"),
    [
        ('{"type": "outcome", "key": "a", "key": "b"}', "duplicate JSON object key"),
        ('{"type": "outcome", "judge_confidence": NaN}', "non-JSON numeric constant"),
        ('{"type": "outcome", "judge_confidence": Infinity}', "non-JSON numeric constant"),
        ('{"type": "outcome", "judge_confidence": -Infinity}', "non-JSON numeric constant"),
    ],
)
def test_load_sidecar_rejects_complete_unterminated_strict_json_corruption(
    tmp_path: Path,
    corrupt_tail: str,
    expected_error: str,
) -> None:
    """A complete strict-JSON violation is corruption even without a final newline."""
    sidecar = tmp_path / "sweep.jsonl"
    original = (_valid_header_line() + "\n" + corrupt_tail).encode()
    sidecar.write_bytes(original)

    with pytest.raises(SidecarCorruptError) as exc_info:
        load_sidecar(sidecar)

    assert "line 2" in str(exc_info.value)
    assert expected_error in str(exc_info.value)
    assert sidecar.read_bytes() == original


@pytest.mark.parametrize(
    ("corrupt_tail", "expected_error"),
    [
        ('{"type": "outcome", "key": "a", "key": "b"}', "duplicate JSON object key"),
        ('{"type": "outcome", "judge_confidence": NaN}', "non-JSON numeric constant"),
        ('{"type": "outcome", "judge_confidence": Infinity}', "non-JSON numeric constant"),
        ('{"type": "outcome", "judge_confidence": -Infinity}', "non-JSON numeric constant"),
    ],
)
def test_sidecar_writer_refuses_complete_unterminated_strict_json_corruption_without_mutation(
    tmp_path: Path,
    corrupt_tail: str,
    expected_error: str,
) -> None:
    """Append repair must not truncate a complete strict-JSON violation."""
    sidecar = tmp_path / ".reaudit-state" / "sweep.jsonl"
    sidecar.parent.mkdir()
    original = (_valid_header_line() + "\n" + corrupt_tail).encode()
    sidecar.write_bytes(original)

    with pytest.raises(SidecarCorruptError) as exc_info, SidecarWriter(sidecar, _valid_header(), append=True):
        pass

    assert expected_error in str(exc_info.value)
    assert sidecar.read_bytes() == original


def test_load_sidecar_recovers_final_tail_split_inside_utf8_codepoint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A killed write may leave only the first byte of a multibyte character."""
    sidecar = tmp_path / "sweep.jsonl"
    header = _valid_header_line().encode() + b"\n"
    partial_tail = b'{"type": "outcome", "fresh_rationale": "\xe2'
    sidecar.write_bytes(header + partial_tail)

    loaded = load_sidecar(sidecar)

    assert loaded.header.run_id == _valid_header().run_id
    assert loaded.outcomes == ()
    assert loaded.trailer is None
    warning = capsys.readouterr().err
    assert "partial final line" in warning
    assert f"byte offset {len(header)}" in warning
    assert "UTF-8 decode failed" in warning


def test_sidecar_writer_resume_repairs_final_tail_split_inside_utf8_codepoint(tmp_path: Path) -> None:
    """Resume validates under lock, then truncates the split UTF-8 tail."""
    sidecar = tmp_path / ".reaudit-state" / "sweep.jsonl"
    sidecar.parent.mkdir()
    header = _valid_header_line().encode() + b"\n"
    partial_tail = b'{"type": "outcome", "fresh_rationale": "\xe2'
    sidecar.write_bytes(header + partial_tail)
    loaded_under_lock = []

    with SidecarWriter(
        sidecar,
        _valid_header(),
        append=True,
        on_resume_locked=loaded_under_lock.append,
    ):
        pass

    assert len(loaded_under_lock) == 1
    assert loaded_under_lock[0].header.run_id == _valid_header().run_id
    assert sidecar.read_bytes() == header
    assert load_sidecar(sidecar).outcomes == ()


_COMPLETE_INVALID_UTF8_TAILS = [
    pytest.param(b"\xff", "invalid start byte", id="invalid-start-byte"),
    pytest.param(b"\xe2X", "invalid continuation byte", id="invalid-continuation-byte"),
]


@pytest.mark.parametrize(("corrupt_sequence", "expected_reason"), _COMPLETE_INVALID_UTF8_TAILS)
def test_load_sidecar_rejects_complete_unterminated_invalid_utf8_without_mutation(
    tmp_path: Path,
    corrupt_sequence: bytes,
    expected_reason: str,
) -> None:
    """Complete invalid UTF-8 is corruption, not an interrupted codepoint."""
    sidecar = tmp_path / "sweep.jsonl"
    original = _valid_header_line().encode() + b'\n{"type": "outcome", "fresh_rationale": "' + corrupt_sequence + b'"}'
    sidecar.write_bytes(original)

    with pytest.raises(SidecarCorruptError, match=expected_reason):
        load_sidecar(sidecar)

    assert sidecar.read_bytes() == original


@pytest.mark.parametrize(("corrupt_sequence", "expected_reason"), _COMPLETE_INVALID_UTF8_TAILS)
def test_sidecar_writer_refuses_complete_unterminated_invalid_utf8_without_mutation(
    tmp_path: Path,
    corrupt_sequence: bytes,
    expected_reason: str,
) -> None:
    """Append repair must not truncate complete invalid UTF-8."""
    sidecar = tmp_path / ".reaudit-state" / "sweep.jsonl"
    sidecar.parent.mkdir()
    original = _valid_header_line().encode() + b'\n{"type": "outcome", "fresh_rationale": "' + corrupt_sequence + b'"}'
    sidecar.write_bytes(original)

    with pytest.raises(SidecarCorruptError, match=expected_reason), SidecarWriter(sidecar, _valid_header(), append=True):
        pass

    assert sidecar.read_bytes() == original


@pytest.mark.parametrize(
    "suffix",
    [
        b'{"type": "outcome", "fresh_rationale": "\xe2\n',
        b'{"type": "outcome", "fresh_rationale": "\xe2\n{}\n',
    ],
    ids=["newline-terminated-final-line", "non-final-line"],
)
def test_load_sidecar_rejects_invalid_utf8_outside_recoverable_final_tail(
    tmp_path: Path,
    suffix: bytes,
) -> None:
    """Invalid UTF-8 is recoverable only in the final unterminated line."""
    sidecar = tmp_path / "sweep.jsonl"
    sidecar.write_bytes(_valid_header_line().encode() + b"\n" + suffix)

    with pytest.raises(SidecarCorruptError, match="invalid UTF-8"):
        load_sidecar(sidecar)
