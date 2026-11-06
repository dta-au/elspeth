"""Shared fixtures for elspeth-lints unit tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

_JUDGE_KEY = "ELSPETH_JUDGE_METADATA_HMAC_KEY"
_UNSET = object()
_JUDGE_KEY_SNAPSHOT = pytest.StashKey[object]()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item: pytest.Item) -> Iterator[None]:
    """Snapshot the judge-key state before any fixture (incl. monkeypatch) runs."""
    item.stash[_JUDGE_KEY_SNAPSHOT] = os.environ.get(_JUDGE_KEY, _UNSET)
    return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> Iterator[None]:
    """Fail any test that leaks ELSPETH_JUDGE_METADATA_HMAC_KEY into os.environ.

    The sign/diagnose CLIs under test load the operator key from ``--env-file``
    into ``os.environ`` for the life of the process, so a test that exercises
    that path must remove the key itself with ``os.environ.pop`` in a
    ``finally`` block. ``monkeypatch.delenv`` cannot do that job: it snapshots
    the polluted value at call time and faithfully restores it at teardown.
    Under xdist a leaked key trips the [O1] key-free guard in whichever
    test_tier_model_corpus tests later share the worker, far from the culprit.

    Implemented as a runtest-protocol wrapper (not an autouse fixture) so the
    check runs after every fixture finalizer, monkeypatch undo included.
    """
    result = yield
    before = item.stash.get(_JUDGE_KEY_SNAPSHOT, _UNSET)
    after = os.environ.get(_JUDGE_KEY, _UNSET)
    if after is before or after == before:
        return result
    # Repair the environment first so one leaking test cannot cascade into
    # key-free-guard failures in unrelated tests on the same xdist worker.
    if before is _UNSET:
        os.environ.pop(_JUDGE_KEY, None)
    else:
        os.environ[_JUDGE_KEY] = before  # type: ignore[assignment]
    pytest.fail(
        f"test leaked {_JUDGE_KEY} into os.environ (state changed across the test). "
        "If the code under test loads it (e.g. --env-file), remove it with "
        "os.environ.pop in a finally block; monkeypatch.delenv restores the "
        "polluted value at teardown and re-leaks it."
    )


@pytest.fixture
def elspeth_lints_subprocess_env() -> dict[str, str]:
    """Environment for subprocesses that invoke the elspeth-lints CLI."""
    project_root = Path(__file__).resolve().parents[3]
    pythonpath_entries = [
        str(project_root / "src"),
        str(project_root / "elspeth-lints" / "src"),
    ]
    existing_pythonpath = os.environ.get("PYTHONPATH")
    if existing_pythonpath:
        pythonpath_entries.append(existing_pythonpath)
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(pythonpath_entries),
    }


@pytest.fixture
def synthetic_operator_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Stub only OS admission for explicitly opted-in synthetic-key unit tests.

    These tests exercise signing and recovery after trusted startup. Real
    admission/refusal tests deliberately do not request this fixture.
    """
    from elspeth_lints.core import operator_runtime

    evidence = operator_runtime.OperatorRuntimeEvidence(
        artifact_root=tmp_path / "synthetic-runtime",
        artifact_id="0" * 64,
        operator_uid=2001,
        candidate_uid=2002,
        invocation_cwd=tmp_path,
        argv=(),
    )
    monkeypatch.setattr(operator_runtime, "require_operator_runtime", lambda: evidence)


@pytest.fixture
def synthetic_judge_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise mocked provider SDKs downstream of the separately tested RPC.

    Production routing and worker admission tests never opt into this fixture.
    """
    from elspeth_lints.core import judge, judge_rpc, judge_worker

    def call_mock_provider(
        request: judge.JudgeRequest,
        *,
        model_id: str,
        max_tokens: int,
        transport: str,
        tool_scope: judge.AgentToolScope | None,
    ) -> judge._TransportResult:
        implementation = judge._TRANSPORTS[transport]
        if tool_scope is not None:
            return implementation(request, model_id, max_tokens, tool_scope=tool_scope)
        return implementation(request, model_id, max_tokens)

    monkeypatch.setattr(judge_worker, "require_worker_execution", lambda: None)
    monkeypatch.setattr(judge_rpc, "call_worker", call_mock_provider)
