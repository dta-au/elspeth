"""Build-raised edge-contract failures must disclose actionable repair advice.

elspeth-aed3b69cf0: ``EdgeContractError`` is raised during graph BUILD
(``builder.build_execution_graph`` -> ``validate_edge_compatibility``), so a
type-mismatch edge failure surfaces in composer phase 1
(``validate_graph_structure``) — never in phase 3
(``validate_schema_compatibility``), whose dedicated handler requires a
successfully built graph. Phase 1 must route the failure through the same
edge-contract formatter so the composer receives the consumer-side patch
suggestion instead of ``suggestion=None``.

Every test here drives a REAL graph build through the production validation
entry point. A mocked graph cannot detect that the real path never reaches
phase 3 — that mock is exactly why this gap survived.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretBytes
from pydantic import ValidationError as PydanticValidationError

from elspeth.web.composer import yaml_generator as production_yaml_generator
from elspeth.web.composer.state import (
    CompositionState,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
)
from elspeth.web.config import WebSettings
from elspeth.web.execution import validation as validation_module
from elspeth.web.execution.schemas import CHECK_OUTCOME_SKIPPED_AFTER_FAILURE
from elspeth.web.execution.validation import validate_pipeline_for_trained_operator

_SESSION_ID = "test-session"


def _web_settings(data_dir: Path) -> WebSettings:
    return WebSettings(
        data_dir=data_dir,
        composer_max_composition_turns=10,
        composer_max_discovery_turns=5,
        composer_timeout_seconds=30.0,
        composer_rate_limit_per_minute=60,
        shareable_link_signing_key=SecretBytes(b"\x00" * 32),
    )


def _coerce_state(declared_score_type: str) -> CompositionState:
    """CSV source emitting ``score: str`` into a type_coerce converting to float.

    ``declared_score_type`` is the type the type_coerce node's own ``schema:``
    block declares for ``score`` — the pre-coercion input contract. ``float``
    contradicts the producer (and the strict runtime gate); ``str`` and
    ``any`` are honest declarations.
    """
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="coerce_score",
            options={
                "path": f"blobs/{_SESSION_ID}/input.csv",
                "schema": {"mode": "fixed", "fields": ["id: str", "score: str"]},
            },
            on_validation_failure="discard",
        ),
        nodes=(
            NodeSpec(
                id="coerce_score",
                node_type="transform",
                plugin="type_coerce",
                input="coerce_score",
                on_success="results",
                on_error="discard",
                options={
                    "schema": {"mode": "fixed", "fields": ["id: str", f"score: {declared_score_type}"]},
                    "conversions": [{"field": "score", "to": "float"}],
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(
            OutputSpec(
                name="results",
                plugin="csv",
                options={"path": "outputs/results.csv", "schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


def _validate_real(declared_score_type: str, data_dir: Path) -> tuple[Any, Any]:
    """Run the full production validation path, capturing the real plugin bundle."""
    source_dir = data_dir / "blobs" / _SESSION_ID
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "input.csv").write_text("id,score\na,12.5\nb,3.0\n")

    captured: list[Any] = []
    real_instantiate = validation_module.instantiate_runtime_plugins

    def capture_bundle(runtime_settings: Any, *, plugin_snapshot: Any) -> Any:
        bundle = real_instantiate(runtime_settings, plugin_snapshot=plugin_snapshot)
        captured.append(bundle)
        return bundle

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(validation_module, "instantiate_runtime_plugins", capture_bundle)
        result = validate_pipeline_for_trained_operator(
            _coerce_state(declared_score_type),
            _web_settings(data_dir),
            production_yaml_generator,
            session_id=_SESSION_ID,
        )
    finally:
        monkeypatch.undo()
    assert len(captured) == 1
    return result, captured[0]


@pytest.fixture(scope="module")
def float_declaration_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[Any, Any]:
    """One real validation run of the contradictory ``score: float`` declaration."""
    return _validate_real("float", tmp_path_factory.mktemp("g02_float"))


def test_build_raised_edge_failure_discloses_consumer_patch_suggestion(
    float_declaration_run: tuple[Any, Any],
) -> None:
    """The composer must receive the repair advice, not ``suggestion=None``.

    This is the load-bearing disclosure assertion: the battery's g02 session
    received the bare ``str(exc)`` with no suggestion and could not
    self-repair.
    """
    result, _bundle = float_declaration_run

    assert result.is_valid is False
    assert result.errors, "expected a validation error for the contradictory declaration"
    error = result.errors[0]
    assert "consumer requires 'float', producer emits 'str'" in error.message
    assert error.suggestion is not None
    assert "Change the declared field type(s) to match what the producer emits" in error.suggestion
    # The consumer-side patch call survives even though the graph never built
    # (patch-target resolution degrades to the DAG node id by design).
    assert "patch_node_options(node_id='transform_coerce_score_" in error.suggestion


def test_build_raised_edge_failure_stays_owned_by_graph_structure_check(
    float_declaration_run: tuple[Any, Any],
) -> None:
    """Phase-ownership pin: edge validation lives in the graph BUILD.

    Drivers key off the failed-check name. Moving edge validation out of the
    builder (so it fails under ``schema_compatibility`` instead) must break
    this test deliberately rather than silently re-orphaning the phase-1
    handler.
    """
    result, _bundle = float_declaration_run

    hard_failures = [check for check in result.checks if not check.passed and check.outcome_code is None]
    assert [check.name for check in hard_failures] == ["graph_structure"]
    schema_check = next(check for check in result.checks if check.name == "schema_compatibility")
    assert schema_check.outcome_code == CHECK_OUTCOME_SKIPPED_AFTER_FAILURE


def test_float_declaration_is_runtime_unsatisfiable_under_strict_gate(
    float_declaration_run: tuple[Any, Any],
) -> None:
    """Pin WHY the build-time rejection is correct and load-bearing.

    The declared ``score: float`` input contract rejects the producer's string
    rows at the executor's strict gate
    (``transform.input_schema.model_validate(..., strict=True)``), so letting
    the edge through would fail 100% of rows at runtime. Anyone tempted to
    "fix" elspeth-aed3b69cf0 by weakening ``check_compatibility`` instead of
    disclosing the repair pays this cost explicitly.
    """
    _result, bundle = float_declaration_run

    transform = bundle.transforms[0].plugin
    with pytest.raises(PydanticValidationError):
        transform.input_schema.model_validate({"id": "a", "score": "12.5"}, strict=True)


@pytest.mark.parametrize(
    ("declared_score_type", "expect_valid"),
    [("float", False), ("str", True), ("any", True)],
)
def test_declared_input_type_discriminates_edge_compatibility(
    declared_score_type: str,
    expect_valid: bool,
    tmp_path: Path,
) -> None:
    """Three-case discrimination: only the contradictory declaration is rejected.

    type_coerce is NOT unbuildable downstream of a str-typed CSV source — the
    honest declarations build. Only declaring the POST-coercion type on the
    node's own input schema fails, because that contract is unsatisfiable.
    """
    result, _bundle = _validate_real(declared_score_type, tmp_path)

    assert result.is_valid is expect_valid
    if not expect_valid:
        failed = next(check for check in result.checks if not check.passed and check.outcome_code is None)
        assert failed.name == "graph_structure"


def test_honest_str_declaration_still_derives_float_output_contract(tmp_path: Path) -> None:
    """The coerced type belongs to the OUTPUT schema and is derived automatically.

    One ``schema:`` block feeds both sides asymmetrically: the declared block
    verbatim is the pre-coercion input; the output overwrites each converted
    field's type with the conversion target. Authors never declare the
    post-coercion type.
    """
    result, bundle = _validate_real("str", tmp_path)

    assert result.is_valid is True
    transform = bundle.transforms[0].plugin
    assert transform.input_schema.model_fields["score"].annotation is str
    assert transform.output_schema.model_fields["score"].annotation is float
