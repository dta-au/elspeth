"""Frozen-input pins for the nominal `type()` guards B40 landed in `tools/_common.py`.

`deep_freeze` (contracts/freeze.py) rewrites `dict`/`Mapping` to
`MappingProxyType` and `list` to `tuple`, recursively, and
`NodeSpec.__post_init__` freezes every container field. A nominal
`type(x) is dict` guard on anything read out of a frozen composition-state
bag is therefore ALWAYS False, and swapping an `isinstance(x, Mapping)`
check for one silently disables the guard with no test failure.

Each guard pinned here reads from frozen state, and each is safe for a
DIFFERENT reason — every one of them a coupling a future edit could break
without touching the guard itself:

* `_serialize_node` writes `"branches"` through `_serialize_branches`, which
  thaws and re-wraps in an exact `dict(...)`. Drop that thaw and the in-code
  comment above `if type(patched_branches) is not dict:` ("a closed owned
  union built by _serialize_branches") becomes false.
* `_normalize_echoed_interpretation_requirements` compares `stored_rows`
  against the PAIR `(list, tuple)` because `deep_freeze` renders the stored
  `interpretation_requirements` list as a `tuple`. Narrow that pair to
  `is not list` and the function abstains on every real stored options bag,
  silently switching off echo normalisation.
* `ReviewedSourceAuthority.__post_init__` screens its values with
  `(dict, MappingProxyType)`. Drop the second half and it rejects every
  frozen authority the guided settlement path builds.
* `_merged_component_rejection_result` must KEEP `isinstance(data, Mapping)`:
  `ToolResult.__post_init__` freezes `data`, so converting that one to the
  house scalar idiom silently discards the whole rejection payload. It is the
  one guard here that is pinned against a conversion rather than for one.

Every assertion below was mutation-checked when written — each fails, alone,
under the specific narrowing it guards against.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

from elspeth.contracts.freeze import deep_freeze
from elspeth.web.composer.state import (
    CompositionState,
    NodeSpec,
    PipelineMetadata,
    ValidationEntry,
    ValidationSummary,
)
from elspeth.web.composer.tools._common import (
    ToolResult,
    _normalize_echoed_interpretation_requirements,
    _serialize_node,
)
from elspeth.web.interpretation_state import INTERPRETATION_REQUIREMENTS_KEY


def _empty_state() -> CompositionState:
    """Build the minimal CompositionState a ToolResult envelope needs."""
    return CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)


def _node(**overrides: Any) -> NodeSpec:
    """Build a NodeSpec with every required field supplied."""
    fields: dict[str, Any] = {
        "id": "n1",
        "node_type": "transform",
        "plugin": "passthrough",
        "input": "shared",
        "on_success": None,
        "on_error": None,
        "condition": None,
        "routes": None,
        "fork_to": None,
        "policy": None,
        "merge": None,
        "branches": None,
        "options": {},
    }
    fields.update(overrides)
    return NodeSpec(**fields)


def test_node_spec_post_init_really_does_freeze_its_containers() -> None:
    """The premise both pins rest on: state containers ARE mappingproxy/tuple."""
    node = _node(
        branches={"alpha": "shared", "beta": "shared"},
        options={INTERPRETATION_REQUIREMENTS_KEY: [{"id": "r"}]},
    )
    assert type(node.branches) is MappingProxyType
    assert type(node.options) is MappingProxyType
    # Nested, not just the outer bag — this is what defeats `type(x) is dict`.
    assert type(node.options[INTERPRETATION_REQUIREMENTS_KEY]) is tuple
    assert type(node.options[INTERPRETATION_REQUIREMENTS_KEY][0]) is MappingProxyType


def test_serialize_node_branches_is_an_exact_dict_for_a_frozen_node_spec() -> None:
    """`_serialize_node` must thaw `branches` to an exact `dict`.

    `_duplicate_consumer_repair_suggestions` reads that value under
    `if type(patched_branches) is not dict:`. The mapping form must take the
    patch-in-place arm, so the serializer owes it an exact `dict` even though
    `NodeSpec.branches` is a `mappingproxy`.
    """
    node = _node(node_type="row_union", plugin=None, branches={"alpha": "a_conn", "beta": "b_conn"})
    assert type(node.branches) is MappingProxyType, "premise: the input is frozen"

    serialized = _serialize_node(node)

    assert type(serialized["branches"]) is dict
    assert serialized["branches"] == {"alpha": "a_conn", "beta": "b_conn"}
    # The alias (sequence) form is the other arm of the same closed union.
    assert type(_serialize_node(_node(branches=("alpha", "beta")))["branches"]) is list


def test_echoed_requirement_rows_normalise_against_a_frozen_stored_options_bag() -> None:
    """The `(list, tuple)` pair must admit `deep_freeze`'s tuple form.

    All NINE production call sites (enumerated, not sampled: two in
    `tools/transforms.py`, four in `tools/sources.py`, three in
    `tools/sessions.py`) pass either `None` or a stored spec's `.options` —
    `existing_node.options`, `current.options`, `current_nodes[id].options`,
    `current_source.options`, or `state.sources[name].options`. Both
    `NodeSpec.__post_init__` and `SourceSpec.__post_init__` run
    `freeze_fields(self, "options")`, so the stored
    `interpretation_requirements` list has ALWAYS been frozen to a `tuple` by
    the time it is read here. If the guard only accepted `list`, the function
    would abstain on every real call and the echoed resolver-owned row would
    reach the admission gate and be rejected (elspeth-c67fbbbd83).
    """
    stored_row = {
        "id": "prompt_template_review:n1",
        "kind": "llm_prompt_template",
        "user_term": "llm_prompt_template:n1",
        "status": "resolved",
        "draft": "d",
        "event_id": None,
        "accepted_value": "v",
        "accepted_artifact_hash": None,
        "resolved_prompt_template_hash": None,
    }
    stored_options = _node(plugin="llm", options={INTERPRETATION_REQUIREMENTS_KEY: [stored_row]}).options
    assert type(stored_options[INTERPRETATION_REQUIREMENTS_KEY]) is tuple, "premise: stored rows are frozen"

    supplied = {INTERPRETATION_REQUIREMENTS_KEY: [dict(stored_row)]}
    normalized, reduced = _normalize_echoed_interpretation_requirements(supplied, stored_options=stored_options)

    assert reduced is True
    assert normalized[INTERPRETATION_REQUIREMENTS_KEY] == [
        {"kind": "llm_prompt_template", "user_term": "llm_prompt_template:n1", "draft": "d"}
    ]


def test_merged_component_rejection_keeps_the_whole_data_payload_of_a_frozen_result() -> None:
    """`ToolResult.data` is frozen, so its merge guard must stay `isinstance`.

    `ToolResult.__post_init__` runs `freeze_fields(self, "data")`, so
    `base.data` is a `mappingproxy`. `_merged_component_rejection_result`
    merges it with `{**data, COMPONENTS_WITHHELD_KEY: ...} if
    isinstance(data, Mapping) else {COMPONENTS_WITHHELD_KEY: ...}`.

    Converting that guard to the house `type(data) is dict` scalar idiom would
    make it permanently False and send every merge to the else branch, so the
    rejection envelope the model receives would carry ONLY
    `components_withheld` — `error_code` and every detail silently dropped.
    This is a data-loss trap, not a lint-shape preference: do not convert it.
    """
    from elspeth.web.composer.tools._common import _merged_component_rejection_result

    def _result(component: str) -> ToolResult:
        return ToolResult(
            success=False,
            updated_state=_empty_state(),
            validation=ValidationSummary(
                is_valid=False,
                errors=(
                    ValidationEntry(
                        component="rejected_mutation",
                        message=f"rejected {component}",
                        severity="high",
                        error_code="rejected_mutation",
                    ),
                ),
            ),
            affected_nodes=(),
            data={"error_code": "rejected_mutation", "detail": {"component": component}},
        )

    base = _result("first")
    assert type(base.data) is MappingProxyType, "premise: ToolResult freezes data"

    merged = _merged_component_rejection_result([base, _result("second")], components_withheld=1)

    assert merged.data["error_code"] == "rejected_mutation"
    assert merged.data["detail"]["component"] == "first"
    assert merged.data["components_withheld"] == 1


def test_reviewed_source_authority_accepts_deep_frozen_reviewed_sources() -> None:
    """The values check names both halves of the frozen/unfrozen mapping pair.

    `ReviewedSourceAuthority.__post_init__` screens `reviewed_sources` values
    with `type(source) not in (dict, MappingProxyType)`. `deep_freeze` produces
    `MappingProxyType` and nothing else, so dropping that half would reject
    every frozen authority the guided settlement path builds.
    """
    from elspeth.web.composer.tools._common import ReviewedSourceAuthority

    frozen_sources = deep_freeze({"src": {"name": "src", "plugin": "csv", "options": {"path": "p"}}})
    assert type(frozen_sources["src"]) is MappingProxyType, "premise: the source record is frozen"

    authority = ReviewedSourceAuthority(
        session_id="s1",
        reviewed_anchor_hash="a" * 64,
        reviewed_sources=frozen_sources,
        verified_blob_paths={},
    )

    assert authority.reviewed_sources["src"]["plugin"] == "csv"
