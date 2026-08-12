"""Shared harness for the protected ``live_provider`` state-engine lane.

The generic remote-provider lane (marker expression exactly ``live_provider``)
runs each PB-09 provider variant through the full production boundary:
``load_settings_from_yaml_string`` -> ``instantiate_plugins_from_config`` ->
``ExecutionGraph.from_plugin_instances`` ->
``assemble_and_validate_pipeline_config`` -> public ``Orchestrator.run`` with
the lane's live PostgreSQL Landscape database.

Landscape identity comes from the closed lane resource vocabulary
(``scripts/state_engine_assessment_lib/selectors.py`` ``COMMON_LIVE_RESOURCES``):
``ELSPETH_STATE_ENGINE_AWS_POSTGRES_URL`` names the live database and
``ELSPETH_STATE_ENGINE_AWS_DEPLOYMENT_PROVENANCE`` must assert the
``aws-single-leader-landscape`` deployment. The schema is provisioned by the
deployment — this harness never issues DDL against the shared database
(``create_tables=False``), matching
``tests/e2e/recovery/test_run_lifecycle_aws_live.py``.

Credential values are never hardcoded here: the pipeline YAML carries
``${ENV_NAME}`` references and the production ``_expand_env_vars`` pass in
``elspeth.core.config`` resolves them at settings load, so secrets flow
through the same expansion path operators use.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
import yaml
from sqlalchemy import func, select

from elspeth.contracts.config.runtime import RuntimeRateLimitConfig
from elspeth.core.config import load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import (
    artifacts_table,
    calls_table,
    node_states_table,
    scheduler_events_table,
    sink_effects_table,
    token_outcomes_table,
)
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.core.rate_limit.registry import RateLimitRegistry
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.orchestrator.preflight import assemble_and_validate_pipeline_config
from elspeth.plugins.infrastructure.runtime_factory import instantiate_plugins_from_config
from elspeth.plugins.transforms.llm.model_catalog import read_openrouter_catalog_snapshot_id

if TYPE_CHECKING:
    from scripts.state_engine_profile_reporter import RuntimeProfileReporter

LANDSCAPE_URL_ENV = "ELSPETH_STATE_ENGINE_AWS_POSTGRES_URL"
PROVENANCE_ENV = "ELSPETH_STATE_ENGINE_AWS_DEPLOYMENT_PROVENANCE"
EXPECTED_PROVENANCE = "aws-single-leader-landscape"


def require_env(*names: str) -> dict[str, str]:
    """Skip (never fail) when live-lane environment resources are absent.

    Credentials alone never authorize execution (``--run-live-provider``
    does); conversely, absent credentials make the node honestly
    unexecutable, which is a skip, not a pass and not a failure.
    """
    values: dict[str, str] = {}
    missing: list[str] = []
    for name in names:
        value = os.environ.get(name)
        if value is None or not value.strip():
            missing.append(name)
        else:
            values[name] = value
    if missing:
        pytest.skip(f"live-provider lane resources missing: {', '.join(sorted(missing))}")
    return values


def require_live_landscape() -> str:
    """Require the lane's live PostgreSQL Landscape identity; return its URL."""
    values = require_env(LANDSCAPE_URL_ENV, PROVENANCE_ENV)
    provenance = values[PROVENANCE_ENV]
    if provenance != EXPECTED_PROVENANCE:
        raise AssertionError(
            f"{PROVENANCE_ENV} must assert {EXPECTED_PROVENANCE!r} for the protected live-provider lane; got {provenance!r}"
        )
    return values[LANDSCAPE_URL_ENV]


def json_output_sink(path: Path) -> dict[str, Any]:
    """Standard lane output sink: JSONL under the test's tmp directory."""
    return {
        "plugin": "json",
        "on_write_failure": "discard",
        "options": {
            "path": str(path),
            "format": "jsonl",
            "mode": "write",
            "collision_policy": "auto_increment",
            "schema": {"mode": "observed"},
        },
    }


def jsonl_input_source(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    on_success: str,
    guaranteed_fields: list[str] | None = None,
) -> dict[str, Any]:
    """Materialize ``rows`` as a JSONL file and return the json source node.

    ``guaranteed_fields`` switches the source to a fixed schema (``name: str``
    entries) so a downstream consumer's ``required_input_fields`` declaration
    passes DAG contract validation.
    """
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    schema: dict[str, Any]
    if guaranteed_fields is None:
        schema = {"mode": "observed"}
    else:
        schema = {"mode": "fixed", "fields": [f"{name}: str" for name in guaranteed_fields]}
    return {
        "plugin": "json",
        "on_success": on_success,
        "options": {
            "path": str(path),
            "format": "jsonl",
            "schema": schema,
            "on_validation_failure": "discard",
        },
    }


def read_output_rows(path: Path) -> list[dict[str, Any]]:
    """Parse the JSONL rows the lane's output sink wrote."""
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parsed = json.loads(line)
        assert type(parsed) is dict
        rows.append(parsed)
    return rows


@dataclass(frozen=True, slots=True)
class LiveRunEvidence:
    """Landscape facts recorded for one live pipeline run."""

    run_id: str
    status: Any
    node_state_count: int
    scheduler_event_count: int
    token_outcome_count: int
    call_types: tuple[str, ...]
    sink_effect_states: tuple[str, ...]
    artifact_count: int


def run_live_pipeline(
    request: pytest.FixtureRequest,
    pipeline: dict[str, Any],
    tmp_path: Path,
) -> LiveRunEvidence:
    """Run one pipeline through the public production boundary and audit it.

    Injects the lane's Landscape URL (as a ``${...}`` reference resolved by
    the production env-expansion pass) and a tmp-scoped payload store, runs
    the real ``Orchestrator``, and returns the run's Landscape evidence.
    """
    require_live_landscape()
    store = FilesystemPayloadStore(tmp_path / "payloads")
    pipeline = dict(pipeline)
    pipeline["landscape"] = {"backend": "postgresql", "url": f"${{{LANDSCAPE_URL_ENV}}}"}
    pipeline["payload_store"] = {"backend": "filesystem", "base_path": str(store.base_path)}

    # expand_env_vars=True is the documented trusted in-process opt-in: this
    # harness authors the YAML itself, and ``${NAME}`` references are how the
    # lane's credential values reach the config without ever appearing in
    # test code or the settings text.
    settings = load_settings_from_yaml_string(yaml.safe_dump(pipeline, sort_keys=False), expand_env_vars=True)
    bundle = instantiate_plugins_from_config(settings)
    graph = ExecutionGraph.from_plugin_instances(
        sources=bundle.sources,
        source_settings_map=bundle.source_settings_map,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        gates=list(settings.gates),
        coalesce_settings=list(settings.coalesce),
        queues=settings.queues,
    )
    config = assemble_and_validate_pipeline_config(
        sources=bundle.sources,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        settings=settings,
        graph=graph,
        sink_effect_modes=bundle.sink_effect_modes,
    )
    catalog_sha256, catalog_source = read_openrouter_catalog_snapshot_id()

    db = LandscapeDB.from_url(settings.landscape.url, create_tables=False)
    rate_limits = RateLimitRegistry(RuntimeRateLimitConfig.from_settings(settings.rate_limit))
    try:
        if request.config.pluginmanager.hasplugin("scripts.state_engine_profile_reporter"):
            reporter = cast("RuntimeProfileReporter", request.getfixturevalue("state_engine_profile"))
            with db.read_only_connection() as connection:
                reporter.observe_postgresql(connection, deployment=EXPECTED_PROVENANCE)

        result = Orchestrator(db, rate_limit_registry=rate_limits).run(
            config,
            graph=graph,
            settings=settings,
            payload_store=store,
            openrouter_catalog_sha256=catalog_sha256,
            openrouter_catalog_source=catalog_source,
        )

        with db.engine.connect() as connection:
            run_states = select(node_states_table.c.state_id).where(node_states_table.c.run_id == result.run_id)
            node_state_count = connection.scalar(
                select(func.count()).select_from(node_states_table).where(node_states_table.c.run_id == result.run_id)
            )
            scheduler_event_count = connection.scalar(
                select(func.count()).select_from(scheduler_events_table).where(scheduler_events_table.c.run_id == result.run_id)
            )
            token_outcome_count = connection.scalar(
                select(func.count()).select_from(token_outcomes_table).where(token_outcomes_table.c.run_id == result.run_id)
            )
            call_types = tuple(connection.execute(select(calls_table.c.call_type).where(calls_table.c.state_id.in_(run_states))).scalars())
            sink_effect_states = tuple(
                connection.execute(select(sink_effects_table.c.state).where(sink_effects_table.c.run_id == result.run_id)).scalars()
            )
            artifact_count = connection.scalar(
                select(func.count()).select_from(artifacts_table).where(artifacts_table.c.run_id == result.run_id)
            )

        return LiveRunEvidence(
            run_id=result.run_id,
            status=result.status,
            node_state_count=cast(int, node_state_count),
            scheduler_event_count=cast(int, scheduler_event_count),
            token_outcome_count=cast(int, token_outcome_count),
            call_types=call_types,
            sink_effect_states=sink_effect_states,
            artifact_count=cast(int, artifact_count),
        )
    finally:
        rate_limits.close()
        db.close()
