"""Bounded live Composer profiling; requires all-extras/dev dependencies.

Run as python -m scripts.profile_composer_live from the repository root.
Root must coordinate the shared account before passing --execute-live. This
flag is an execution interlock, not a claim that the account is exclusive.
Never delete/reset the reservation ledger between runs in the same activity.
No prompt, credential, response text, or provider error body is exported.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import time
from collections import Counter
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx
import litellm
from sqlalchemy import Engine, select, text

from scripts import profile_composer as shared

MODELS = ("openrouter/deepseek/deepseek-v4.1-flash", "openrouter/z-ai/glm-5.3")
CAP = Decimal("10.00")
MAX_ACTIVITY_CALLS = 8
HEADROOM = Decimal("1.00")


class BudgetRefused(RuntimeError):
    """A fail-closed local budget refusal; message carries no provider data."""


def money(value):
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise BudgetRefused("invalid monetary value")
    return amount


async def public_prices():
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get("https://openrouter.ai/api/v1/models")
        response.raise_for_status()
        models = {item["id"]: item for item in response.json()["data"]}
    prices = {}
    for name in MODELS:
        item = models[name.removeprefix("openrouter/")]
        context, output = item["context_length"], item["top_provider"]["max_completion_tokens"]
        if type(context) is not int or type(output) is not int or min(context, output) <= 0:
            raise BudgetRefused("missing finite model limits")
        prompt, completion = money(item["pricing"]["prompt"]), money(item["pricing"]["completion"])
        # Deliberately over-reserve even if cache prices replace input pricing.
        input_ceiling = prompt + money(item["pricing"].get("input_cache_read", 0)) + money(item["pricing"].get("input_cache_write", 0))
        if any(money(item["pricing"].get(key, 0)) > prompt for key in ("input_cache_read", "input_cache_write")):
            raise BudgetRefused("cache input price exceeds reservation")
        # Non-token charges are not covered by the reservation formula.
        if any(
            money(value)
            for key, value in item["pricing"].items()
            if key not in {"prompt", "completion", "input_cache_read", "input_cache_write"}
        ):
            raise BudgetRefused("unsupported pricing component")
        prices[name] = {
            "context": context,
            "output": output,
            "prompt": str(prompt),
            "completion": str(completion),
            "reservation_input_rate": str(input_ceiling),
            "reservation_usd": str(context * input_ceiling + output * completion),
        }
    return prices


class Ledger:
    """Append-only reservation journal, locked and synced before each SDK call."""

    def __init__(self, path):
        self.path = path

    def reserve(self, model, amount):
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+") as file:
            fcntl.flock(file, fcntl.LOCK_EX)
            if os.fstat(file.fileno()).st_mode & 0o077:
                raise BudgetRefused("ledger permissions must be 0600")
            rows = [json.loads(line) for line in file]
            total = sum((money(row["reserved_usd"]) for row in rows), Decimal(0))
            if len(rows) >= MAX_ACTIVITY_CALLS or total + amount > CAP:
                raise BudgetRefused("activity reservation ceiling reached")
            row = {"sequence": len(rows) + 1, "model": model, "reserved_usd": str(amount), "timestamp": time.time()}
            file.write(json.dumps(row) + "\n")
            file.flush()
            os.fsync(file.fileno())
            return row["sequence"]


class LiveTransport:
    def __init__(self, original, prices, ledger):
        self.original, self.prices, self.ledger = original, prices, ledger
        self.records = []
        self.budget_checks = []

    async def reserve_with_credit_check(self, model, reserved, key):
        record = {"model": model}
        self.budget_checks.append(record)
        start = time.perf_counter_ns()
        try:
            if not isinstance(key, str) or not key:
                raise BudgetRefused("missing runtime key for fresh balance check")
            async with httpx.AsyncClient(timeout=20) as client:
                balance = await client.get("https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {key}"})
                balance.raise_for_status()
                data = balance.json()["data"]
                remaining = money(data["total_credits"]) - money(data["total_usage"])
            record["credit_http_ms"] = (time.perf_counter_ns() - start) / 1e6
            if remaining < reserved + HEADROOM:
                raise BudgetRefused("insufficient current account headroom")
            ledger_start = time.perf_counter_ns()
            try:
                sequence = self.ledger.reserve(model, reserved)
            finally:
                record["ledger_ms"] = (time.perf_counter_ns() - ledger_start) / 1e6
            record["outcome"] = "reserved"
            return sequence
        except BaseException as exc:
            record["outcome"] = type(exc).__name__
            raise
        finally:
            record["total_ms"] = (time.perf_counter_ns() - start) / 1e6

    async def __call__(self, **kwargs):
        model = kwargs["model"]
        if model not in self.prices or kwargs.get("stream"):
            raise BudgetRefused("unsupported request")
        if kwargs.get("fallbacks") or kwargs.get("context_window_fallback_dict"):
            raise BudgetRefused("fallback requests are not budgeted")
        price = self.prices[model]
        reserved = money(price["reservation_usd"])
        # Runtime-only access; never read the ignored secret configuration file.
        key = kwargs.get("api_key") or os.environ.get("OPENROUTER_API_KEY")
        sequence = await self.reserve_with_credit_check(model, reserved, key)
        kwargs["num_retries"] = kwargs["max_retries"] = 0
        # OpenRouter max_price is USD per million tokens. Pin a price ceiling,
        # refuse unsupported parameters, and disable provider fallback retries.
        extra = dict(kwargs.get("extra_body") or {})
        extra["provider"] = {
            "allow_fallbacks": False,
            "require_parameters": True,
            "max_price": {"prompt": float(money(price["prompt"]) * 1_000_000), "completion": float(money(price["completion"]) * 1_000_000)},
        }
        kwargs["extra_body"] = extra
        schema = kwargs.get("response_format", {}).get("json_schema", {}).get("name")
        category = schema if schema in {"advisor_end_response", "advisor_checkpoint_response"} else "composer"
        record = {
            "sequence": sequence,
            "model": model,
            "category": category,
            "reserved_usd": str(reserved),
            "request_bytes": len(json.dumps({"messages": kwargs["messages"], "tools": kwargs.get("tools")}, default=str).encode()),
        }
        self.records.append(record)
        start = time.perf_counter_ns()
        try:
            result = await self.original(**kwargs)
            usage = result.usage.model_dump() if result.usage is not None else {}
            record["tokens"] = {
                key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens") if type(usage.get(key)) is int
            }
            # Only upstream-reported cost, never LiteLLM's estimated cost.
            cost = usage.get("cost")
            try:
                record["provider_reported_cost_usd"] = str(money(cost)) if cost is not None else None
            except (BudgetRefused, ArithmeticError, ValueError):
                record["provider_reported_cost_usd"] = None
            record["outcome"] = "returned"
            return result
        except BaseException as exc:
            record["outcome"] = type(exc).__name__
            raise
        finally:
            record["start_ns"], record["end_ns"] = start, time.perf_counter_ns()


def review_evidence(engine, sid, after, result, operation_id):
    identity = result.graph_review_identity
    with engine.connect() as connection:
        rows = (
            connection.execute(
                select(shared.chat_messages_table.c.tool_calls).where(
                    shared.chat_messages_table.c.session_id == str(sid), shared.chat_messages_table.c.role == "audit"
                )
            )
            .scalars()
            .all()
        )
    envelopes = [envelope for row in rows if row for envelope in row]
    counts = Counter(envelope.get("_kind", "unknown") for envelope in envelopes)
    facts = [
        shared.AdvisorGraphReviewFact.from_envelope(envelope)
        for envelope in envelopes
        if envelope.get("_kind") == shared.ADVISOR_GRAPH_REVIEW_AUDIT_KIND
    ]
    matching = [
        fact
        for fact in facts
        if identity is not None
        and fact.identity == identity
        and fact.state_id == after.id
        and str(fact.terminal_operation_id) == operation_id
        and fact.content_hash == shared.composition_content_hash(result.state)
    ]
    passes = [
        shared.AdvisorCheckpointPassRecord.from_dict(envelope["pass"])
        for envelope in envelopes
        if envelope.get("_kind") == shared.ADVISOR_CHECKPOINT_PASS_AUDIT_KIND
    ]
    terminal = [item for item in passes if identity is not None and item.phase == "end" and item.publication_binding == identity.binding]
    admitted = bool(
        matching
        and terminal
        and matching[-1].action == "passed"
        and terminal[-1].publication_verdict == "ADMIT"
        and identity.source == "model"
        and identity.for_graph == shared.completion_gate_fingerprint(result.state)
        and isinstance(result.advisor_gate_decision, shared.AdvisorGatePassed)
    )
    return {
        "matching_admitted_review": admitted,
        "audit_kind_counts_cumulative": dict(counts),
        "publication_verdict": terminal[-1].publication_verdict if terminal else None,
        "provider_attempts": terminal[-1].provider_attempts if terminal else None,
    }


async def run(args):
    prices = await public_prices()
    args.run_dir.mkdir(parents=True, exist_ok=False)
    for name in ("blobs", "outputs", "runs", "payloads"):
        (args.run_dir / name).mkdir()
    # WebSettings is an owned BaseModel, not an environment settings loader.
    # Pass the runtime values through the fixture's normal WebSettings validation.
    settings = shared._make_settings(
        args.run_dir,
        shareable_link_signing_key=os.environ["ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY"],
        secret_key=os.environ["ELSPETH_WEB__SECRET_KEY"],
        composer_model=MODELS[0],
        composer_advisor_model=MODELS[1],
        composer_max_composition_turns=10,
        composer_max_discovery_turns=24,
        composer_timeout_seconds=240,
        composer_rate_limit_per_minute=20,
        composer_boot_probe_enabled=False,
        payload_store_path=args.run_dir / "payloads",
        plugin_allowlist=("transform:type_coerce", "transform:value_transform"),
    )
    transport = LiveTransport(litellm.acompletion, prices, Ledger(args.budget_ledger))
    metadata = {
        "source": shared.source_provenance(),
        "environment": shared.measurement_environment(),
        "live_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "prices": prices,
        "prompt_guard_mode": settings.composer_prompt_guard_mode,
        "account_exclusive": False,
        "reservation_cap_usd": str(CAP),
        "max_activity_calls": MAX_ACTIVITY_CALLS,
        "tracemalloc": False,
        "command": [
            "python",
            "-m",
            "scripts.profile_composer_live",
            "--execute-live",
            "--turns",
            str(args.turns),
            "--run-dir",
            str(args.run_dir),
            "--output",
            str(args.output),
            "--budget-ledger",
            str(args.budget_ledger),
        ],
        "boundary": "http-admission-durable-worker-terminal-status",
        "effective_settings": shared.safe_settings(settings),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as output, patch.object(litellm, "acompletion", transport):
        output.write(json.dumps({"metadata": metadata}) + "\n")
        output.flush()
        app = shared.create_app(settings, process_watchdog_factory=shared.OwnedTestProcessWatchdog)
        registry, worker = app.state.composer_session_cache_registry, app.state.composer_async_worker
        try:
            app.state.auth_audit_recorder.start()
            sid, token = await shared.seed(app, "small")
            sessions = app.state.session_service
            meter = shared.SQLMeter(Engine)
            with app.state.session_engine.connect() as connection:
                db_version = connection.scalar(text("SELECT sqlite_version()"))
            captured = []
            original = app.state.composer_service.compose

            async def observe(*positional, **keywords):
                result = await original(*positional, **keywords)
                captured.append(result)
                return result

            requests = Counter()

            async def http_observer(request):
                requests[request.method] += 1

            with patch.object(app.state.composer_service, "compose", observe):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://profile",
                    headers={"Authorization": f"Bearer {token}"},
                    event_hooks={"request": [http_observer]},
                ) as client:
                    for turn in range(1, args.turns + 1):
                        before = await sessions.get_current_state(sid)
                        history = shared.durable_message_count(app.state.session_engine, sid)
                        captured.clear()
                        requests.clear()
                        offset = len(transport.records)
                        budget_offset = len(transport.budget_checks)
                        meter.start()
                        wall, cpu = time.perf_counter_ns(), time.process_time_ns()
                        failure, snapshot = None, {}
                        try:
                            settled = await shared.submit_and_settle(
                                client,
                                app,
                                path=f"/api/sessions/{sid}/messages",
                                body=shared.message_body(
                                    "Please call get_plugin_schema for the csv source plugin and summarize its schema. Do not mutate the pipeline.",
                                    state_id=before.id,
                                ),
                            )
                            snapshot = settled.final.json()
                        except Exception as exc:
                            failure = type(exc).__name__
                        cpu_ms, wall_ms = (time.process_time_ns() - cpu) / 1e6, (time.perf_counter_ns() - wall) / 1e6
                        sql = meter.stop()
                        after = await sessions.get_current_state(sid)
                        evidence, semantic = {}, False
                        unchanged = after is not None and shared.composition_content_hash(
                            shared.state_from_record(after)
                        ) == shared.composition_content_hash(shared.state_from_record(before))
                        if len(captured) == 1 and after is not None:
                            result = captured[0]
                            semantic = any(
                                item.tool_name == "get_plugin_schema"
                                and item.status is shared.ComposerToolStatus.SUCCESS
                                and json.loads(item.result_canonical)["success"] is True
                                for item in result.tool_invocations
                            )
                            evidence = review_evidence(app.state.session_engine, sid, after, result, snapshot.get("operation_id"))
                            evidence["runtime_valid"] = result.runtime_preflight is not None and result.runtime_preflight.is_valid
                        healthy = bool(
                            snapshot.get("status") == "completed"
                            and semantic
                            and unchanged
                            and evidence.get("matching_admitted_review")
                            and evidence.get("runtime_valid")
                        )
                        calls = transport.records[offset:]
                        intervals = sorted((item["start_ns"], item["end_ns"]) for item in calls)
                        union, end = 0, 0
                        for start, stop in intervals:
                            union += max(0, stop - max(start, end))
                            end = max(end, stop)
                        row = {
                            "turn": turn,
                            "healthy": healthy,
                            "operation_status": snapshot.get("status"),
                            "failure_class": failure,
                            "semantic_schema_success": semantic,
                            "state_unchanged": unchanged,
                            "review": evidence,
                            "cpu_ms": cpu_ms,
                            "wall_ms": wall_ms,
                            "sdk_wait_union_ms": union / 1e6,
                            "wall_minus_sdk_ms": wall_ms - union / 1e6,
                            "wall_minus_sdk_scope": "includes_budget_guard_network_and_instrumentation_not_pure_local_compute",
                            "budget_checks": transport.budget_checks[budget_offset:],
                            "provider_calls": calls,
                            "sql": sql,
                            "http_requests": dict(requests),
                            "history_rows_before": history,
                            "history_rows_after": shared.durable_message_count(app.state.session_engine, sid),
                            "cache": asdict(registry.snapshot()),
                            "rss_bytes": shared.rss_bytes(),
                            "sqlite_version": db_version,
                        }
                        output.write(json.dumps(row) + "\n")
                        output.flush()
                        if not healthy:
                            return 1
            return 0
        finally:
            registry.begin_shutdown()
            await worker.stop()
            worker.assert_shutdown_complete()
            await shared.shutdown_async_workers()
            await registry.finish_shutdown()
            app.state.auth_audit_recorder.close()
            app.state.session_engine.dispose()
            await app.state.operator_telemetry.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-live", action="store_true", help="Root has coordinated this run against the shared activity budget")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget-ledger", type=Path, required=True)
    parser.add_argument("--turns", type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    if not args.execute_live:
        parser.error("live execution requires explicit coordinated --execute-live")
    if args.output.exists() or args.run_dir.exists():
        parser.error("run directory and output must be new")
    # Production audit remains in the synthetic run DB. Console logging and
    # uncaught SDK error bodies are suppressed; export exception classes only.
    logging.disable(logging.CRITICAL)
    with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
        source_before = shared.source_provenance()
        environment_before = shared.measurement_environment()
        try:
            code = asyncio.run(run(args))
        except Exception as exc:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("a") as output:
                output.write(json.dumps({"failure_class": type(exc).__name__}) + "\n")
            code = 1
        finally:
            source_after = shared.source_provenance()
            if source_before != source_after:
                code = 1
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("a") as output:
                output.write(
                    json.dumps(
                        {
                            "final_metadata": {
                                "source_before": source_before,
                                "source_after": source_after,
                                "source_unchanged": source_before == source_after,
                                "invalid_reason": "source_drift" if source_before != source_after else None,
                                "environment_before": environment_before,
                                "environment_after": shared.measurement_environment(),
                            }
                        }
                    )
                    + "\n"
                )
    raise SystemExit(code)


if __name__ == "__main__":
    main()
