"""Stage 2b: a field a deferring node could add is left to the runtime preflight, not refused at authoring.

A node whose contract waits on a file (``contract_probe_deferred``) votes with
what it forwards and never with what it adds (stage 2a). Its additions are
decided on the bytes before the run starts, so a missing-field check must not
refuse them. ``_PossibleAdditions`` carries them beside the vote, along the
same paths the vote walks, and never into a guarantee or an edge contract:

- the names the config shape fixes (``reference_join`` output keys,
  ``file_inject`` output fields) are demoted, and nothing else: a misspelling
  stays an error at every site where the set is known;
- a deferring node whose config cannot name what it adds (``llm`` with
  ``response_field`` in a file, any deferring non-pass-through) leaves every
  missing field to the preflight; the generic notice says "the fields this
  step reads and adds";
- a closed schema downstream bounds them to its declared names;
- fan-ins combine them the way the vote combines guarantees.

Each verdict here is paired with the runtime's on the real bytes.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from elspeth.contracts.freeze import deep_thaw
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec, ValidationSummary
from elspeth.web.execution.service import ExecutionServiceImpl
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    prompt_review_anchor_hash_from_options,
    prompt_review_draft_from_options,
)
from elspeth.web.sessions.routes._helpers import _composer_persisted_validation
from tests.helpers.execution_custody import ExecutionTestCustody
from tests.unit.web.composer import test_deferred_contract_notice as g
from tests.unit.web.composer import test_file_inject_upload_only_tool_parity as upload_tools
from tests.unit.web.composer import test_llm_file_prompt_probe as llm_fixtures
from tests.unit.web.execution import test_deferred_contract_runtime_refusal as runtime_proofs
from tests.unit.web.execution import test_service as _service_tests
from tests.unit.web.execution.test_service import _WebSettingsStub

blob_env = upload_tools.blob_env

# Run-admission fixtures, requested by name only (none of them is autouse here).
broadcaster = _service_tests.broadcaster
execution_fixture = _service_tests.execution_fixture
mock_loop = _service_tests.mock_loop
mock_session_service = _service_tests.mock_session_service
mock_settings = _service_tests.mock_settings
real_loop = _service_tests.real_loop
service = _service_tests.service

NO_COLUMN_TABLE = '[{"sku": "p", "label": "v"}]'
TYPO = "addde"


def _join_over_fixed(content: object) -> list[NodeSpec]:
    return [g._join_node("enrich", "src_out", "mid", g._join(content, "json", schema=g.FIXED_SOURCE))]


def _join_then_open_relay(content: object) -> list[NodeSpec]:
    return [
        g._join_node("enrich", "src_out", "hop", g._join(content, "json", schema=g.FIXED_SOURCE)),
        g._transform("relay", "passthrough", "hop", "mid", {"schema": g.OBSERVED}),
    ]


def _join_then_closed_relay(content: object, *, declares_added: bool = False) -> list[NodeSpec]:
    fields = ["id: str", "k: str", *(["added: str?"] if declares_added else [])]
    return [
        g._join_node("enrich", "src_out", "hop", g._join(content, "json", schema=g.FIXED_SOURCE)),
        g._transform("relay", "passthrough", "hop", "mid", {"schema": {"mode": "fixed", "fields": fields}}),
    ]


_SHAPES = {"join_over_fixed": _join_over_fixed, "join_then_open_relay": _join_then_open_relay}


def _site_graph(tmp_path: Path, site: str, producers: list[NodeSpec], required: list[str]) -> CompositionState:
    """The producer chain writes ``mid``; the consumer at ``site`` requires ``required``."""
    if site == "sink":
        last = producers[-1]
        return g._graph(
            tmp_path,
            *producers[:-1],
            replace(last, on_success="output"),
            source_schema=g.FIXED_SOURCE,
            sink_schema={"mode": "observed", "required_fields": required},
        )
    if site == "node":
        consumer = g._transform("use", "passthrough", "mid", "output", {"schema": g.OBSERVED, "required_input_fields": required})
    else:
        conversions = [{"field": name, "to": "str"} for name in required]
        consumer = g._transform("use", "type_coerce", "mid", "output", {"schema": g.OBSERVED, "conversions": conversions})
    return g._graph(tmp_path, *producers, consumer, source_schema=g.FIXED_SOURCE)


def _contracts_into(summary: ValidationSummary, consumer: str) -> list[Any]:
    return [contract for contract in summary.edge_contracts if contract.to_id == consumer]


_CONSUMER_ID = {"sink": "output:output", "node": "use", "declared_input": "use"}


# ---------------------------------------------------------------------------
# Every missing-field site the deferring paths reach
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", sorted(_SHAPES))
@pytest.mark.parametrize("site", ["sink", "node", "declared_input"])
def test_a_field_the_join_could_add_is_left_to_the_preflight_at_every_site(tmp_path: Path, site: str, shape: str) -> None:
    """``added`` is the join's ``output`` key: valid with the named notice, no verdict on the edge, the bytes decide.

    Before 2b the sink and node sites refused it with ``guarantees: [id, k]``
    while execution validation admitted the graph. The declared-input site
    never reached it (its closedness gate skips an unknown producer); it is
    pinned here so a change to that gate is seen.
    """
    build = _SHAPES[shape]
    summary = _site_graph(tmp_path / "authoring", site, build(g._marker(g.STR_TABLE)), ["added"]).validate()
    admitted = g._execution_validate(
        tmp_path / "admit", _site_graph(tmp_path / "admit", site, build(g._marker(g.STR_TABLE)), ["added"]), g.STR_TABLE
    )
    refused = g._execution_validate(
        tmp_path / "refuse", _site_graph(tmp_path / "refuse", site, build(g._marker(NO_COLUMN_TABLE)), ["added"]), NO_COLUMN_TABLE
    )

    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert g._notices(summary) == [g._join_notice("enrich")]
    assert not _contracts_into(summary, _CONSUMER_ID[site])
    for contract in summary.edge_contracts:
        assert "added" not in contract.producer_guarantees, contract.to_dict()
    assert admitted.is_valid, [error.message for error in admitted.errors]
    assert not refused.is_valid
    assert any("resolved against none of the 1 reference entries" in error.message for error in refused.errors)


@pytest.mark.parametrize("shape", sorted(_SHAPES))
@pytest.mark.parametrize(("site", "code"), [("sink", "sink_contract_violation"), ("node", "schema_contract_violation")])
def test_a_misspelt_field_is_still_refused_where_the_join_names_what_it_adds(tmp_path: Path, site: str, code: str, shape: str) -> None:
    """The typo case, per site: ``addde`` is neither upstream nor an ``output`` key, so it stays an error.

    Beside the joined field it is the ONLY one reported missing, and the edge
    still carries no verdict (part of it is undecided).
    """
    alone = _site_graph(tmp_path / "alone", site, _SHAPES[shape](g._marker(g.STR_TABLE)), [TYPO]).validate()
    beside = _site_graph(tmp_path / "beside", site, _SHAPES[shape](g._marker(g.STR_TABLE)), ["added", TYPO]).validate()
    execution = g._execution_validate(
        tmp_path / "beside", _site_graph(tmp_path / "beside", site, _SHAPES[shape](g._marker(g.STR_TABLE)), [TYPO]), g.STR_TABLE
    )

    for summary in (alone, beside):
        assert not summary.is_valid
        [error] = summary.errors
        assert error.error_code == code
        assert error.message.endswith(f"guarantees: [id, k]. Missing fields: [{TYPO}].")
        assert g._notices(summary) == [g._join_notice("enrich")]
    assert not _contracts_into(beside, _CONSUMER_ID[site])
    assert not execution.is_valid


def _two_deferring_joins(content: object) -> list[NodeSpec]:
    """Two file-held joins in a row: the second (flexible, so ``added`` may pass) adds ``later`` and carries ``added`` on."""
    flexible = {"mode": "flexible", "fields": ["id: str", "k: str"]}
    second = g._join(content, "json", schema=flexible, output={"later": "ref['description']"})
    return [
        g._join_node("enrich", "src_out", "hop", g._join(content, "json", schema=g.FIXED_SOURCE)),
        g._join_node("enrich_again", "hop", "mid", second),
    ]


@pytest.mark.parametrize(("site", "code"), [("sink", "sink_contract_violation"), ("node", "schema_contract_violation")])
def test_a_deferring_pass_through_carries_on_what_a_deferring_node_before_it_could_add(tmp_path: Path, site: str, code: str) -> None:
    """``added`` comes from the FIRST join; the second, deferring too, forwards it: left to the preflight, a typo still refused."""
    carried = _site_graph(tmp_path / "carried", site, _two_deferring_joins(g._marker(g.STR_TABLE)), ["added", "later"]).validate()
    typo = _site_graph(tmp_path / "typo", site, _two_deferring_joins(g._marker(g.STR_TABLE)), ["added", TYPO]).validate()
    admitted = g._execution_validate(
        tmp_path / "admit",
        _site_graph(tmp_path / "admit", site, _two_deferring_joins(g._marker(g.STR_TABLE)), ["added", "later"]),
        g.STR_TABLE,
    )

    assert admitted.is_valid, [error.message for error in admitted.errors]
    assert carried.is_valid, [entry.to_dict() for entry in carried.errors]
    assert sorted(g._notices(carried)) == sorted([g._join_notice("enrich"), g._join_notice("enrich_again", "later")])
    assert [entry.error_code for entry in typo.errors] == [code]
    assert typo.errors[0].message.endswith(f"Missing fields: [{TYPO}].")


def test_LIMIT_a_misspelt_declared_input_after_a_deferring_join_is_left_to_the_preflight(tmp_path: Path) -> None:
    """Known limit, unchanged by 2b: the declared-input rule needs a CLOSED producer, and a deferring one is unknown.

    Proving a field absent needs the producer's upper bound, which only its
    constructed output schema gives; 2b publishes nothing for a deferring
    node. Authoring is valid with the notice, and execution validation
    refuses the typo on the bytes.
    """
    state = _site_graph(tmp_path, "declared_input", _join_over_fixed(g._marker(g.STR_TABLE)), [TYPO])

    summary = state.validate()
    execution = g._execution_validate(tmp_path, state, g.STR_TABLE)

    assert summary.is_valid
    assert g._notices(summary) == [g._join_notice("enrich")]
    assert not execution.is_valid
    assert any(f"requires input fields ['{TYPO}']" in error.message for error in execution.errors)


@pytest.mark.parametrize(("site", "code"), [("sink", "sink_contract_violation"), ("node", "schema_contract_violation")])
def test_a_closed_schema_after_the_join_bounds_what_it_could_add(tmp_path: Path, site: str, code: str) -> None:
    """A fixed relay declaring only ``id, k`` lets nothing else through: ``added`` is refused, as the runtime refuses it."""
    state = _site_graph(tmp_path, site, _join_then_closed_relay(g._marker(g.STR_TABLE)), ["added"])

    summary = state.validate()
    execution = g._execution_validate(tmp_path, state, g.STR_TABLE)

    assert [entry.error_code for entry in summary.errors] == [code]
    assert summary.errors[0].message.endswith("Producer (passthrough) guarantees: [id, k]. Missing fields: [added].")
    assert not execution.is_valid


@pytest.mark.parametrize("site", ["sink", "node", "declared_input"])
def test_a_closed_schema_declaring_the_field_optional_passes_it_on(tmp_path: Path, site: str) -> None:
    """``added: str?`` on the closed relay: not guaranteed, yet the join could add it, so the bytes decide."""

    def build(root: Path) -> CompositionState:
        return _site_graph(root, site, _join_then_closed_relay(g._marker(g.STR_TABLE), declares_added=True), ["added"])

    summary = build(tmp_path / "authoring").validate()
    admitted = g._execution_validate(tmp_path / "admit", build(tmp_path / "admit"), g.STR_TABLE)

    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert g._notices(summary) == [g._join_notice("enrich")]
    assert admitted.is_valid, [error.message for error in admitted.errors]


# ---------------------------------------------------------------------------
# Where the config cannot name what the step adds
# ---------------------------------------------------------------------------


def _resolved_prompt_review(state: CompositionState) -> CompositionState:
    """Resolve the llm node's prompt-template review the way the review card does, so run admission can proceed."""
    node = state.nodes[0]
    options = deep_thaw(node.options)
    requirements = options.pop(INTERPRETATION_REQUIREMENTS_KEY) if INTERPRETATION_REQUIREMENTS_KEY in options else []
    anchor = prompt_review_anchor_hash_from_options(options)
    draft = prompt_review_draft_from_options(options)
    assert anchor is not None and draft is not None
    review = {
        "id": f"prompt_template_review:{node.id}",
        "kind": "llm_prompt_template",
        "user_term": f"llm_prompt_template:{node.id}",
        "status": "resolved",
        "draft": draft,
        "event_id": "evt-prompt",
        "accepted_value": draft,
        "accepted_artifact_hash": None,
        "resolved_prompt_template_hash": anchor,
    }
    kept = [requirement for requirement in requirements if requirement["kind"] != "llm_prompt_template"]
    options[INTERPRETATION_REQUIREMENTS_KEY] = [*kept, review]
    return replace(state, nodes=(replace(node, options=options), *state.nodes[1:]))


_LLM_NOTICE = (
    "Node 'classify' (llm) reads an attached file that this check does not open, so the fields this step reads and adds, "
    "their types and whether they can be empty are checked before the run starts. No change is needed."
)


@pytest.mark.parametrize(
    ("required", "file_names", "admitted"),
    [
        pytest.param("summary", "summary", True, id="the-file-names-the-required-field"),
        pytest.param("summary", "verdict", False, id="the-file-names-another-field"),
        pytest.param("sumary", "summary", False, id="the-sink-misspells-it"),
    ],
)
def test_a_pass_through_whose_added_names_are_in_the_file_leaves_every_missing_field_to_the_preflight(
    tmp_path: Path, required: str, file_names: str, admitted: bool
) -> None:
    """``llm.response_field`` held in a file: nothing in the config names what the step adds.

    Authoring cannot tell ``summary`` from ``sumary`` here, so neither is
    refused; the generic notice says the step's fields are checked before the
    run. The runtime reads the name and decides all three.
    """
    session_id, state = llm_fixtures._state(
        tmp_path,
        prompt_template=llm_fixtures.GOOD_PROMPT,
        options={"response_field": llm_fixtures._marker(file_names)},
        sink_schema={"mode": "observed", "required_fields": ["review", required]},
    )
    state = _resolved_prompt_review(state)

    summary = state.validate()
    execution = llm_fixtures._execution_validate(tmp_path, session_id, state, file_names)

    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert g._notices(summary) == [_LLM_NOTICE]
    assert not [contract for contract in summary.edge_contracts if contract.from_id == "classify"]
    assert execution.is_valid is admitted, [error.message for error in execution.errors]


@pytest.mark.parametrize(
    ("required", "file_names", "admitted"),
    [
        pytest.param("added", "added", True, id="the-file-names-the-required-field"),
        pytest.param("added", "other", False, id="the-file-names-another-field"),
        pytest.param(TYPO, "added", False, id="the-consumer-misspells-it"),
    ],
)
def test_a_deferring_non_pass_through_leaves_a_node_consumers_requirement_to_the_preflight(
    tmp_path: Path, required: str, file_names: str, admitted: bool
) -> None:
    """``field_mapper`` with a mapping target in a file abstains (2a); the NODE direction folded that to ``(none)``.

    A transform's ``required_input_fields`` treats an abstaining producer as
    guaranteeing nothing, so before 2b this read ``Producer (field_mapper)
    guarantees: [(none)]`` while the runtime admitted a file naming the field.
    """
    mapper = g._transform("rename", "field_mapper", "src_out", "mid", {"schema": g.OBSERVED, "mapping": {"id": g._marker(file_names)}})
    consumer = g._transform("use", "passthrough", "mid", "output", {"schema": g.OBSERVED, "required_input_fields": [required]})
    state = g._graph(tmp_path, mapper, consumer, source_schema=g.FIXED_SOURCE)

    summary = state.validate()
    execution = g._execution_validate(tmp_path, state, file_names, mime_type="text/plain")

    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert len(g._notices(summary)) == 1
    assert not _contracts_into(summary, "use")
    assert execution.is_valid is admitted, [error.message for error in execution.errors]


def test_the_generic_notice_names_what_the_step_reads_when_the_file_holds_an_input_field(tmp_path: Path) -> None:
    """``blob_text_expand.blob_ref_field`` in a file names a field the step READS, not one it adds.

    The notice must not speak only of added fields there: the runtime refuses
    the input the bytes name (``nope``), which the old wording hid. Authoring
    stays valid, as before; only the sentence changes.
    """
    expand = g._transform(
        "t",
        "blob_text_expand",
        "src_out",
        "output",
        {"schema": g.OBSERVED, "blob_ref_field": g._marker("nope"), "output_field": "line", "index_field": "line_index"},
    )
    state = g._graph(tmp_path, expand, source_schema=g.FIXED_SOURCE)

    summary = state.validate()
    execution = g._execution_validate(tmp_path, state, "nope", mime_type="text/plain")

    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert g._notices(summary) == [
        "Node 't' (blob_text_expand) reads an attached file that this check does not open, so the fields this step reads and adds, "
        "their types and whether they can be empty are checked before the run starts. No change is needed."
    ]
    assert not execution.is_valid
    assert any("nope" in error.message for error in execution.errors), [error.message for error in execution.errors]


# ---------------------------------------------------------------------------
# Fan-ins combine possible additions the way the vote combines guarantees
# ---------------------------------------------------------------------------


def _inline_join(output_field: str) -> tuple[str, dict[str, Any]]:
    plugin, options = g._join(g.STR_TABLE, "json", schema=g.FIXED_SOURCE)
    return plugin, {**options, "output": {output_field: "ref['description']"}}


def _fan_requiring(tmp_path: Path, control_field: str, content: object, *, merge: str, required: list[str]) -> CompositionState:
    """fork -> {inline join writing ``control_field``, file-held join writing ``added``} -> merge -> sink requiring ``required``."""
    state = g._fan(tmp_path, _inline_join(control_field), g._join(content, "json", schema=g.FIXED_SOURCE), merge=merge)
    (sink,) = state.outputs
    return replace(state, outputs=(replace(sink, options={**sink.options, "schema": {"mode": "observed", "required_fields": required}}),))


@pytest.mark.parametrize("merge", ["row_union", "coalesce"])
def test_a_fan_in_leaves_a_field_every_branch_has_or_could_add_to_the_preflight(tmp_path: Path, merge: str) -> None:
    """The inline branch guarantees ``added``; the file-held one could add it: neither guaranteed nor refused."""
    summary = _fan_requiring(tmp_path / "a", "added", g._marker(g.STR_TABLE), merge=merge, required=["added"]).validate()
    execution = g._execution_validate(
        tmp_path / "b", _fan_requiring(tmp_path / "b", "added", g._marker(g.STR_TABLE), merge=merge, required=["added"]), g.STR_TABLE
    )

    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert g._notices(summary) == [g._join_notice("treatment")]
    assert execution.is_valid, [error.message for error in execution.errors]


def test_a_row_union_still_refuses_a_field_one_branch_can_never_carry(tmp_path: Path) -> None:
    """The inline branch writes ``other``, not ``added``: rows from it lack ``added`` whatever the file says."""
    state = _fan_requiring(tmp_path, "other", g._marker(g.STR_TABLE), merge="row_union", required=["added"])

    summary = state.validate()
    execution = g._execution_validate(tmp_path, state, g.STR_TABLE)

    assert [entry.error_code for entry in summary.errors] == ["sink_contract_violation"]
    assert summary.errors[0].message.endswith("Missing fields: [added].")
    assert not execution.is_valid


def test_a_require_all_coalesce_judges_a_field_only_the_file_held_branch_adds_as_its_inline_twin_does(tmp_path: Path) -> None:
    """Every branch arrives, so the merged row carries the file-held branch's ``added``: authoring agrees with the twin.

    Authoring only. The runtime refuses this union for branches whose fields
    differ (``other`` against ``added``) whether the table is inline or held,
    a coalesce disagreement older than and separate from the deferral.
    """

    def build(root: Path, content: object, required: list[str]) -> CompositionState:
        return _fan_requiring(root, "other", content, merge="coalesce", required=required)

    held = build(tmp_path / "held", g._marker(g.STR_TABLE), ["added"]).validate()
    twin = build(tmp_path / "twin", g.STR_TABLE, ["added"]).validate()
    held_typo = build(tmp_path / "held_typo", g._marker(g.STR_TABLE), [TYPO]).validate()

    assert held.is_valid and twin.is_valid, [entry.to_dict() for entry in held.errors]
    assert g._notices(held) == [g._join_notice("treatment")]
    assert [entry.error_code for entry in held_typo.errors] == ["sink_contract_violation"]
    assert held_typo.errors[0].message.endswith(f"Missing fields: [{TYPO}].")


# ---------------------------------------------------------------------------
# The web gates decide what authoring left to them; the planner sees it in one payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["sink", "node"])
async def test_the_save_path_and_run_admission_refuse_what_the_table_cannot_add(
    tmp_path: Path,
    service: ExecutionServiceImpl,
    mock_session_service: MagicMock,
    mock_settings: _WebSettingsStub,
    execution_fixture: ExecutionTestCustody,
    site: str,
) -> None:
    """The headline graph over a table without the column: valid at authoring, refused before any run exists."""
    state = _site_graph(tmp_path, site, _join_over_fixed(g._marker(NO_COLUMN_TABLE)), ["added"])
    authoring = state.validate()
    refusal = "output fields ['added'] resolved against none of the 1 reference entries"

    runtime = await asyncio.to_thread(runtime_proofs.save_path_preflight, tmp_path, state, NO_COLUMN_TABLE)
    persisted_valid, persisted_errors = _composer_persisted_validation(authoring, runtime)
    admission = await runtime_proofs.run_admission_refusal(
        service, mock_session_service, mock_settings, execution_fixture, tmp_path, state, NO_COLUMN_TABLE
    )

    assert authoring.is_valid, [entry.to_dict() for entry in authoring.errors]
    assert persisted_valid is False
    assert persisted_errors is not None and any(refusal in error.message for error in persisted_errors), persisted_errors
    assert any(refusal in error.message for error in admission.errors), [error.message for error in admission.errors]


def test_the_save_path_admits_the_headline_graph_over_a_table_with_the_column(tmp_path: Path) -> None:
    """Control for the refusal above: only the table differs."""
    state = _site_graph(tmp_path, "sink", _join_over_fixed(g._marker(g.STR_TABLE)), ["added"])
    authoring = state.validate()

    runtime = runtime_proofs.save_path_preflight(tmp_path, state, g.STR_TABLE)

    assert runtime.is_valid, [error.message for error in runtime.errors]
    assert _composer_persisted_validation(authoring, runtime) == (True, None)


def test_the_tool_result_carries_is_valid_and_the_notice_for_a_field_the_join_could_add(tmp_path: Path, blob_env: dict[str, Any]) -> None:
    """Wiring the table to a file turns a satisfied requirement into one the preflight decides, in ONE payload."""
    state = _site_graph(
        tmp_path, "sink", [g._join_node("enrich", "src_out", "mid", g._join(g.CSV_TABLE, "csv", schema=g.FIXED_SOURCE))], ["added"]
    )
    uploaded = upload_tools._upload(blob_env, content=g.CSV_TABLE)

    result = upload_tools._execute(
        "wire_blob_inline_ref",
        {"field_path": "node:enrich.options.reference_content", "blob_id": uploaded["blob_ref"]},
        state,
        blob_env,
    )
    payload = result.to_dict()

    assert state.validate().is_valid, "the inline twin guarantees ``added``"
    assert result.success is True, payload
    assert payload["validation"]["is_valid"] is True, payload["validation"]["errors"]
    notices = [warning for warning in payload["validation"]["warnings"] if warning["error_code"] == g.DEFERRED]
    assert [(notice["component"], notice["message"]) for notice in notices] == [("node:enrich", g._join_notice("enrich"))]


# ---------------------------------------------------------------------------
# The registry pin: every transform x every text position of its probe config
# ---------------------------------------------------------------------------

# MEASURED (stage 2b scan): the positions where a marker is a probe FAILURE,
# not a deferral, because the position takes a fixed value (an enum, a mode,
# a provider) that no file can stand in for. Their errors are real config
# failures; each must stay one, so a position that starts deferring shows up.
_LITERAL_POSITIONS = frozenset(
    {
        ("aws_textract_document_analysis", "auth_mode"),
        ("aws_textract_document_analysis", "feature_types.0"),
        ("aws_textract_inline_analysis", "auth_mode"),
        ("aws_textract_inline_analysis", "document_format"),
        ("aws_textract_inline_analysis", "feature_types.0"),
        ("blob_json_expand", "source"),
        ("file_inject", "mode"),
        ("file_inject", "files.probe.format"),
        ("llm", "provider"),
        ("rag_retrieval", "provider"),
        ("rag_retrieval", "provider_config.mode"),
        ("reference_join", "reference_format"),
        ("type_coerce", "conversions.0.to"),
    }
)


def _text_leaves(value: object, path: tuple[str | int, ...] = ()) -> list[tuple[tuple[str | int, ...], str]]:
    if type(value) is dict:
        return [leaf for key, child in value.items() if key != "schema" for leaf in _text_leaves(child, (*path, key))]
    if type(value) is list:
        return [leaf for index, child in enumerate(value) for leaf in _text_leaves(child, (*path, index))]
    return [(path, value)] if type(value) is str else []


def _registry_positions() -> list[tuple[str, tuple[str | int, ...]]]:
    positions = []
    for plugin_class in sorted(get_shared_plugin_manager().get_transforms(), key=lambda cls: cls.name):
        positions.extend((plugin_class.name, path) for path, _text in _text_leaves(plugin_class.probe_config()))
    return positions


def _with_marker(options: dict[str, Any], path: tuple[str | int, ...], text: str) -> dict[str, Any]:
    held = copy.deepcopy(options)
    container: Any = held
    for segment in path[:-1]:
        container = container[segment]
    container[path[-1]] = {"blob_ref": str(g.TABLE_BLOB), "mode": "inline_content", "sha256": hashlib.sha256(text.encode()).hexdigest()}
    return held


def _registry_graph(
    tmp_path: Path, plugin: str, options: dict[str, Any], inputs: frozenset[str], required: frozenset[str]
) -> CompositionState:
    """csv (declaring the node's inputs) -> node -> json sink requiring what the inline twin guarantees."""
    tmp_path.mkdir(parents=True)
    columns = sorted(inputs) or ["unused"]
    (tmp_path / "in.csv").write_text(",".join(columns) + "\n" + ",".join("v" for _ in columns) + "\n", encoding="utf-8")
    node = g._transform("step", plugin, "src_out", "output", options)
    sink_schema: dict[str, Any] = {"mode": "observed", "required_fields": sorted(required)} if required else {"mode": "observed"}
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="src_out",
            options={"path": str(tmp_path / "in.csv"), "schema": {"mode": "flexible", "fields": [f"{name}: str" for name in columns]}},
            on_validation_failure="discard",
        ),
        nodes=(node,),
        edges=(),
        outputs=(
            OutputSpec(
                name="output",
                plugin="json",
                options={"path": str(tmp_path / "o.jsonl"), "format": "jsonl", "schema": sink_schema},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


def _error_keys(summary: ValidationSummary) -> set[tuple[str | None, str]]:
    return {(entry.error_code, entry.component) for entry in summary.errors}


def _twin_and_held(
    tmp_path: Path, plugin: str, path: tuple[str | int, ...], *, extra_required: frozenset[str] = frozenset()
) -> tuple[ValidationSummary, ValidationSummary]:
    manager = get_shared_plugin_manager()
    plugin_class = manager.get_transform_by_name(plugin)
    options = plugin_class.probe_config()
    twin_node = manager.create_transform(plugin, copy.deepcopy(options))
    inputs = frozenset(twin_node.declared_input_fields)
    output_schema = twin_node._output_schema_config
    required = output_schema.get_effective_guaranteed_fields() if output_schema is not None else frozenset()
    if twin_node.passes_through_input:
        required |= inputs
    text = dict(_text_leaves(options))[path]
    twin = _registry_graph(tmp_path / "twin", plugin, copy.deepcopy(options), inputs, required).validate()
    held = _registry_graph(tmp_path / "held", plugin, _with_marker(options, path, text), inputs, required | extra_required).validate()
    return twin, held


@pytest.mark.parametrize(
    ("plugin", "path"), _registry_positions(), ids=lambda value: ".".join(map(str, value)) if type(value) is tuple else value
)
def test_a_file_at_any_text_position_adds_no_authoring_error_its_inline_twin_lacks(
    tmp_path: Path, plugin: str, path: tuple[str | int, ...]
) -> None:
    """The pin for 2a and 2b together: the sink asks for everything the inline twin guarantees.

    With the file in place of the text, authoring may defer (and say so) but
    must not refuse anything the twin satisfies. The literal positions are
    the exception, pinned by name: there the marker is a real config failure.
    """
    twin, held = _twin_and_held(tmp_path, plugin, path)
    extra = _error_keys(held) - _error_keys(twin)

    if (plugin, ".".join(map(str, path))) in _LITERAL_POSITIONS:
        assert extra, "a literal position stopped failing: move it out of _LITERAL_POSITIONS deliberately"
        assert "contract_probe_failed" in {entry.error_code for entry in held.warnings}
    else:
        assert not extra, [entry.message for entry in held.errors]


def test_the_registry_comparison_sees_an_error_the_twin_lacks(tmp_path: Path) -> None:
    """Control: the same comparison, with the held graph also requiring a field nothing adds, reports it."""
    twin, held = _twin_and_held(tmp_path, "reference_join", ("reference_content",), extra_required=frozenset({"nothing_writes_this"}))

    assert _error_keys(held) - _error_keys(twin) == {("sink_contract_violation", "output:output")}
