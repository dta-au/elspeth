"""Readiness serves a recent CURRENT schema-shape verdict instead of reflecting on every probe.

Every test counts calls on the REAL schema probes (``probe_session_schema`` /
``probe_landscape_schema`` from ``elspeth.web.schema_probe``) through a
counting wrapper installed where readiness looks them up, against real
SQLite stores, so a count is the number of schema reflections readiness ran.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretBytes
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import Connection

import elspeth.web.readiness as readiness
from elspeth.web.app import create_app
from elspeth.web.config import WebSettings
from elspeth.web.schema_probe import (
    SchemaState,
    init_landscape_schema,
    probe_landscape_schema,
    probe_session_schema,
)


class _ProbeSpy:
    """Count calls to a real schema probe, optionally holding or overriding one call."""

    def __init__(self, real: Callable[[Engine | Connection], SchemaState]) -> None:
        self._real = real
        self.calls = 0
        self.answers: list[SchemaState] = []
        self.before_call: Callable[[int], None] | None = None

    def __call__(self, bind: Engine | Connection) -> SchemaState:
        self.calls += 1
        if self.before_call is not None:
            self.before_call(self.calls)
        state = self.answers.pop(0) if self.answers else self._real(bind)
        return state


def _settings(tmp_path: Path) -> WebSettings:
    # The directories a deployment provides, so the only checks that can fail
    # are the database ones under test.
    for directory in (tmp_path / "payloads", tmp_path / "blobs"):
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    return WebSettings(
        data_dir=tmp_path,
        composer_max_composition_turns=15,
        composer_context_max_prompt_tokens=200_000,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=SecretBytes(b"\x00" * 32),
        operator_metrics_bearer_token="operator-metrics-token-for-tests-0001",
    )


@pytest.fixture
def spies(monkeypatch: pytest.MonkeyPatch) -> dict[str, _ProbeSpy]:
    session = _ProbeSpy(probe_session_schema)
    landscape = _ProbeSpy(probe_landscape_schema)
    monkeypatch.setattr(readiness, "probe_session_schema", session)
    monkeypatch.setattr(readiness, "probe_landscape_schema", landscape)
    return {"session": session, "landscape": landscape}


def _counts(spies: dict[str, _ProbeSpy]) -> dict[str, int]:
    return {kind: spy.calls for kind, spy in spies.items()}


def _get_ready(client: TestClient, app: FastAPI) -> Any:
    # Expire the route's two-second report cache so every request is a real
    # probe; only the schema-shape verdict may be reused.
    app.state.readiness_cache._completed_at = float("-inf")
    return client.get("/api/ready")


class TestRouteReusesShapeVerdict:
    def test_probes_within_the_interval_do_not_reflect_again(self, tmp_path: Path, spies: dict[str, _ProbeSpy]) -> None:
        app = create_app(_settings(tmp_path))
        client = TestClient(app)

        responses = [_get_ready(client, app) for _ in range(4)]

        assert [response.status_code for response in responses] == [200, 200, 200, 200]
        assert all(response.json() == responses[0].json() for response in responses)
        assert _counts(spies) == {"session": 1, "landscape": 1}

    def test_probe_after_the_interval_reflects_again(
        self, tmp_path: Path, spies: dict[str, _ProbeSpy], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = create_app(_settings(tmp_path))
        client = TestClient(app)
        clock = [1000.0]
        monkeypatch.setattr(app.state.readiness_probe_runner.schema_verdicts, "_now", lambda: clock[0])

        assert _get_ready(client, app).status_code == 200
        assert _counts(spies) == {"session": 1, "landscape": 1}

        clock[0] += readiness.SCHEMA_SHAPE_REFRESH_SECONDS - 0.001
        assert _get_ready(client, app).status_code == 200
        assert _counts(spies) == {"session": 1, "landscape": 1}

        clock[0] += 0.001
        assert _get_ready(client, app).status_code == 200
        assert _counts(spies) == {"session": 2, "landscape": 2}

    def test_failed_connectivity_check_forces_a_refresh_inside_the_interval(
        self, tmp_path: Path, spies: dict[str, _ProbeSpy], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = create_app(_settings(tmp_path))
        client = TestClient(app)
        monkeypatch.setattr(app.state.readiness_probe_runner.schema_verdicts, "_now", lambda: 1000.0)
        assert _get_ready(client, app).status_code == 200
        assert _counts(spies) == {"session": 1, "landscape": 1}

        def unreachable(*_args: object, **_kwargs: object) -> Engine:
            raise ConnectionRefusedError("landscape unreachable")

        real_create_engine = readiness.create_engine
        monkeypatch.setattr(readiness, "create_engine", unreachable)
        failed = _get_ready(client, app)
        assert failed.status_code == 503
        by_name = {check["name"]: check for check in failed.json()["checks"]}
        assert by_name["landscape_db"] == {"name": "landscape_db", "ok": False, "detail": "probe failed (ConnectionRefusedError)"}
        assert by_name["landscape_schema"]["detail"] == "not checked: connectivity probe failed"

        monkeypatch.setattr(readiness, "create_engine", real_create_engine)
        recovered = _get_ready(client, app)
        assert recovered.status_code == 200
        # The landscape verdict was discarded by the failed probe; the session
        # database stayed reachable throughout and keeps its verdict.
        assert _counts(spies) == {"session": 1, "landscape": 2}

    def test_cached_failure_does_not_mask_recovery(
        self, tmp_path: Path, spies: dict[str, _ProbeSpy], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = create_app(_settings(tmp_path))
        client = TestClient(app)
        monkeypatch.setattr(app.state.readiness_probe_runner.schema_verdicts, "_now", lambda: 1000.0)
        spies["session"].answers.append(SchemaState.STALE)

        stale = _get_ready(client, app)
        assert stale.status_code == 503
        by_name = {check["name"]: check for check in stale.json()["checks"]}
        assert by_name["session_schema"] == {"name": "session_schema", "ok": False, "detail": "schema state: STALE"}

        # Same instant: a not-current verdict is never served from cache.
        assert _get_ready(client, app).status_code == 200
        assert _get_ready(client, app).status_code == 200
        assert _counts(spies) == {"session": 2, "landscape": 1}

    def test_response_body_shape_is_unchanged_for_ready_and_not_ready(self, tmp_path: Path, spies: dict[str, _ProbeSpy]) -> None:
        app = create_app(_settings(tmp_path))
        client = TestClient(app)

        fresh = _get_ready(client, app)
        cached = _get_ready(client, app)
        spies["landscape"].answers.append(SchemaState.PARTIAL)
        app.state.readiness_probe_runner.schema_verdicts.invalidate("landscape")
        not_ready = _get_ready(client, app)

        assert (fresh.status_code, cached.status_code, not_ready.status_code) == (200, 200, 503)
        assert fresh.content == cached.content
        for response in (fresh, not_ready):
            body = response.json()
            assert list(body) == ["ready", "checks"]
            assert [check["name"] for check in body["checks"]] == list(readiness.READINESS_CHECK_NAMES)
            assert all(list(check) == ["name", "ok", "detail"] for check in body["checks"])
        by_name = {check["name"]: check for check in fresh.json()["checks"]}
        assert by_name["session_schema"] == {"name": "session_schema", "ok": True, "detail": "schema state: CURRENT"}
        assert by_name["landscape_db"] == {"name": "landscape_db", "ok": True, "detail": "connected"}
        assert not_ready.json()["ready"] is False
        failed = [check for check in not_ready.json()["checks"] if not check["ok"]]
        assert failed == [{"name": "landscape_schema", "ok": False, "detail": "schema state: PARTIAL"}]


@pytest.fixture
def landscape_engine(tmp_path: Path) -> Any:
    engine = create_engine(f"sqlite:///{tmp_path / 'audit.db'}")
    init_landscape_schema(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _state_in_thread(engine: Engine, verdicts: Any, results: list[SchemaState]) -> threading.Thread:
    def body() -> None:
        with engine.connect() as conn:
            results.append(verdicts.state("landscape", conn))

    thread = threading.Thread(target=body)
    thread.start()
    return thread


class TestSchemaShapeVerdicts:
    def test_overlapping_probes_run_one_reflection(self, landscape_engine: Engine, spies: dict[str, _ProbeSpy]) -> None:
        verdicts = readiness.SchemaShapeVerdicts()
        first_inside = threading.Event()
        second_started = threading.Event()

        def hold_first(call: int) -> None:
            if call == 1:
                first_inside.set()
                assert second_started.wait(5)
                # Give the second probe time to reach the refresh it must wait on.
                time.sleep(0.2)

        spies["landscape"].before_call = hold_first
        results: list[SchemaState] = []
        first = _state_in_thread(landscape_engine, verdicts, results)
        assert first_inside.wait(5)
        second_started.set()
        second = _state_in_thread(landscape_engine, verdicts, results)
        first.join(10)
        second.join(10)

        assert results == [SchemaState.CURRENT, SchemaState.CURRENT]
        assert spies["landscape"].calls == 1

    def test_reflection_running_when_invalidated_is_not_cached(self, landscape_engine: Engine, spies: dict[str, _ProbeSpy]) -> None:
        verdicts = readiness.SchemaShapeVerdicts()
        inside = threading.Event()
        release = threading.Event()

        def hold(call: int) -> None:
            if call == 1:
                inside.set()
                assert release.wait(5)

        spies["landscape"].before_call = hold
        results: list[SchemaState] = []
        running = _state_in_thread(landscape_engine, verdicts, results)
        assert inside.wait(5)
        verdicts.invalidate("landscape")
        release.set()
        running.join(10)
        assert results == [SchemaState.CURRENT]

        with landscape_engine.connect() as conn:
            assert verdicts.state("landscape", conn) is SchemaState.CURRENT
        assert spies["landscape"].calls == 2

    def test_reflection_that_raises_leaves_no_verdict(
        self, landscape_engine: Engine, spies: dict[str, _ProbeSpy], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        verdicts = readiness.SchemaShapeVerdicts()
        clock = [1000.0]
        monkeypatch.setattr(verdicts, "_now", lambda: clock[0])
        with landscape_engine.connect() as conn:
            assert verdicts.state("landscape", conn) is SchemaState.CURRENT
            clock[0] += readiness.SCHEMA_SHAPE_REFRESH_SECONDS

            def fail(call: int) -> None:
                if call == 2:
                    raise TimeoutError("statement timeout")

            spies["landscape"].before_call = fail
            with pytest.raises(TimeoutError):
                verdicts.state("landscape", conn)
            assert verdicts.state("landscape", conn) is SchemaState.CURRENT
        assert spies["landscape"].calls == 3

    @pytest.mark.parametrize("detail", ["probe timed out", "probe already in flight"])
    def test_report_with_failed_database_probe_discards_that_verdict(
        self, tmp_path: Path, landscape_engine: Engine, spies: dict[str, _ProbeSpy], detail: str
    ) -> None:
        verdicts = readiness.SchemaShapeVerdicts()
        with landscape_engine.connect() as conn:
            assert verdicts.state("landscape", conn) is SchemaState.CURRENT

        class StaticRunner:
            """A probe runner whose database probes did not complete in time."""

            schema_verdicts = verdicts

            async def run(self, label: str, check_names: tuple[str, ...], *_args: object) -> tuple[readiness.ReadinessCheck, ...]:
                ok = label not in {"session", "landscape"}
                return tuple(readiness.ReadinessCheck(name, ok, "ok" if ok else detail) for name in check_names)

        report = asyncio.run(
            readiness.readiness_report(
                _settings(tmp_path),
                landscape_engine,
                StaticRunner(),
                "sqlite-single",
                instance_draining=threading.Event(),
            )
        )
        assert report.ready is False

        with landscape_engine.connect() as conn:
            assert verdicts.state("landscape", conn) is SchemaState.CURRENT
        assert spies["landscape"].calls == 2
