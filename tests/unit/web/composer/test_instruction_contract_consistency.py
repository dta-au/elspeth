"""Delivered authoring instructions agree with the source and review contracts."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from elspeth.contracts.composer_interpretation import InterpretationKind
from elspeth.contracts.schema import SchemaConfig
from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config
from elspeth.plugins.sources.csv_source import CSVSource
from elspeth.web.composer.prompts import build_system_prompt
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata
from elspeth.web.composer.tool_error_payloads import arg_error_payload
from elspeth.web.composer.tools.sessions import _assert_affected_component
from elspeth.web.interpretation_state import vague_term_wiring_count
from elspeth.web.sessions.pending_interpretation import _patch_structured_interpretation_prompt
from elspeth.web.sessions.protocol import InterpretationPlaceholderConsumedError
from tests.fixtures.factories import make_context


def test_delivered_types_separate_source_ingestion_from_consumer_validation() -> None:
    delivered = " ".join(build_system_prompt(None).split())

    assert "Source plugins may coerce declared fields only where their live contract permits it" in delivered
    assert "CSV fixed/flexible schemas coerce declared fields at ingestion; observed CSV cells remain strings" in delivered
    assert "Source validation failures use `on_validation_failure`" in delivered
    assert "A downstream input type declaration validates arriving values without converting them" in delivered
    assert "A type you declare is checked on every row and never converted to fit" not in delivered


def test_delivered_new_structured_requirements_do_not_offer_flat_token_wiring() -> None:
    delivered = " ".join(build_system_prompt(None).split())
    rule = delivered.split("**HARD RULE", 1)[1].split("These checks stack.", 1)[0]

    assert "A legacy flat token is not an alternative to structured wiring" in rule
    assert "slot (preferred), or the legacy flat token" not in rule
    assert "staging and wiring happen in the same mutation" in rule


def test_delivered_legacy_compatibility_does_not_replace_structured_slots() -> None:
    delivered = " ".join(build_system_prompt(None).split())

    assert "Historical legacy-only flat tokens remain supported when no pending `vague_term` requirement matches that term" in delivered
    assert "Other review kinds may coexist with that legacy token" in delivered
    assert "A flat `{{interpretation:<term>}}` placeholder also wires the slot" not in delivered


@pytest.mark.parametrize("mode", ["single_ref", "repeated_ref", "flat_only", "missing_ref", "duplicate_requirement"])
def test_structured_wiring_matches_actual_resolution(mode: str) -> None:
    requirement = {
        "id": "clarity:review_text",
        "kind": "vague_term",
        "user_term": "clarity",
        "status": "pending",
        "draft": "Prefer direct language.",
        "event_id": None,
        "accepted_value": None,
        "resolved_prompt_template_hash": None,
    }
    parts = [
        {"kind": "text", "text": "Review: "},
        {"kind": "interpretation_ref", "requirement_id": "clarity:review_text"},
    ]
    options = {
        "prompt_template": "Review: Prefer direct language.",
        "prompt_template_parts": parts,
        "interpretation_requirements": [requirement],
    }
    if mode == "repeated_ref":
        parts.extend(
            [
                {"kind": "text", "text": " Again: "},
                {"kind": "interpretation_ref", "requirement_id": "clarity:review_text"},
            ]
        )
        options["prompt_template"] = "Review: Prefer direct language. Again: Prefer direct language."
    elif mode == "flat_only":
        del options["prompt_template_parts"]
        options["prompt_template"] = "Review: {{interpretation:clarity}}"
    elif mode == "missing_ref":
        options["prompt_template_parts"] = [{"kind": "text", "text": "Review the writing."}]
    elif mode == "duplicate_requirement":
        options["interpretation_requirements"] = [requirement, {**requirement, "id": "second-clarity"}]
    node = NodeSpec(
        id="review_text",
        node_type="transform",
        plugin="llm",
        input="rows",
        on_success="out",
        on_error="discard",
        options=options,
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )
    state = CompositionState(source=None, nodes=(node,), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)

    if mode in {"flat_only", "missing_ref", "duplicate_requirement"}:
        assert vague_term_wiring_count(node.options, user_term="clarity") != 1
        with pytest.raises(ToolArgumentError) as rejection:
            _assert_affected_component(state, node.id, InterpretationKind.VAGUE_TERM, "clarity")
        if mode == "flat_only":
            delivered_error = arg_error_payload(rejection.value, "request_interpretation_review")["error"]
            assert "one matching pending vague_term requirement" in delivered_error
            assert "at least one matching interpretation_ref" in delivered_error
            assert "legacy-only: exactly one placeholder with no matching pending vague_term" in delivered_error
        with pytest.raises(InterpretationPlaceholderConsumedError):
            _patch_structured_interpretation_prompt(
                options=node.options, affected_node_id=node.id, user_term="clarity", accepted_value="Use concrete nouns."
            )
    else:
        assert vague_term_wiring_count(node.options, user_term="clarity") == 1
        assert _assert_affected_component(state, node.id, InterpretationKind.VAGUE_TERM, "clarity") == "Prefer direct language."
        patched = _patch_structured_interpretation_prompt(
            options=node.options, affected_node_id=node.id, user_term="clarity", accepted_value="Use concrete nouns."
        )
        assert patched is not None
        expected = "Review: Use concrete nouns."
        if mode == "repeated_ref":
            expected += " Again: Use concrete nouns."
        assert patched["prompt_template"] == expected


@pytest.mark.parametrize("schema_mode", ["fixed", "flexible"])
def test_csv_ingestion_coercion_is_not_downstream_input_coercion(tmp_path: Path, schema_mode: str) -> None:
    path = tmp_path / "inventory.csv"
    path.write_text("item,quantity,note\nbolts,12,0042\nnuts,invalid,007\n")
    context = make_context(node_id="inventory_source")
    declared = CSVSource(
        {
            "path": str(path),
            "on_validation_failure": "rejected",
            "schema": {"mode": schema_mode, "fields": ["item: str", "quantity: int", "note: str"]},
        }
    )
    declared_rows = list(declared.load(context))
    assert len(declared_rows) == 2
    assert declared_rows[0].is_quarantined is False
    assert declared_rows[0].row["quantity"] == 12
    assert type(declared_rows[0].row["quantity"]) is int
    assert declared_rows[0].row["note"] == "0042"
    assert declared_rows[1].is_quarantined is True
    assert declared_rows[1].row["quantity"] == "invalid"
    assert declared_rows[1].quarantine_destination == "rejected"

    observed = CSVSource({"path": str(path), "on_validation_failure": "rejected", "schema": {"mode": "observed"}})
    observed_rows = list(observed.load(context))
    assert [row.is_quarantined for row in observed_rows] == [False, False]
    assert observed_rows[0].row["quantity"] == "12"
    assert type(observed_rows[0].row["quantity"]) is str
    consumer = create_schema_from_config(
        SchemaConfig.from_dict({"mode": "flexible", "fields": ["quantity: int"]}),
        "QuantityInput",
        allow_coercion=False,
    )
    assert consumer.model_validate(declared_rows[0].row).to_row()["quantity"] == 12
    with pytest.raises(ValidationError):
        consumer.model_validate(observed_rows[0].row)
    assert observed_rows[0].row["quantity"] == "12"


@pytest.mark.parametrize("canary", ["untrusted-diagnostic-detail", "x" * 4097], ids=["dynamic", "oversized"])
def test_wiring_repair_payload_remains_closed_and_bounded(canary: str) -> None:
    error = ToolArgumentError(
        argument="affected_node_id",
        expected="prompt_template_parts " + canary,
        actual_type=canary,
    )
    delivered = arg_error_payload(error, "request_interpretation_review")["error"]

    assert canary not in delivered
    assert len(delivered) < 4096
