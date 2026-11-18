"""Prompt identity is committed at run start before a provider can act."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

import tests.integration.pipeline.test_composer_runtime_agreement as agreement
from elspeth.contracts import RunStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.run_prompt_input import RunPromptInputEvidence
from elspeth.contracts.run_start import RunStartPermitBinding
from elspeth.core.landscape.schema import run_prompt_inputs_table, runs_table
from elspeth.engine.orchestrator.core import Orchestrator
from elspeth.engine.orchestrator.types import PipelineConfig
from tests.fixtures.base_classes import as_sink, as_source, as_transform
from tests.fixtures.landscape import make_factory, make_landscape_db
from tests.fixtures.pipeline import build_production_graph
from tests.fixtures.plugins import CollectSink, ListSource


def _evidence() -> RunPromptInputEvidence:
    return RunPromptInputEvidence("classify", "prompt_template", str(uuid4()), "a" * 64, "b" * 64, None)


def _pipeline(provider: Mock) -> PipelineConfig:
    source = ListSource([{"text": "hello"}])
    transform = agreement.LLMTransform(
        {
            "provider": "openrouter",
            "model": "openai/gpt-4o",
            "api_key": "test-key",
            "prompt_template": "Classify {{ row.text }}",
            "schema": {"mode": "observed"},
            "required_input_fields": [],
        }
    )
    transform.on_success = "default"
    # Specced from the bound factory, so the engine's call must match its signature.
    transform._create_provider = Mock(spec=transform._create_provider, return_value=provider)
    return PipelineConfig(
        sources={"primary": as_source(source)},
        transforms=[as_transform(transform)],
        sinks={"default": as_sink(CollectSink())},
    )


def test_prompt_input_exists_with_run_fk_before_first_provider_call(payload_store: object) -> None:
    with make_landscape_db() as db:
        admitted = _evidence()
        calls: list[str] = []
        provider = Mock(spec=agreement.LLMProvider)

        def call_provider(*_args: object, **_kwargs: object) -> agreement.LLMQueryResult:
            calls.append("provider")
            with db.read_only_connection() as conn:
                row = conn.execute(select(run_prompt_inputs_table)).one()
                run = conn.execute(select(runs_table).where(runs_table.c.run_id == row.run_id)).one()
            assert run.status == RunStatus.RUNNING.value
            assert row.content_sha256 == admitted.content_sha256
            return agreement.LLMQueryResult(
                content="accepted",
                usage=agreement.TokenUsage.known(4, 1),
                model="openai/gpt-4o",
                finish_reason=agreement.FinishReason.STOP,
            )

        provider.execute_query.side_effect = call_provider
        config = _pipeline(provider)
        result = Orchestrator(db).run(
            config, graph=build_production_graph(config), payload_store=payload_store, run_prompt_inputs=(admitted,)
        )
        assert result.status is RunStatus.COMPLETED
        assert calls == ["provider"]
        with db.read_only_connection() as conn:
            foreign_keys = db.engine.dialect.name == "sqlite" and conn.exec_driver_sql("PRAGMA foreign_key_list(run_prompt_inputs)").all()
        assert any(key[2] == "runs" for key in foreign_keys)


def test_prompt_input_insert_failure_stops_before_provider_and_rolls_back(payload_store: object) -> None:
    with make_landscape_db() as db:
        provider = Mock(spec=agreement.LLMProvider)
        config = _pipeline(provider)

        def fail_insert(_conn: object, _cursor: object, statement: str, _parameters: object, _context: object, _many: bool) -> None:
            if statement.startswith("INSERT INTO run_prompt_inputs"):
                raise IntegrityError(statement, {}, RuntimeError("prompt evidence unavailable"))

        event.listen(db.engine, "before_cursor_execute", fail_insert)
        try:
            with pytest.raises(Exception, match="database rejected audit write"):
                Orchestrator(db).run(
                    config, graph=build_production_graph(config), payload_store=payload_store, run_prompt_inputs=(_evidence(),)
                )
        finally:
            event.remove(db.engine, "before_cursor_execute", fail_insert)
        provider.execute_query.assert_not_called()
        with db.read_only_connection() as conn:
            assert conn.execute(select(runs_table)).all() == []


def test_permit_retry_requires_identical_prompt_input_set() -> None:
    with make_landscape_db() as db:
        repo = make_factory(db).run_lifecycle
        binding = RunStartPermitBinding("prompt-run", "permit", 1, "c" * 64)
        evidence = _evidence()
        first = repo.begin_run({}, "v1", run_id=binding.run_id, run_start_permit=binding, run_prompt_inputs=(evidence,))
        assert (
            repo.begin_run({}, "v1", run_id=binding.run_id, run_start_permit=binding, run_prompt_inputs=(evidence,)).run_id == first.run_id
        )
        with pytest.raises(AuditIntegrityError, match="prompt input evidence"):
            repo.begin_run(
                {}, "v1", run_id=binding.run_id, run_start_permit=binding, run_prompt_inputs=(replace(evidence, content_sha256="d" * 64),)
            )
        with pytest.raises(AuditIntegrityError, match="prompt input evidence"):
            repo.begin_run({}, "v1", run_id=binding.run_id, run_start_permit=binding)
