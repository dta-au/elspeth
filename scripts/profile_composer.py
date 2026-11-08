"""Profile real HTTP admission and durable Composer worker settlement.

Uses the actual
create_app wiring, local JWT authentication, request middleware, job admission,
worker claim, Composer, post-compose validation/save, and terminal transaction.
Model SDK transport/readiness and OS watchdog test authority are controlled.
The repository fixture uses explicit debug-when-unconfigured prompt-guard mode;
effective model and budget settings are recorded, with real fenced call audits.
Healthy worker claims are driven inline using the repository-supported
run_until_idle testing seam, rather than measuring an arbitrary scan delay.

Requires repository development/all-extras dependencies (uv sync --frozen
--all-extras), including test helpers. From the repository root with both source
roots on PYTHONPATH, invoke python -m scripts.profile_composer --help.
Each invocation requires a new run directory/output and a quiet host. SQL
counts mean cursor statements, never rows returned/scanned or total DB work.
CPU is the instrumented Python process (including its worker threads), not the
PostgreSQL server. Cursor time excludes result fetching and transaction API
commit time. Wall time includes instrumentation; subtracting artificial SDK
wait is a diagnostic remainder, not a measurement of pure application compute.
This feature-tree cache-disabled control is not an unmodified main baseline.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import importlib.metadata
import json
import math
import os
import re
import resource
import secrets
import subprocess
import sys
import threading
import time
import tracemalloc
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
import litellm
from sqlalchemy import Engine, create_engine, event, func, select, text
from tests.fixtures.composer_session_cache import make_chain_composer_state, make_small_composer_state
from tests.fixtures.identities import ensure_test_identity, grant_test_pipeline_user
from tests.fixtures.process_watchdog import OwnedTestProcessWatchdog
from tests.helpers.composer_operations import message_body, submit_and_settle
from tests.helpers.postgres_target import postgres_test_target
from tests.unit.web.composer.conftest import _make_settings

from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.app import create_app
from elspeth.web.async_workers import run_sync_in_worker, shutdown_async_workers
from elspeth.web.auth.local import LocalAuthProvider
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.advisor_audit import (
    ADVISOR_CHECKPOINT_PASS_AUDIT_KIND,
    ADVISOR_GRAPH_REVIEW_AUDIT_KIND,
    AdvisorCheckpointPassRecord,
    AdvisorGraphReviewFact,
)
from elspeth.web.composer.advisor_decision import AdvisorBlockCause, AdvisorGateBlocked, AdvisorGatePassed
from elspeth.web.composer.availability import ComposerAvailability
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools import execute_tool
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.execution.completion_gates import completion_gate_fingerprint, parse_completion_gates
from elspeth.web.schema_probe import init_landscape_schema, init_session_schema
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import chat_messages_table, composer_async_operations_table, composition_states_table
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.state_envelope import unwrap_state_column


def source_provenance():
    """Bind measurements to actual source, including uncommitted new modules."""
    root = Path(__file__).resolve().parents[1]
    paths = (
        subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "src", "tests", "scripts", "pyproject.toml", "uv.lock"],
            cwd=root,
        )
        .decode()
        .split("\0")
    )
    digest = hashlib.sha256()
    for relative in sorted(set(paths) - {""}):
        path = root / relative
        digest.update(relative.encode() + b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"<deleted>")
        digest.update(b"\0")
    return {
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "branch": subprocess.check_output(["git", "branch", "--show-current"], cwd=root, text=True).strip(),
        "source_sha256": digest.hexdigest(),
        "profiler_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "input_csv_sha256": hashlib.sha256((root / "examples/transform_pipeline/input.csv").read_bytes()).hexdigest(),
        "graph_fixture_sha256": hashlib.sha256((root / "tests/fixtures/composer_session_cache.py").read_bytes()).hexdigest(),
        "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=root)),
    }


def measurement_environment():
    limits = {}
    for name in ("cpu.max", "memory.max", "cpuset.cpus.effective"):
        path = Path("/sys/fs/cgroup") / name
        limits[name] = path.read_text().strip() if path.is_file() else None
    return {
        "python": sys.version,
        "cpu_affinity": sorted(os.sched_getaffinity(0)),
        "cgroup": limits,
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("SQLAlchemy", "litellm", "httpx", "psycopg", "pydantic", "fastapi", "starlette", "anyio")
        },
        "load_average": os.getloadavg(),
    }


def safe_settings(settings):
    """Explicit nonsecret fields; never reflect over the settings object."""
    return {
        "composer_model": settings.composer_model,
        "composer_advisor_model": settings.composer_advisor_model,
        "composer_timeout_seconds": settings.composer_timeout_seconds,
        "composer_runtime_preflight_timeout_seconds": settings.composer_runtime_preflight_timeout_seconds,
        "composer_advisor_timeout_seconds": settings.composer_advisor_timeout_seconds,
        "composer_max_composition_turns": settings.composer_max_composition_turns,
        "composer_max_discovery_turns": settings.composer_max_discovery_turns,
        "composer_rate_limit_per_minute": settings.composer_rate_limit_per_minute,
        "composer_advisor_checkpoint_max_passes": settings.composer_advisor_checkpoint_max_passes,
        "composer_advisor_max_prompt_tokens": settings.composer_advisor_max_prompt_tokens,
        "composer_advisor_max_completion_tokens": settings.composer_advisor_max_completion_tokens,
        "composer_discovery_reasoning_effort": settings.composer_discovery_reasoning_effort,
        "composer_candidate_reasoning_effort": settings.composer_candidate_reasoning_effort,
        "composer_advisor_reasoning_effort": settings.composer_advisor_reasoning_effort,
        "composer_prompt_guard_mode": settings.composer_prompt_guard_mode,
        "composer_session_cache_enabled": settings.composer_session_cache_enabled,
    }


class SQLMeter:
    """Count actual SQLAlchemy cursor executions; transaction API separately."""

    def __init__(self, engine):
        self.lock = threading.Lock()
        self.active = False
        self.counts = Counter()
        self.sql_ns = 0
        self.executemany_calls = 0
        self.shapes = {}
        self.generation = 0
        self.inflight = {}
        event.listen(engine, "before_cursor_execute", self.before)
        event.listen(engine, "after_cursor_execute", self.after)
        event.listen(engine, "handle_error", self.failed)
        for name in ("begin", "commit", "rollback"):
            event.listen(engine, name, self.transaction(name))

    def transaction(self, name):
        def observe(connection):
            with self.lock:
                if self.active:
                    self.counts[f"transaction_{name}"] += 1

        return observe

    def before(self, connection, cursor, statement, parameters, context, executemany):
        with self.lock:
            if not self.active:
                return
            generation = self.generation
        # Never retain parameter values. Also remove SQL literal strings/numbers
        # from textual statements; preserve identifiers and placeholders.
        shape = re.sub(r"'(?:''|[^'])*'", "?", statement)
        shape = re.sub(r"\b\d+(?:\.\d+)?\b", "?", shape)
        shape = " ".join(shape.split())
        # Compiled DML flags avoid misclassifying INSERT...RETURNING as reads.
        if context.isinsert or context.isupdate or context.isdelete:
            kind = "write"
        else:
            verb = statement.lstrip().split(None, 1)[0].upper()
            kind = (
                "read" if verb in {"SELECT", "SHOW", "EXPLAIN"} else "write" if verb in {"INSERT", "UPDATE", "DELETE", "MERGE"} else "other"
            )
        with self.lock:
            if not self.active or generation != self.generation:
                return
            self.counts["statements"] += 1
            self.counts[kind] += 1
            self.executemany_calls += int(executemany)
            item = self.shapes.setdefault(shape, {"count": 0, "cursor_ns": 0, "kind": kind})
            item["count"] += 1
            # Exclude normalization/counting and lock acquisition from timing.
            self.inflight[id(context)] = (self.generation, shape, time.perf_counter_ns())

    def after(self, connection, cursor, statement, parameters, context, executemany):
        ended = time.perf_counter_ns()
        with self.lock:
            pending = self.inflight.pop(id(context), None)
            if pending is not None:
                generation, shape, started = pending
                if not self.active or generation != self.generation:
                    return
                elapsed = ended - started
                self.sql_ns += elapsed
                self.shapes[shape]["cursor_ns"] += elapsed

    def failed(self, exception_context):
        with self.lock:
            pending = self.inflight.pop(id(exception_context.execution_context), None)
            if pending is not None and self.active and pending[0] == self.generation:
                self.counts["cursor_errors"] += 1

    def start(self):
        with self.lock:
            self.counts.clear()
            self.generation += 1
            self.shapes.clear()
            self.sql_ns = self.executemany_calls = 0
            self.active = True

    def stop(self):
        with self.lock:
            self.active = False
            shapes = [
                {"statement": shape, "count": item["count"], "kind": item["kind"], "cursor_ms": item["cursor_ns"] / 1e6}
                for shape, item in sorted(self.shapes.items())
            ]
            return {**self.counts, "cursor_ms": self.sql_ns / 1e6, "executemany_calls": self.executemany_calls, "statement_shapes": shapes}


class ProviderTransport:
    """Real gateway/admission around deterministic zero-network SDK replies."""

    def __init__(self, workload, delay, *, withhold=False):
        self.workload = workload
        self.delay = delay
        self.withhold = withhold
        self.turn = self.primary = self.calls = 0
        self.wait_ns = 0
        self.request_bytes = []
        self.request_categories = Counter()

    def begin(self, turn):
        self.turn, self.primary, self.calls, self.wait_ns = turn, 0, 0, 0
        self.request_bytes.clear()
        self.request_categories.clear()

    async def __call__(self, **kwargs):
        self.calls += 1
        self.request_bytes.append(len(json.dumps({"messages": kwargs["messages"], "tools": kwargs.get("tools")}, default=str).encode()))
        if self.delay:
            start = time.perf_counter_ns()
            await asyncio.sleep(self.delay)
            self.wait_ns += time.perf_counter_ns() - start
        calls = None
        if "response_format" in kwargs:
            schema_name = kwargs["response_format"]["json_schema"]["name"]
            verdict = {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "", "note": None}
            if schema_name == "advisor_end_response":
                verdict["publication_verdict"] = "WITHHOLD" if self.withhold else "ADMIT"
                if self.withhold:
                    verdict["findings"] = "Controlled publication refusal: do not publish the candidate reply."
            elif schema_name != "advisor_checkpoint_response":
                raise RuntimeError(f"Unscripted structured provider schema: {schema_name}")
            self.request_categories[schema_name] += 1
            content = json.dumps(verdict)
        else:
            name = "get_plugin_schema" if self.workload == "discovery" else "set_metadata"
            offered = {tool["function"]["name"] for tool in kwargs.get("tools", ())}
            if name not in offered:
                raise RuntimeError("Unscripted provider request: expected Composer tool surface, not title/special request")
            self.primary += 1
            if self.primary == 1:
                self.request_categories["composer_tool_request"] += 1
                arguments = (
                    {"plugin_type": "source", "name": "csv"}
                    if self.workload == "discovery"
                    else {"patch": {"name": f"Profile draft {self.turn}", "description": "Controlled authored metadata change"}}
                )
                calls = [
                    {
                        "id": f"profile-{self.turn}-{uuid4()}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                ]
                content = None
            else:
                if self.primary > 2:
                    raise RuntimeError("Unexpected Composer repair/additional round; refuse to profile an unhealthy scripted turn")
                self.request_categories["composer_final_response"] += 1
                content = f"Turn {self.turn}: the requested draft information is ready."
        return litellm.ModelResponse(
            choices=[
                {
                    "message": {"role": "assistant", "content": content, "tool_calls": calls},
                    "finish_reason": "tool_calls" if calls else "stop",
                    "index": 0,
                }
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20, "cost": 0.001},
            model=kwargs["model"],
        )


def rss_bytes():
    # Linux RSS is sampled, not a per-turn peak; ru_maxrss is process lifetime.
    pages = int(Path("/proc/self/statm").read_text().split()[1])
    return pages * os.sysconf("SC_PAGE_SIZE")


def filtered_retained_bytes():
    """Exclude direct harness/tracemalloc allocations, not caller stacks.

    This is not pure application heap: indirect allocations in SDKs/libraries
    caused by measurement remain. Never call in the CPU timing mode.
    """
    snapshot = tracemalloc.take_snapshot()
    filtered = snapshot.filter_traces(
        (
            tracemalloc.Filter(False, str(Path(__file__).resolve()), all_frames=False),
            tracemalloc.Filter(False, tracemalloc.__file__, all_frames=False),
        )
    )
    total = sum(trace.size for trace in filtered.traces)
    del filtered, snapshot
    return total


async def acquire(sessions, sid):
    return await SessionOperationLease.acquire(
        sessions.session_operation_authority,
        session_id=sid,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=sessions.session_operation_owner_instance_id,
        lease_seconds=sessions.session_operation_lease_seconds,
    )


async def seed(app, graph):
    sessions = app.state.session_service
    auth = app.state.auth_provider
    assert isinstance(auth, LocalAuthProvider)
    auth.create_user("profile-user", "local profile fixture password", "Profile User")
    with app.state.session_engine.begin() as connection:
        ensure_test_identity(connection, identity_id="profile-user")
        grant_test_pipeline_user(connection, identity_id="profile-user")
    token = auth._issuer.mint(identity_id="profile-user", username="profile-user")
    session = await sessions.create_session("profile-user", "Explicit profile draft", "local")
    state = make_small_composer_state() if graph == "small" else make_chain_composer_state(80)
    raw = state.to_dict()
    async with await acquire(sessions, session.id) as lease:
        # Real ready blob and permitted per-session output paths; the workload
        # should not become a missing-file/path-refusal benchmark by accident.
        blob = await app.state.blob_service.create_blob(
            session.id,
            "profile-input.csv",
            (Path.cwd() / "examples/transform_pipeline/input.csv").read_bytes(),
            "text/csv",
            session_operation_context=lease.context,
        )
        output_dir = app.state.settings.data_dir / "outputs" / str(session.id)
        output_dir.mkdir(parents=True, exist_ok=True)
        for output in raw["outputs"]:
            output["options"]["path"] = str(output_dir / f"{output['name']}.csv")
        state = CompositionState.from_dict(raw)
        snapshot = app.state.plugin_snapshot_factory.for_user_id("profile-user")
        catalog = PolicyCatalogView(app.state.catalog_service, snapshot, app.state.operator_profile_registry)
        for name, source in raw["sources"].items():
            options = dict(source["options"])
            options.pop("path", None)
            bound = await run_sync_in_worker(
                execute_tool,
                "set_source_from_blob",
                {
                    "source_name": name,
                    "blob_id": str(blob.id),
                    "plugin": source["plugin"],
                    "on_success": source["on_success"],
                    "on_validation_failure": source["on_validation_failure"],
                    "options": options,
                },
                state,
                catalog,
                plugin_snapshot=snapshot,
                data_dir=str(app.state.settings.data_dir),
                session_engine=app.state.session_engine,
                session_id=str(session.id),
                session_operation_context=lease.context,
                session_operation_authority=sessions.session_operation_authority,
                user_id="profile-user",
            )
            assert bound.success, bound
            state = bound.updated_state
        raw = state.to_dict()
        validation = catalog.validate_composition_state(state).validation
        assert validation.is_valid, validation
        await sessions.save_composition_state(
            session.id,
            CompositionStateData(
                sources=raw["sources"],
                nodes=raw["nodes"],
                edges=raw["edges"],
                outputs=raw["outputs"],
                metadata_=raw["metadata"],
                is_valid=state.validate().is_valid,
            ),
            provenance="session_seed",
            session_operation_context=lease.context,
        )
    return session.id, token


def durable_message_count(engine, sid):
    with engine.connect() as connection:
        return connection.scalar(select(func.count()).select_from(chat_messages_table).where(chat_messages_table.c.session_id == str(sid)))


def qualify_final_review(engine, sid, state_record, result, operation_id, *, withhold, authored_mutation):
    """Require actual END admission and the matching committed graph fact.

    Pending interpretation gates can remain legitimate; a green runtime check
    alone must never qualify an omitted or publication-withheld Advisor call.
    These reads are outside every measured interval.
    """
    identity = result.graph_review_identity
    assert identity is not None and identity.source == "model"
    assert identity.for_graph == completion_gate_fingerprint(result.state)
    assert composition_content_hash(state_from_record(state_record)) == composition_content_hash(result.state)
    fact = None
    with engine.connect() as connection:
        if authored_mutation:
            # Authored changes persist adjudication in a new immutable state;
            # only the review-only path emits AdvisorGraphReviewFact. Require
            # exact terminal-job/state custody, not a permissive missing-row fallback.
            stored = connection.execute(select(composition_states_table).where(composition_states_table.c.id == str(state_record.id))).one()
            job = connection.execute(
                select(composer_async_operations_table).where(
                    composer_async_operations_table.c.session_id == str(sid),
                    composer_async_operations_table.c.operation_id == operation_id,
                )
            ).one()
            assert stored.session_id == str(sid) and stored.provenance == "post_compose"
            assert stored.creator_operation_id == job.session_operation_id
            assert job.status == "completed" and json.loads(job.result_json)["state"]["id"] == str(state_record.id)
            gates = parse_completion_gates(unwrap_state_column(stored.composer_meta))
            if withhold:
                assert gates is not None and isinstance(result.advisor_gate_decision, AdvisorGateBlocked)
                assert gates.advisor_signoff == result.advisor_gate_decision.fact
            else:
                assert gates is None or gates.advisor_signoff is None
        else:
            review_row = connection.execute(
                select(chat_messages_table.c.tool_calls)
                .where(
                    chat_messages_table.c.session_id == str(sid),
                    chat_messages_table.c.role == "audit",
                    chat_messages_table.c.writer_principal == "compose_loop",
                    chat_messages_table.c.tool_calls[0]["_kind"].as_string() == ADVISOR_GRAPH_REVIEW_AUDIT_KIND,
                )
                .order_by(chat_messages_table.c.sequence_no.desc())
                .limit(1)
            ).one()
            assert len(review_row.tool_calls) == 1
            fact = AdvisorGraphReviewFact.from_envelope(review_row.tool_calls[0])
            assert fact.identity == identity
            assert fact.session_id == sid and fact.state_id == state_record.id
            assert fact.terminal_operation_id == operation_id
            assert fact.content_hash == composition_content_hash(result.state)
        checkpoint_row = connection.execute(
            select(chat_messages_table.c.tool_calls)
            .where(
                chat_messages_table.c.session_id == str(sid),
                chat_messages_table.c.role == "audit",
                chat_messages_table.c.writer_principal == "compose_loop",
                chat_messages_table.c.tool_calls[0]["_kind"].as_string() == ADVISOR_CHECKPOINT_PASS_AUDIT_KIND,
                chat_messages_table.c.tool_calls[0]["pass"]["phase"].as_string() == "end",
                chat_messages_table.c.tool_calls[0]["pass"]["publication_binding"].as_string() == identity.binding,
            )
            .order_by(chat_messages_table.c.sequence_no.desc())
            .limit(1)
        ).one()
        assert len(checkpoint_row.tool_calls) == 1
        checkpoint = AdvisorCheckpointPassRecord.from_dict(checkpoint_row.tool_calls[0]["pass"])
    assert checkpoint.source == "model" and checkpoint.verdict == "clean"
    assert checkpoint.provider_attempts == 1 and checkpoint.first_attempt_accepted is True
    assert checkpoint.format_reprompt_sent is False
    assert checkpoint.publication_binding == identity.binding
    if withhold:
        assert checkpoint.publication_verdict == "WITHHOLD"
        assert isinstance(result.advisor_gate_decision, AdvisorGateBlocked)
        assert result.advisor_gate_decision.fact.cause is AdvisorBlockCause.PUBLICATION_REJECTED
        assert result.advisor_terminal_published
        if fact is not None:
            assert fact.action == "blocked" and fact.gates.advisor_signoff is not None
            assert fact.gates.advisor_signoff.cause is AdvisorBlockCause.PUBLICATION_REJECTED
    else:
        assert checkpoint.publication_verdict == "ADMIT"
        assert result.advisor_gate_decision == AdvisorGatePassed(for_graph=identity.for_graph)
        assert not result.advisor_terminal_published
        if fact is not None:
            assert fact.action == "passed" and fact.gates.advisor_signoff is None
    return {
        "end_verdict": checkpoint.verdict,
        "publication_verdict": checkpoint.publication_verdict,
        "committed_graph_review": "blocked" if withhold else "passed",
        "review_storage": "authored_state_and_end_receipt" if authored_mutation else "review_only_graph_fact",
        "matching_review_identity": True,
        "provider_attempts": checkpoint.provider_attempts,
        "format_reprompt_sent": checkpoint.format_reprompt_sent,
    }


async def run(args, session_url=None, landscape_url=None):
    if session_url is not None:
        # Isolated targets only, provisioned by the repository helper. App
        # external-mode startup validates schemas and never creates them.
        for url, initialize in ((session_url, init_session_schema), (landscape_url, init_landscape_schema)):
            engine = create_engine(url)
            try:
                initialize(engine)
            finally:
                engine.dispose()
    args.run_dir.mkdir(parents=True, exist_ok=False)
    for name in ("blobs", "outputs", "runs", "payloads"):
        (args.run_dir / name).mkdir()
    settings = _make_settings(
        args.run_dir,
        composer_model=args.composer_model,
        composer_advisor_model=args.advisor_model,
        composer_max_composition_turns=args.composition_rounds,
        composer_max_discovery_turns=args.discovery_rounds,
        composer_timeout_seconds=args.turn_timeout,
        composer_boot_probe_enabled=False,
        composer_rate_limit_per_minute=args.rate_limit,
        composer_session_cache_enabled=not args.disable_cache,
        session_db_url=session_url,
        landscape_url=landscape_url,
        payload_store_path=args.run_dir / "payloads",
        # Fixture-local explicit policy for the shipped graph. Actual compiled
        # policy/snapshots remain installed; never use trained-operator bypass.
        plugin_allowlist=("transform:type_coerce", "transform:value_transform"),
        # External-state startup rejects the unit fixture's placeholder keys.
        # Fresh in-memory fixture keys satisfy the real contract without
        # changing checks or retaining authentication material in artifacts.
        **(
            {"secret_key": secrets.token_urlsafe(48), "shareable_link_signing_key": secrets.token_bytes(32)}
            if session_url is not None
            else {}
        ),
        **({"composer_advisor_checkpoint_max_passes": 1} if args.qualify_withhold else {}),
    )
    effective_settings = safe_settings(settings)
    transport = ProviderTransport(args.workload, args.provider_delay, withhold=args.qualify_withhold)

    def available(*, model, **kwargs):
        return ComposerAvailability(available=True, model=model, provider="profile-controlled")

    with patch("elspeth.web.composer.service.compute_availability", available), patch.object(litellm, "acompletion", transport):
        app = create_app(settings, process_watchdog_factory=OwnedTestProcessWatchdog)
        app.state.auth_audit_recorder.start()
        sid, token = await seed(app, args.graph)
        sessions = app.state.session_service
        registry = app.state.composer_session_cache_registry
        worker = app.state.composer_async_worker
        captured = []
        compose = app.state.composer_service.compose

        async def observe_compose(*positional, **keywords):
            result = await compose(*positional, **keywords)
            captured.append(result)
            return result

        # Class-level SQLAlchemy events include separate Landscape/auth-audit
        # engines, not only Sessions. Direct sqlite3 calls, if any, are excluded.
        meter = SQLMeter(Engine)
        with app.state.session_engine.connect() as connection:
            database_version = connection.scalar(text("SELECT version()" if args.backend == "postgres" else "SELECT sqlite_version()"))
        # Qualify actual statement counting independently of Composer. This is
        # a connection-local temporary table, never application persistence.
        with app.state.session_engine.connect() as connection:
            connection.execute(text("CREATE TEMPORARY TABLE profile_counter_control (value INTEGER)"))
            connection.commit()
            with connection.begin():
                meter.start()
                connection.execute(text("INSERT INTO profile_counter_control VALUES (:value)"), {"value": 1})
                connection.execute(text("UPDATE profile_counter_control SET value = :value"), {"value": 2})
                assert connection.scalar(text("SELECT value FROM profile_counter_control")) == 2
            control = meter.stop()
            assert control["statements"] == 3 and control["read"] == 1 and control["write"] == 2, control
            connection.execute(text("SELECT 1"))
            connection.execute(text("DROP TABLE profile_counter_control"))
            connection.commit()
            assert meter.stop()["statements"] == 3
        headers = {"Authorization": f"Bearer {token}"}
        http_requests = Counter()

        async def observe_http(request):
            http_requests[request.method] += 1

        args.output.parent.mkdir(parents=True, exist_ok=True)
        try:
            with patch.object(app.state.composer_service, "compose", observe_compose), args.output.open("x") as output:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://profile",
                    headers=headers,
                    event_hooks={"request": [observe_http]},
                ) as client:
                    if args.mode == "memory":
                        gc.collect()
                        # Retained-byte filtering uses only the allocating
                        # frame; deeper tracebacks add cost without improving
                        # this metric and can distort real preflight deadlines.
                        tracemalloc.start(1)
                    for index in range(1, args.turns + 1):
                        # Preconditions/postconditions are outside measured SQL
                        # and elapsed time. GET state is a separate UI operation;
                        # it is not silently counted as part of send-message.
                        before = await sessions.get_current_state(sid)
                        assert before is not None
                        before_hash = composition_content_hash(state_from_record(before))
                        history_rows_before = durable_message_count(app.state.session_engine, sid)
                        captured.clear()
                        transport.begin(index)
                        http_requests.clear()
                        if args.mode == "memory":
                            gc.collect()
                            memory_start, _ = tracemalloc.get_traced_memory()
                            filtered_start = filtered_retained_bytes()
                            # Snapshot sampling must not inflate the turn peak.
                            tracemalloc.reset_peak()
                        meter.start()
                        start_wall, start_cpu = time.perf_counter_ns(), time.process_time_ns()
                        settled = await submit_and_settle(
                            client,
                            app,
                            path=f"/api/sessions/{sid}/messages",
                            body=message_body(
                                f"Profile turn {index}: inspect the schema or apply the requested metadata change.", state_id=before.id
                            ),
                        )
                        elapsed_cpu = time.process_time_ns() - start_cpu
                        elapsed_wall = time.perf_counter_ns() - start_wall
                        sql = meter.stop()
                        peak = tracemalloc.get_traced_memory()[1] if args.mode == "memory" else None
                        # No adaptation of measured behavior to force success:
                        # refusal/failed jobs abort the run and must be diagnosed.
                        snapshot = settled.final.json()
                        assert snapshot["status"] == "completed", snapshot
                        assert len(captured) == 1
                        result = captured.pop()
                        assert transport.calls == 3
                        assert transport.request_categories == {
                            "composer_tool_request": 1,
                            "composer_final_response": 1,
                            "advisor_end_response": 1,
                        }, transport.request_categories
                        invocations = result.tool_invocations
                        expected_tool = "get_plugin_schema" if args.workload == "discovery" else "set_metadata"
                        tool_records = [item for item in invocations if item.tool_name == expected_tool]
                        assert len(tool_records) == 1
                        invocation = tool_records[0]
                        assert invocation.status is ComposerToolStatus.SUCCESS, invocation.status
                        tool_result = json.loads(invocation.result_canonical)
                        assert tool_result["success"] is True, tool_result
                        assert tool_result["validation"]["is_valid"] is True, tool_result["validation"]
                        if result.runtime_preflight is not None and not args.qualify_withhold:
                            assert result.runtime_preflight.is_valid, result.runtime_preflight
                        after = await sessions.get_current_state(sid)
                        assert after is not None
                        after_hash = composition_content_hash(state_from_record(after))
                        assert after_hash == composition_content_hash(result.state)
                        assert (after_hash == before_hash) is (args.workload == "discovery")
                        if args.workload == "mutation":
                            assert after.version > before.version
                        review = qualify_final_review(
                            app.state.session_engine,
                            sid,
                            after,
                            result,
                            snapshot["operation_id"],
                            withhold=args.qualify_withhold,
                            authored_mutation=args.workload == "mutation",
                        )
                        if args.qualify_withhold:
                            assert f"Turn {index}: the requested draft information is ready." not in result.message
                        observation = {
                            "history_rows_before": history_rows_before,
                            "history_rows_after": durable_message_count(app.state.session_engine, sid),
                            "version_before": before.version,
                            "version_after": after.version,
                            "semantic_tool_success": True,
                            "physical_discovery_hit": invocation.cache_hit,
                            "final_review": review,
                        }
                        # Release captured protocol/results before retained-GC
                        # sampling; per-turn peak was captured before assertions.
                        del result, invocations, tool_records, invocation, tool_result, settled, snapshot, before, after
                        memory = {}
                        if args.mode == "memory":
                            gc.collect()
                            retained, _ = tracemalloc.get_traced_memory()
                            filtered_retained = filtered_retained_bytes()
                            memory = {
                                "python_retained_bytes": retained,
                                "python_retained_delta_bytes": retained - memory_start,
                                "python_filtered_retained_bytes": filtered_retained,
                                "python_filtered_retained_delta_bytes": filtered_retained - filtered_start,
                                "python_turn_peak_above_start_bytes": peak - memory_start,
                            }
                        row = {
                            "boundary": "http-admission-durable-worker-terminal-status",
                            "backend": app.state.session_engine.dialect.name,
                            "driver": app.state.session_engine.dialect.driver,
                            "database_version": database_version,
                            "graph": args.graph,
                            "workload": args.workload,
                            "mode": args.mode,
                            "tracemalloc_traceback_frames": 1 if args.mode == "memory" else 0,
                            "cpu_scope": "instrumented_python_process_all_threads_excluding_database_server",
                            "sql_timing_scope": "driver_cursor_execute_excluding_fetch_and_transaction_commit",
                            "sample_kind": "withhold_qualification" if args.qualify_withhold else "healthy_turn",
                            "effective_settings": effective_settings,
                            "cache_enabled": not args.disable_cache,
                            "turn": index,
                            "cpu_ms": elapsed_cpu / 1e6 if args.mode == "cpu" and not args.qualify_withhold else None,
                            "wall_ms": elapsed_wall / 1e6 if args.mode == "cpu" and not args.qualify_withhold else None,
                            "local_wall_ms": (elapsed_wall - transport.wait_ns) / 1e6
                            if args.mode == "cpu" and not args.qualify_withhold
                            else None,
                            "provider_artificial_wait_ms": transport.wait_ns / 1e6,
                            "provider_calls": transport.calls,
                            "provider_request_bytes": list(transport.request_bytes),
                            "provider_request_categories": dict(transport.request_categories),
                            "sql": sql,
                            "http_requests": dict(http_requests),
                            "cache": asdict(registry.snapshot()),
                            "rss_sample_bytes": rss_bytes(),
                            "rss_scope": "process_without_tracemalloc" if args.mode == "cpu" else "process_including_tracemalloc_overhead",
                            "process_lifetime_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
                            **memory,
                            **observation,
                        }
                        output.write(json.dumps(row, sort_keys=True) + "\n")
                        output.flush()
                        del row
        finally:
            if tracemalloc.is_tracing():
                tracemalloc.stop()
            registry.begin_shutdown()
            await worker.stop()
            worker.assert_shutdown_complete()
            await shutdown_async_workers()
            await registry.finish_shutdown()
            app.state.auth_audit_recorder.close()
            app.state.session_engine.dispose()
            await app.state.operator_telemetry.shutdown()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("sqlite", "postgres"), default="sqlite")
    parser.add_argument("--graph", choices=("small", "chain80"), required=True)
    parser.add_argument("--workload", choices=("discovery", "mutation"), required=True)
    parser.add_argument("--mode", choices=("cpu", "memory"), required=True)
    parser.add_argument("--turns", type=int, default=20)
    parser.add_argument("--provider-delay", type=float, default=0)
    parser.add_argument("--composer-model", default="openrouter/deepseek/deepseek-v4.1-flash")
    parser.add_argument("--advisor-model", default="openrouter/z-ai/glm-5.3")
    parser.add_argument("--composition-rounds", type=int, default=10)
    parser.add_argument("--discovery-rounds", type=int, default=24)
    parser.add_argument("--turn-timeout", type=float, default=240)
    parser.add_argument("--rate-limit", type=int, default=20)
    parser.add_argument("--disable-cache", action="store_true", help="Use the existing cache-disabled control on the same source tree")
    parser.add_argument(
        "--qualify-withhold", action="store_true", help="One-turn negative admission control; emits no performance timing claims"
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.turns <= 100:
        parser.error("turns must be 1..100")
    if not math.isfinite(args.provider_delay) or not 0 <= args.provider_delay <= 10:
        parser.error("provider-delay must be finite and between 0 and 10 seconds")
    if args.qualify_withhold and (args.turns != 1 or args.mode != "cpu"):
        parser.error("qualify-withhold requires --turns 1 --mode cpu")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    metadata_path = args.output.with_suffix(".metadata.json")
    metadata = {
        "source_before": source_provenance(),
        "environment": measurement_environment(),
        "command": sys.argv,
        "conditions": {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
        "status": "running",
    }
    with metadata_path.open("x") as output:
        json.dump(metadata, output, indent=2)
    try:
        if args.backend == "postgres":
            # Distinct targets are required by actual app deployment admission.
            with postgres_test_target(driver="psycopg") as session_url, postgres_test_target(driver="psycopg") as landscape_url:
                asyncio.run(run(args, session_url, landscape_url))
        else:
            asyncio.run(run(args))
        metadata["status"] = "completed"
    finally:
        if metadata["status"] == "running":
            metadata["status"] = "failed"
        metadata["source_after"] = source_provenance()
        metadata["source_unchanged"] = metadata["source_before"] == metadata["source_after"]
        if not metadata["source_unchanged"] and metadata["status"] == "completed":
            metadata["status"] = "invalid_source_drift"
        metadata["environment_after"] = measurement_environment()
        metadata_path.write_text(json.dumps(metadata, indent=2))
    if metadata["status"] == "invalid_source_drift":
        raise RuntimeError("Source changed during profiling; measurements are not qualified")


if __name__ == "__main__":
    main()
