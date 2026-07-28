"""Ordered, replay-safe AWS ECS acceptance gate ledger."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from .contracts import (
    _GIT_SHA_PATTERN,
    _SHA256_PATTERN,
    AcceptanceCheckError,
    _control_timestamp,
    _sha256,
    _utc_timestamp,
)
from .secure_documents import (
    _read_protected_document,
    _receipt_manifest_write_lock,
    _write_protected_document,
)

_GATE_LEDGER_FIELDS = frozenset(
    {
        "schema",
        "branch",
        "starting_sha",
        "plan_sha256",
        "program_base_sha",
        "reconciled_release_sha",
        "candidate_sha",
        "candidate_bound_record_count",
        "records",
        "cleanup_records",
        "success_record_count_at_cleanup_start",
        "finalized",
        "created_at",
        "updated_at",
    }
)
_GATE_RECORD_FIELDS = frozenset({"check_id", "candidate_sha", "started_at", "ended_at", "exit_status", "receipt_hash"})
_SUCCESS_GATE_CHECK_ORDER = ("candidate", "static", "tests", "image", "live")
_CLEANUP_GATE_CHECK_ORDER = ("cleanup",)
_REQUIRED_GATE_CHECK_ORDER = (*_SUCCESS_GATE_CHECK_ORDER, *_CLEANUP_GATE_CHECK_ORDER)
_REQUIRED_GATE_CHECK_IDS = frozenset(_REQUIRED_GATE_CHECK_ORDER)
_TASK1_GATE_CHECK_ORDER = ("candidate",)
_GATE_LEDGER_GET_FIELDS = frozenset(
    {"branch", "starting_sha", "plan_sha256", "program_base_sha", "reconciled_release_sha", "candidate_sha"}
)
_TERMINAL_GATE_CHECK_ID = "cleanup"


def _gate_records_hash(records: object) -> str:
    return _sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _gate_ledger_records_hash(ledger: Mapping[str, object]) -> str:
    return _gate_records_hash(
        {
            "records": ledger["records"],
            "cleanup_records": ledger["cleanup_records"],
        }
    )


def _gate_ledger_cleanup_prefix_hash(ledger: Mapping[str, object]) -> str:
    cleanup_records = cast(list[dict[str, object]], ledger["cleanup_records"])
    prefix_records = [record for record in cleanup_records if record["check_id"] != _TERMINAL_GATE_CHECK_ID]
    return _gate_ledger_records_hash({**ledger, "cleanup_records": prefix_records})


def _validate_gate_record_stream(records: object, order: tuple[str, ...]) -> list[dict[str, object]]:
    if not isinstance(records, list) or len(records) > len(order):
        raise AcceptanceCheckError("gate_ledger_schema")
    seen: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict) or set(record) != _GATE_RECORD_FIELDS:
            raise AcceptanceCheckError("gate_ledger_schema")
        check_id = record["check_id"]
        if type(check_id) is not str or check_id != order[index] or check_id in seen:
            raise AcceptanceCheckError("gate_ledger_schema")
        seen.add(check_id)
        candidate_sha = record["candidate_sha"]
        if type(candidate_sha) is not str or _GIT_SHA_PATTERN.fullmatch(candidate_sha) is None:
            raise AcceptanceCheckError("gate_ledger_schema")
        started = _control_timestamp(record["started_at"])
        ended = _control_timestamp(record["ended_at"])
        if ended < started:
            raise AcceptanceCheckError("gate_ledger_schema")
        exit_status = record["exit_status"]
        receipt_hash = record["receipt_hash"]
        if type(exit_status) is not int or not 0 <= exit_status <= 255:
            raise AcceptanceCheckError("gate_ledger_schema")
        if type(receipt_hash) is not str or _SHA256_PATTERN.fullmatch(receipt_hash) is None:
            raise AcceptanceCheckError("gate_ledger_schema")
    return cast(list[dict[str, object]], records)


def _validate_gate_ledger(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != _GATE_LEDGER_FIELDS:
        raise AcceptanceCheckError("gate_ledger_schema")
    if payload["schema"] != "elspeth.aws-ecs-gate-ledger.v4":
        raise AcceptanceCheckError("gate_ledger_schema")
    branch = payload["branch"]
    starting_sha = payload["starting_sha"]
    if type(branch) is not str or re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", branch) is None or branch.startswith("/"):
        raise AcceptanceCheckError("gate_ledger_schema")
    if type(starting_sha) is not str or _GIT_SHA_PATTERN.fullmatch(starting_sha) is None:
        raise AcceptanceCheckError("gate_ledger_schema")
    for field, pattern in (
        ("plan_sha256", _SHA256_PATTERN),
        ("program_base_sha", _GIT_SHA_PATTERN),
        ("reconciled_release_sha", _GIT_SHA_PATTERN),
    ):
        value = payload[field]
        if type(value) is not str or pattern.fullmatch(value) is None:
            raise AcceptanceCheckError("gate_ledger_schema")
    records = _validate_gate_record_stream(payload["records"], _SUCCESS_GATE_CHECK_ORDER)
    cleanup_records = _validate_gate_record_stream(payload["cleanup_records"], _CLEANUP_GATE_CHECK_ORDER)
    cleanup_start_count = payload["success_record_count_at_cleanup_start"]
    if cleanup_records:
        if type(cleanup_start_count) is not int or cleanup_start_count != len(records):
            raise AcceptanceCheckError("gate_ledger_schema")
    elif cleanup_start_count is not None:
        raise AcceptanceCheckError("gate_ledger_schema")
    bound_candidate = payload["candidate_sha"]
    bound_record_count = payload["candidate_bound_record_count"]
    if (bound_candidate is None) != (bound_record_count is None):
        raise AcceptanceCheckError("gate_ledger_schema")
    if bound_candidate is None and (len(records) > len(_TASK1_GATE_CHECK_ORDER) or cleanup_records):
        raise AcceptanceCheckError("gate_ledger_schema")
    if bound_candidate is not None and (
        type(bound_candidate) is not str
        or _GIT_SHA_PATTERN.fullmatch(bound_candidate) is None
        or type(bound_record_count) is not int
        or bound_record_count != len(_TASK1_GATE_CHECK_ORDER)
        or bound_record_count > len(records)
        or any(record["candidate_sha"] != bound_candidate for record in records[:bound_record_count])
        or any(record["candidate_sha"] != bound_candidate for record in records[bound_record_count:])
        or any(record["candidate_sha"] != bound_candidate for record in cleanup_records)
    ):
        raise AcceptanceCheckError("gate_ledger_schema")
    finalized = payload["finalized"]
    if finalized is not None:
        if not isinstance(finalized, dict) or set(finalized) != {
            "candidate_sha",
            "record_count",
            "records_sha256",
            "finalized_at",
        }:
            raise AcceptanceCheckError("gate_ledger_schema")
        if (
            type(finalized["candidate_sha"]) is not str
            or _GIT_SHA_PATTERN.fullmatch(finalized["candidate_sha"]) is None
            or finalized["candidate_sha"] != bound_candidate
            or finalized["record_count"] != len(records) + len(cleanup_records)
            or finalized["records_sha256"] != _gate_ledger_records_hash(payload)
        ):
            raise AcceptanceCheckError("gate_ledger_schema")
        _control_timestamp(finalized["finalized_at"])
    created = _control_timestamp(payload["created_at"])
    updated = _control_timestamp(payload["updated_at"])
    if updated < created:
        raise AcceptanceCheckError("gate_ledger_schema")
    return payload


def _read_gate_ledger(path: Path) -> dict[str, object]:
    return _validate_gate_ledger(_read_protected_document(path, check="gate_ledger_file"))


def gate_ledger_init(
    path: Path,
    *,
    branch: str,
    starting_sha: str,
    plan_sha256: str,
    program_base_sha: str,
    reconciled_release_sha: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Create a ledger, or accept an exact-owner unfinalized resume."""

    try:
        existing = _read_gate_ledger(path)
    except AcceptanceCheckError as exc:
        if exc.check != "gate_ledger_file" or path.exists():
            raise
    else:
        if (
            existing["branch"] == branch
            and existing["starting_sha"] == starting_sha
            and existing["plan_sha256"] == plan_sha256
            and existing["program_base_sha"] == program_base_sha
            and existing["reconciled_release_sha"] == reconciled_release_sha
            and existing["finalized"] is None
            and not existing["cleanup_records"]
        ):
            return existing
        raise AcceptanceCheckError("gate_ledger_conflict")
    timestamp = _utc_timestamp(now())
    ledger: dict[str, object] = {
        "schema": "elspeth.aws-ecs-gate-ledger.v4",
        "branch": branch,
        "starting_sha": starting_sha,
        "plan_sha256": plan_sha256,
        "program_base_sha": program_base_sha,
        "reconciled_release_sha": reconciled_release_sha,
        "candidate_sha": None,
        "candidate_bound_record_count": None,
        "records": [],
        "cleanup_records": [],
        "success_record_count_at_cleanup_start": None,
        "finalized": None,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    _validate_gate_ledger(ledger)
    _write_protected_document(
        path,
        ledger,
        create=True,
        exists_check="gate_ledger_exists",
        write_check="gate_ledger_file",
        parent_check="gate_ledger_parent",
    )
    return ledger


def gate_ledger_get(path: Path, field: str) -> str:
    """Return one closed, non-secret ledger anchor for resume-safe shell use."""

    if field not in _GATE_LEDGER_GET_FIELDS:
        raise AcceptanceCheckError("gate_ledger_get")
    value = _read_gate_ledger(path)[field]
    if type(value) is not str:
        raise AcceptanceCheckError("gate_ledger_get")
    return value


def gate_ledger_bind_candidate(
    path: Path,
    *,
    candidate_sha: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Freeze the candidate SHA at the exact ledger boundary where it exists."""

    with _receipt_manifest_write_lock(path, check="gate_ledger_file"):
        return _gate_ledger_bind_candidate_locked(path, candidate_sha=candidate_sha, now=now)


def _gate_ledger_bind_candidate_locked(
    path: Path,
    *,
    candidate_sha: str,
    now: Callable[[], datetime],
) -> dict[str, object]:
    ledger = _read_gate_ledger(path)
    if ledger["finalized"] is not None:
        raise AcceptanceCheckError("gate_ledger_finalized")
    existing = ledger["candidate_sha"]
    if existing is not None:
        if existing == candidate_sha:
            return ledger
        raise AcceptanceCheckError("gate_ledger_conflict")
    if _GIT_SHA_PATTERN.fullmatch(candidate_sha) is None:
        raise AcceptanceCheckError("gate_ledger_schema")
    records = cast(list[dict[str, object]], ledger["records"])
    if [record["check_id"] for record in records] != list(_TASK1_GATE_CHECK_ORDER):
        raise AcceptanceCheckError("gate_ledger_incomplete")
    if any(record["exit_status"] != 0 for record in records):
        raise AcceptanceCheckError("gate_ledger_failed")
    if any(record["candidate_sha"] != candidate_sha for record in records):
        raise AcceptanceCheckError("gate_ledger_candidate")
    timestamp = _utc_timestamp(now())
    ledger["candidate_sha"] = candidate_sha
    ledger["candidate_bound_record_count"] = len(records)
    ledger["updated_at"] = timestamp
    _validate_gate_ledger(ledger)
    _write_protected_document(
        path,
        ledger,
        create=False,
        exists_check="gate_ledger_exists",
        write_check="gate_ledger_file",
        parent_check="gate_ledger_parent",
    )
    return ledger


def _gate_ledger_record_stream(
    path: Path,
    *,
    stream: Literal["records", "cleanup_records"],
    order: tuple[str, ...],
    check_id: str,
    exit_status: int,
    receipt_hash: str,
    candidate_sha: str,
    started_at: str | None = None,
    ended_at: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Append one record while the public caller holds the ledger lock."""

    ledger = _read_gate_ledger(path)
    if ledger["finalized"] is not None:
        raise AcceptanceCheckError("gate_ledger_finalized")
    records = cast(list[dict[str, object]], ledger[stream])
    bound_candidate = ledger["candidate_sha"]
    if bound_candidate is not None and candidate_sha != bound_candidate:
        raise AcceptanceCheckError("gate_ledger_candidate")
    existing = next(
        (item for item in records if item["check_id"] == check_id),
        None,
    )
    if existing is not None:
        same_evidence = (
            existing["candidate_sha"] == candidate_sha
            and existing["exit_status"] == exit_status
            and existing["receipt_hash"] == receipt_hash
            and (started_at is None or existing["started_at"] == started_at)
            and (ended_at is None or existing["ended_at"] == ended_at)
        )
        if same_evidence:
            return ledger
        raise AcceptanceCheckError("gate_ledger_conflict")
    if stream == "records":
        cleanup_records = cast(list[dict[str, object]], ledger["cleanup_records"])
        if cleanup_records or (bound_candidate is None and check_id not in _TASK1_GATE_CHECK_ORDER):
            raise AcceptanceCheckError("gate_ledger_phase")
    expected_index = len(records)
    if expected_index >= len(order) or check_id != order[expected_index]:
        raise AcceptanceCheckError("gate_ledger_schema")
    timestamp = _utc_timestamp(now())
    record: dict[str, object] = {
        "check_id": check_id,
        "candidate_sha": candidate_sha,
        "started_at": started_at or timestamp,
        "ended_at": ended_at or timestamp,
        "exit_status": exit_status,
        "receipt_hash": receipt_hash,
    }
    candidate = {**ledger, stream: [*records, record], "updated_at": timestamp}
    if stream == "cleanup_records" and not records:
        success_records = cast(list[dict[str, object]], ledger["records"])
        candidate["success_record_count_at_cleanup_start"] = len(success_records)
    _validate_gate_ledger(candidate)
    _write_protected_document(
        path,
        candidate,
        create=False,
        exists_check="gate_ledger_exists",
        write_check="gate_ledger_file",
        parent_check="gate_ledger_parent",
    )
    return candidate


def gate_ledger_record(
    path: Path,
    *,
    check_id: str,
    exit_status: int,
    receipt_hash: str,
    candidate_sha: str,
    started_at: str | None = None,
    ended_at: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    with _receipt_manifest_write_lock(path, check="gate_ledger_file"):
        return _gate_ledger_record_stream(
            path,
            stream="records",
            order=_SUCCESS_GATE_CHECK_ORDER,
            check_id=check_id,
            exit_status=exit_status,
            receipt_hash=receipt_hash,
            candidate_sha=candidate_sha,
            started_at=started_at,
            ended_at=ended_at,
            now=now,
        )


def gate_ledger_record_cleanup(
    path: Path,
    *,
    check_id: str,
    exit_status: int,
    receipt_hash: str,
    candidate_sha: str,
    started_at: str | None = None,
    ended_at: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Record ordered cleanup evidence independently of incomplete success checks."""

    with _receipt_manifest_write_lock(path, check="gate_ledger_file"):
        ledger = _read_gate_ledger(path)
        if ledger["candidate_sha"] is None:
            raise AcceptanceCheckError("gate_ledger_candidate")
        return _gate_ledger_record_stream(
            path,
            stream="cleanup_records",
            order=_CLEANUP_GATE_CHECK_ORDER,
            check_id=check_id,
            exit_status=exit_status,
            receipt_hash=receipt_hash,
            candidate_sha=candidate_sha,
            started_at=started_at,
            ended_at=ended_at,
            now=now,
        )


def _gate_ledger_record_cleanup_bound(
    path: Path,
    *,
    check_id: str,
    exit_status: int,
    receipt_hash: str,
    candidate_sha: str,
    expected_prefix_records_sha256: str,
    started_at: str | None = None,
    ended_at: str | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Append cleanup evidence only while its prepared ledger prefix still matches."""

    with _receipt_manifest_write_lock(path, check="gate_ledger_file"):
        ledger = _read_gate_ledger(path)
        if ledger["candidate_sha"] is None:
            raise AcceptanceCheckError("gate_ledger_candidate")
        if (
            _SHA256_PATTERN.fullmatch(expected_prefix_records_sha256) is None
            or _gate_ledger_cleanup_prefix_hash(ledger) != expected_prefix_records_sha256
        ):
            raise AcceptanceCheckError("gate_ledger_conflict")
        cleanup_records = cast(list[dict[str, object]], ledger["cleanup_records"])
        existing = next(
            (record for record in cleanup_records if record["check_id"] == check_id),
            None,
        )
        if existing is not None:
            if (
                existing["candidate_sha"] == candidate_sha
                and existing["exit_status"] == exit_status
                and existing["receipt_hash"] == receipt_hash
            ):
                return ledger
            raise AcceptanceCheckError("gate_ledger_conflict")
        return _gate_ledger_record_stream(
            path,
            stream="cleanup_records",
            order=_CLEANUP_GATE_CHECK_ORDER,
            check_id=check_id,
            exit_status=exit_status,
            receipt_hash=receipt_hash,
            candidate_sha=candidate_sha,
            started_at=started_at,
            ended_at=ended_at,
            now=now,
        )


def gate_ledger_finalize(
    path: Path,
    *,
    candidate_sha: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    with _receipt_manifest_write_lock(path, check="gate_ledger_file"):
        return _gate_ledger_finalize_locked(path, candidate_sha=candidate_sha, now=now)


def _gate_ledger_finalize_locked(
    path: Path,
    *,
    candidate_sha: str,
    now: Callable[[], datetime],
) -> dict[str, object]:
    ledger = _read_gate_ledger(path)
    if ledger["finalized"] is not None:
        finalized = cast(dict[str, object], ledger["finalized"])
        if finalized["candidate_sha"] == candidate_sha:
            return ledger
        raise AcceptanceCheckError("gate_ledger_conflict")
    records = cast(list[dict[str, object]], ledger["records"])
    cleanup_records = cast(list[dict[str, object]], ledger["cleanup_records"])
    if not records:
        raise AcceptanceCheckError("gate_ledger_empty")
    if [record["check_id"] for record in records] != list(_SUCCESS_GATE_CHECK_ORDER):
        raise AcceptanceCheckError("gate_ledger_incomplete")
    if [record["check_id"] for record in cleanup_records] != list(_CLEANUP_GATE_CHECK_ORDER):
        raise AcceptanceCheckError("gate_ledger_incomplete")
    if ledger["candidate_sha"] != candidate_sha:
        raise AcceptanceCheckError("gate_ledger_candidate")
    bound_record_count = ledger["candidate_bound_record_count"]
    if (
        type(bound_record_count) is not int
        or any(record["candidate_sha"] != candidate_sha for record in records[bound_record_count:])
        or any(record["candidate_sha"] != candidate_sha for record in cleanup_records)
    ):
        raise AcceptanceCheckError("gate_ledger_candidate")
    if any(record["exit_status"] != 0 for record in [*records, *cleanup_records]):
        raise AcceptanceCheckError("gate_ledger_failed")
    timestamp = _utc_timestamp(now())
    ledger["finalized"] = {
        "candidate_sha": candidate_sha,
        "record_count": len(records) + len(cleanup_records),
        "records_sha256": _gate_ledger_records_hash(ledger),
        "finalized_at": timestamp,
    }
    ledger["updated_at"] = timestamp
    _validate_gate_ledger(ledger)
    _write_protected_document(
        path,
        ledger,
        create=False,
        exists_check="gate_ledger_exists",
        write_check="gate_ledger_file",
        parent_check="gate_ledger_parent",
    )
    return ledger
