"""Foundation contracts for the AWS ECS acceptance controller."""

from __future__ import annotations

import importlib
import json
import multiprocessing
import os
import re
import threading
from pathlib import Path
from queue import Empty
from types import SimpleNamespace
from typing import Any

import pytest

from elspeth.web import aws_ecs_acceptance as acceptance
from elspeth.web._aws_ecs_acceptance import contracts
from elspeth.web._aws_ecs_acceptance import state as state_module


def _valid_state() -> state_module.AcceptanceState:
    return state_module.AcceptanceState.from_dict(
        {
            "schema_version": 1,
            "session_id": "8e826f53-5f13-420f-8678-5ec0caecd15f",
            "tutorial_session_id": "f6a99a36-13f9-49c9-a3af-d9f6f7924a56",
            "blob_id": "cc742c5f-ae01-49f3-988b-7ecddf0445ef",
            "run_id": "401b6510-a37f-4375-acb8-695fe0098265",
            "landscape_run_id": "a31de342-a9f2-4b31-bb02-9043a047db72",
            # NOT a UUID: real artifact_id values are landscape
            # `artifacts.artifact_id` hex identities (32 or 64 lowercase hex
            # chars), never canonical dashed UUIDs -- see
            # `_ARTIFACT_ID_PATTERN` in contracts.py.
            "artifact_id": "6d9653ae9f51e25579b040ab9ffb7d75e42b731666bbf7500a5c0e3546195d96",
            "uploaded_sha256": "a" * 64,
            "blob_sha256": "a" * 64,
            "artifact_sha256": "b" * 64,
            "run_status": "completed",
            "source_rows": 1,
            "failed_tokens": 0,
            "captured_at": "2026-07-14T04:00:00Z",
            "completed_at": "2026-07-14T04:00:01Z",
        }
    )


def test_http_boundary_constants_are_the_reviewed_budgets() -> None:
    assert contracts.CONNECT_TIMEOUT_SECONDS == 5.0
    assert contracts.READ_TIMEOUT_SECONDS == 15.0
    assert contracts.WRITE_TIMEOUT_SECONDS == 10.0
    assert contracts.POOL_TIMEOUT_SECONDS == 5.0
    assert contracts.MAX_JSON_RESPONSE_BYTES == 1024 * 1024
    assert contracts.MAX_BLOB_RESPONSE_BYTES == 8 * 1024 * 1024
    assert contracts.RUN_POLL_DEADLINE_SECONDS == 5 * 60
    assert contracts.RUN_POLL_INTERVAL_SECONDS == 1.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://staging.example", "https://staging.example"),
        ("https://staging.example:8443", "https://staging.example:8443"),
        ("http://localhost:8451", "http://localhost:8451"),
        ("http://127.0.0.1:8451", "http://127.0.0.1:8451"),
        ("http://[::1]:8451", "http://[::1]:8451"),
    ],
)
def test_acceptance_origin_accepts_https_and_exact_loopback(raw: str, expected: str) -> None:
    assert contracts.normalize_acceptance_origin(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "http://staging.example",
        "http://localhost.example:8451",
        "https://user@staging.example",
        "https://staging.example/",
        "https://staging.example/path",
        "https://staging.example?query=yes",
        "https://staging.example#fragment",
        "https://staging.example:443",
        "HTTPS://staging.example",
        "https://STAGING.example",
        "",
    ],
)
def test_acceptance_origin_rejects_non_normalized_or_ambiguous_values(raw: str) -> None:
    with pytest.raises(contracts.AcceptanceInputError, match="base origin"):
        contracts.normalize_acceptance_origin(raw)


def test_auth_input_accepts_local_or_bearer_modes() -> None:
    local = state_module.AcceptanceCredentials.from_env(
        {
            "ELSPETH_ACCEPTANCE_USERNAME": "operator",
            "ELSPETH_ACCEPTANCE_PASSWORD": "password-secret",
        }
    )
    bearer = state_module.AcceptanceCredentials.from_env({"ELSPETH_ACCEPTANCE_BEARER_TOKEN": "bearer-secret"})

    assert local.mode == "local"
    assert local.username == "operator"
    assert local.password == "password-secret"
    assert local.bearer_token is None
    assert bearer.mode == "bearer"
    assert bearer.username is None
    assert bearer.password is None
    assert bearer.bearer_token == "bearer-secret"


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"ELSPETH_ACCEPTANCE_USERNAME": "operator"},
        {"ELSPETH_ACCEPTANCE_PASSWORD": "password-secret"},
        {
            "ELSPETH_ACCEPTANCE_USERNAME": "operator",
            "ELSPETH_ACCEPTANCE_PASSWORD": "password-secret",
            "ELSPETH_ACCEPTANCE_BEARER_TOKEN": "bearer-secret",
        },
    ],
)
def test_auth_input_rejects_missing_partial_or_mixed_modes_without_echo(env: dict[str, str]) -> None:
    with pytest.raises(contracts.AcceptanceInputError) as raised:
        state_module.AcceptanceCredentials.from_env(env)

    rendered = str(raised.value)
    assert "operator" not in rendered
    assert "password-secret" not in rendered
    assert "bearer-secret" not in rendered


def test_state_file_round_trip_is_mode_0600_and_closed_schema(tmp_path: Path) -> None:
    path = tmp_path / "acceptance-state.json"
    state = _valid_state()

    state_module.write_acceptance_state(path, state)

    assert state_module.read_acceptance_state(path) == state
    assert path.stat().st_mode & 0o777 == 0o600
    assert set(json.loads(path.read_text())) == {
        "schema_version",
        "session_id",
        "tutorial_session_id",
        "blob_id",
        "run_id",
        "landscape_run_id",
        "artifact_id",
        "uploaded_sha256",
        "blob_sha256",
        "artifact_sha256",
        "run_status",
        "source_rows",
        "failed_tokens",
        "captured_at",
        "completed_at",
    }


def test_state_file_rejects_symlink_and_permissive_destinations(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}")
    target.chmod(0o600)
    symlink = tmp_path / "state.json"
    symlink.symlink_to(target)

    with pytest.raises(contracts.AcceptanceStateError, match="regular owner-only file"):
        state_module.write_acceptance_state(symlink, _valid_state())
    with pytest.raises(contracts.AcceptanceStateError, match="regular owner-only file"):
        state_module.read_acceptance_state(symlink)

    symlink.unlink()
    symlink.write_text("{}")
    symlink.chmod(0o640)
    with pytest.raises(contracts.AcceptanceStateError, match="regular owner-only file"):
        state_module.write_acceptance_state(symlink, _valid_state())
    with pytest.raises(contracts.AcceptanceStateError, match="regular owner-only file"):
        state_module.read_acceptance_state(symlink)


def test_state_file_accepts_owner_read_only_mode_distinct_from_exact_mode_documents(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = _valid_state()
    state_module.write_acceptance_state(path, state)
    path.chmod(0o400)

    assert state_module.read_acceptance_state(path) == state

    state_module.write_acceptance_state(path, state)
    assert path.stat().st_mode & 0o777 == 0o600


def test_state_file_rejects_extra_fields_and_oversized_input(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    payload = _valid_state().to_dict()
    payload["password"] = "must-not-survive"
    path.write_text(json.dumps(payload))
    path.chmod(0o600)

    with pytest.raises(contracts.AcceptanceStateError, match="schema") as raised:
        state_module.read_acceptance_state(path)
    assert "must-not-survive" not in str(raised.value)

    path.write_bytes(b"x" * (contracts.MAX_STATE_FILE_BYTES + 1))
    path.chmod(0o600)
    with pytest.raises(contracts.AcceptanceStateError, match="too large"):
        state_module.read_acceptance_state(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_id", "not-a-uuid"),
        ("uploaded_sha256", "ABC"),
        ("run_status", "failed"),
        ("source_rows", 0),
        ("source_rows", True),
        ("failed_tokens", 1),
        ("captured_at", "not-a-timestamp"),
        ("completed_at", "2026-07-14T04:00:01"),
    ],
)
def test_state_schema_rejects_invalid_identifiers_hashes_accounting_and_timestamps(field: str, value: object) -> None:
    payload = _valid_state().to_dict()
    payload[field] = value

    with pytest.raises(contracts.AcceptanceStateError, match="schema"):
        state_module.AcceptanceState.from_dict(payload)


def test_state_write_cleans_temporary_file_when_atomic_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "state.json"

    def fail_replace(_source: str | os.PathLike[str], _destination: str | os.PathLike[str]) -> None:
        raise OSError("replace failed with /private/path")

    monkeypatch.setattr(state_module.os, "replace", fail_replace)
    with pytest.raises(contracts.AcceptanceStateError, match="write failed") as raised:
        state_module.write_acceptance_state(path, _valid_state())

    assert "/private/path" not in str(raised.value)
    assert list(tmp_path.iterdir()) == []


def test_scenario_resource_namespace_fits_strict_aws_name_limits() -> None:
    run_id = "4adf8a87-7fe2-44cc-9c9f-e39f9f51ac48"

    scenario_a = contracts.scenario_resource_namespace(run_id, "A")
    scenario_b = contracts.scenario_resource_namespace(run_id, "B")

    assert re.fullmatch(r"a-[0-9a-f]{20}", scenario_a)
    assert re.fullmatch(r"b-[0-9a-f]{20}", scenario_b)
    assert scenario_a != scenario_b
    assert len(f"{scenario_a}-alb") <= 32
    assert len(f"{scenario_a}-target") <= 32
    assert len(f"{scenario_a}-xray") <= 32


@pytest.mark.parametrize("value", ["2026-07-14T04:00:00", "2026-07-14T04:00:00+00:00", "invalid", 1])
def test_strict_utc_parser_preserves_domain_error_mapping(value: object) -> None:
    with pytest.raises(ValueError):
        contracts._parse_utc_z_timestamp(value)  # type: ignore[arg-type]
    with pytest.raises(contracts.AcceptanceStateError, match="state schema"):
        state_module._parse_state_timestamp(value)  # type: ignore[arg-type]
    with pytest.raises(contracts.AcceptanceCheckError, match="control_manifest_schema"):
        contracts._control_timestamp(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "arn:aws:ecs:ap-southeast-2:123456789012:task-definition/elspeth-web:7",
            "elspeth-web",
        ),
        ("arn:aws:ecs:ap-southeast-2:123456789012:task-definition/elspeth-web", None),
        (1, None),
    ],
)
def test_task_definition_family_parser_is_pure_while_wrapper_preserves_live_error(value: object, expected: str | None) -> None:
    assert contracts._parse_task_definition_family(value) == expected
    if expected is None:
        with pytest.raises(contracts.AcceptanceCheckError, match="orphan_sweep_api"):
            contracts._task_definition_family(value)
    else:
        assert contracts._task_definition_family(value) == expected


def test_moved_public_foundations_are_facade_reexports_by_identity() -> None:
    for name in (
        "CONNECT_TIMEOUT_SECONDS",
        "EVIDENCE_KINDS",
        "FORBIDDEN_AWS_OVERRIDE_ENV",
        "MAX_BLOB_RESPONSE_BYTES",
        "MAX_CONTROL_DOCUMENT_BYTES",
        "MAX_EXEC_RECEIPT_CHARS",
        "MAX_EXEC_STREAM_BYTES",
        "MAX_JSON_RESPONSE_BYTES",
        "MAX_STATE_FILE_BYTES",
        "PLUGIN_POLICY_ASSIGNMENT_NAMES",
        "POOL_TIMEOUT_SECONDS",
        "READ_TIMEOUT_SECONDS",
        "RUN_POLL_DEADLINE_SECONDS",
        "RUN_POLL_INTERVAL_SECONDS",
        "SCENARIO_ASSIGNMENT_NAMES",
        "WRITE_TIMEOUT_SECONDS",
    ):
        assert getattr(acceptance, name) is getattr(contracts, name)

    for name, owner in {
        "AcceptanceCheckError": contracts,
        "AcceptanceCredentials": state_module,
        "AcceptanceHttpError": contracts,
        "AcceptanceInputError": contracts,
        "AcceptanceState": state_module,
        "AcceptanceStateError": contracts,
        "OperatorTelemetryAcceptanceError": contracts,
        "SanitizedResourceIdentity": contracts,
        "normalize_acceptance_origin": contracts,
        "plugin_policy_binding_sha256": contracts,
        "read_acceptance_state": state_module,
        "scenario_resource_namespace": contracts,
        "write_acceptance_state": state_module,
    }.items():
        assert getattr(acceptance, name) is getattr(owner, name)


def _mutate_protected_document_in_process(
    path_value: str,
    item: str,
    entered_queue: Any,
    release_event: Any,
    result_queue: Any,
) -> None:
    secure_documents = importlib.import_module("elspeth.web._aws_ecs_acceptance.secure_documents")

    @secure_documents._serialized_control_manifest_write
    def mutate(path: Path) -> None:
        payload = secure_documents._read_protected_document(path, check="control_manifest_file")
        entered_queue.put(item)
        if item == "first" and not release_event.wait(timeout=10):
            raise RuntimeError("release timeout")
        items = payload["items"]
        if not isinstance(items, list):
            raise RuntimeError("invalid items")
        secure_documents._write_protected_document(
            path,
            {"items": [*items, item]},
            create=False,
            exists_check="control_manifest_exists",
            write_check="control_manifest_file",
        )

    try:
        mutate(Path(path_value))
    except BaseException as exc:
        result_queue.put(("error", item, type(exc).__name__))
    else:
        result_queue.put(("ok", item, None))


def test_serialized_control_manifest_write_locks_the_complete_transaction(tmp_path: Path) -> None:
    pytest.importorskip("fcntl")
    secure_documents = importlib.import_module("elspeth.web._aws_ecs_acceptance.secure_documents")
    assert secure_documents is not None

    path = tmp_path / "control.json"
    path.write_text(json.dumps({"items": []}))
    path.chmod(0o600)

    context = multiprocessing.get_context("spawn")
    entered_queue = context.Queue()
    result_queue = context.Queue()
    release_event = context.Event()
    first = context.Process(
        target=_mutate_protected_document_in_process,
        args=(str(path), "first", entered_queue, release_event, result_queue),
    )
    second = context.Process(
        target=_mutate_protected_document_in_process,
        args=(str(path), "second", entered_queue, release_event, result_queue),
    )
    processes = (first, second)
    try:
        first.start()
        assert entered_queue.get(timeout=20) == "first"
        second.start()
        with pytest.raises(Empty):
            entered_queue.get(timeout=1)
        release_event.set()
        assert entered_queue.get(timeout=20) == "second"
    finally:
        release_event.set()
        for process in processes:
            if process.pid is None:
                continue
            process.join(timeout=20)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert [process.exitcode for process in processes] == [0, 0]
    assert {result_queue.get(timeout=5) for _ in processes} == {
        ("ok", "first", None),
        ("ok", "second", None),
    }
    assert json.loads(path.read_text()) == {"items": ["first", "second"]}
    assert path.stat().st_mode & 0o777 == 0o600
    assert not [candidate for candidate in os.listdir(tmp_path) if candidate.endswith(".tmp")]


def test_receipt_manifest_lock_retries_interrupted_flock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("fcntl")
    secure_documents = importlib.import_module("elspeth.web._aws_ecs_acceptance.secure_documents")
    real_fcntl = secure_documents._fcntl
    operations: list[int] = []

    def interrupted_once(descriptor: int, operation: int) -> None:
        operations.append(operation)
        if operation == real_fcntl.LOCK_EX and operations.count(real_fcntl.LOCK_EX) == 1:
            raise InterruptedError
        real_fcntl.flock(descriptor, operation)

    monkeypatch.setattr(
        secure_documents,
        "_fcntl",
        SimpleNamespace(
            LOCK_EX=real_fcntl.LOCK_EX,
            LOCK_UN=real_fcntl.LOCK_UN,
            flock=interrupted_once,
        ),
    )

    path = tmp_path / "control.json"
    with secure_documents._receipt_manifest_write_lock(path, check="control_manifest_file"):
        pass

    assert operations == [real_fcntl.LOCK_EX, real_fcntl.LOCK_EX, real_fcntl.LOCK_UN]


def test_protected_document_write_rejects_symlinked_immediate_parent(tmp_path: Path) -> None:
    secure_documents = importlib.import_module("elspeth.web._aws_ecs_acceptance.secure_documents")
    real_parent = tmp_path / "real"
    real_parent.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(acceptance.AcceptanceCheckError, match="control_manifest_parent"):
        secure_documents._write_protected_document(
            alias / "control.json",
            {"items": []},
            create=True,
            exists_check="control_manifest_exists",
            write_check="control_manifest_file",
        )

    assert not (real_parent / "control.json").exists()


def test_protected_document_writers_preserve_process_umask_under_concurrency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secure_documents = importlib.import_module("elspeth.web._aws_ecs_acceptance.secure_documents")
    real_mkstemp = secure_documents.tempfile.mkstemp
    control_entered = threading.Event()
    state_entered = threading.Event()
    control_finished = threading.Event()
    errors: list[BaseException] = []

    def control_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        control_entered.set()
        assert state_entered.wait(timeout=10)
        return real_mkstemp(*args, **kwargs)  # type: ignore[arg-type]

    def state_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
        state_entered.set()
        assert control_finished.wait(timeout=10)
        return real_mkstemp(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(secure_documents, "tempfile", SimpleNamespace(mkstemp=control_mkstemp))
    monkeypatch.setattr(state_module, "tempfile", SimpleNamespace(mkstemp=state_mkstemp))

    def write_control() -> None:
        try:
            secure_documents._write_protected_document(
                tmp_path / "control.json",
                {"items": []},
                create=True,
                exists_check="control_manifest_exists",
                write_check="control_manifest_file",
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            control_finished.set()

    def write_state() -> None:
        try:
            state_module.write_acceptance_state(tmp_path / "state.json", _valid_state())
        except BaseException as exc:
            errors.append(exc)

    previous_umask = os.umask(0o022)
    try:
        control_thread = threading.Thread(target=write_control)
        state_thread = threading.Thread(target=write_state)
        control_thread.start()
        assert control_entered.wait(timeout=10)
        state_thread.start()
        control_thread.join(timeout=10)
        state_thread.join(timeout=10)
        assert not control_thread.is_alive()
        assert not state_thread.is_alive()
        observed_umask = os.umask(0o022)
    finally:
        os.umask(previous_umask)

    assert errors == []
    assert observed_umask == 0o022
    assert (tmp_path / "control.json").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "state.json").stat().st_mode & 0o777 == 0o600


def test_protected_document_writers_force_exact_0600_under_restrictive_umask(tmp_path: Path) -> None:
    secure_documents = importlib.import_module("elspeth.web._aws_ecs_acceptance.secure_documents")
    previous_umask = os.umask(0o777)
    try:
        secure_documents._write_protected_document(
            tmp_path / "control.json",
            {"items": []},
            create=True,
            exists_check="control_manifest_exists",
            write_check="control_manifest_file",
        )
        state_module.write_acceptance_state(tmp_path / "state.json", _valid_state())
    finally:
        os.umask(previous_umask)

    assert (tmp_path / "control.json").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "state.json").stat().st_mode & 0o777 == 0o600
