from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml

from elspeth.core.config import ElspethSettings
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec, queue_node_contract_error
from elspeth.web.composer.yaml_generator import generate_public_yaml, generate_yaml
from elspeth.web.composer.yaml_importer import (
    MAX_RUNTIME_YAML_IMPORT_CHARS,
    RuntimeYamlImportError,
    _finite_positive_timeout,
    _nodes_from_runtime_list,
    _outputs_from_runtime_sinks,
    _queues_from_runtime_mapping,
    _reject_unimportable_sections,
    _require_nonblank_str,
    _require_str,
    _row_union_branches,
    _source_from_runtime_entry,
    composition_state_from_runtime_yaml,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]


def test_require_str_rejects_non_string_value() -> None:
    with pytest.raises(RuntimeYamlImportError, match=r"sources\.s\.plugin must be a non-empty string"):
        _require_str({"plugin": 7}, "plugin", "sources.s")


def test_require_nonblank_str_rejects_whitespace_only_value() -> None:
    """Trust-boundary test_ref: a name made only of whitespace is not a name.

    ``_require_str`` accepts it (it is a non-empty string); the nonblank
    variant exists precisely because the sections that route on identity —
    row_unions, collectors, scopes — cannot bind ``"   "`` to anything.
    """
    with pytest.raises(RuntimeYamlImportError, match=r"row_unions\[0\]\.name must be a non-empty string"):
        _require_nonblank_str({"name": "   "}, "name", "row_unions[0]")


def test_finite_positive_timeout_rejects_non_numeric_value() -> None:
    """Trust-boundary test_ref: a non-numeric timeout is refused, not coerced."""
    with pytest.raises(RuntimeYamlImportError, match=r"coalesce\[0\]\.timeout_seconds must be a finite positive number"):
        _finite_positive_timeout({"timeout_seconds": "soon"}, "coalesce[0]")


def test_row_union_branches_rejects_non_string_branch_entry() -> None:
    """Trust-boundary test_ref: a branch list entry that is not a name is refused."""
    with pytest.raises(RuntimeYamlImportError, match=r"row_unions\[0\]\.branches\[0\] must be a non-empty string"):
        _row_union_branches([7, "right"], "row_unions[0].branches")


def test_reject_unimportable_sections_refuses_a_declined_section_by_name() -> None:
    """Trust-boundary test_ref: a modelled section the composer cannot hold is
    refused NAMING itself, rather than silently dropped (elspeth-9482eda744)."""
    with pytest.raises(RuntimeYamlImportError, match="commencement_gates"):
        _reject_unimportable_sections({"transforms": [], "commencement_gates": [{"plugin": "corpus"}]})


def test_source_from_runtime_entry_rejects_non_mapping_entry() -> None:
    with pytest.raises(RuntimeYamlImportError, match=r"sources\.s must be a mapping"):
        _source_from_runtime_entry("s", ["not", "a", "mapping"])


@pytest.mark.parametrize("authored", ["", None, 17])
def test_source_from_runtime_entry_canonicalizes_unroutable_validation_failure_to_discard(
    authored: object,
) -> None:
    """elspeth-bcd7051143 pin: missing, empty, and non-string spellings all
    fall back to 'discard' through the shared source-validation-failure
    canonicalizer — the single owner every authoring seam routes through."""
    spec = _source_from_runtime_entry(
        "s",
        {"plugin": "csv", "on_success": "rows", "options": {}, "on_validation_failure": authored},
    )
    assert spec.on_validation_failure == "discard"


def test_nodes_from_runtime_list_rejects_non_sequence_section() -> None:
    with pytest.raises(RuntimeYamlImportError, match="transforms must be a list"):
        _nodes_from_runtime_list("not-a-list", "transforms", "transform")


@pytest.mark.parametrize(
    ("branches", "expected_input"),
    [
        (["a", "b"], "a"),
        ({"left": "a", "right": "b"}, "a"),
    ],
)
def test_nodes_from_runtime_list_derives_missing_coalesce_input_from_first_branch(
    branches: list[str] | dict[str, str],
    expected_input: str,
) -> None:
    nodes = _nodes_from_runtime_list(
        [{"name": "joined", "branches": branches, "policy": "require_all", "merge": "nested"}],
        "coalesce",
        "coalesce",
    )

    assert nodes[0].input == expected_input


def test_nodes_from_runtime_list_rejects_supplied_non_string_coalesce_input() -> None:
    with pytest.raises(RuntimeYamlImportError, match=r"coalesce\[0\]\.input must be a non-empty string"):
        _nodes_from_runtime_list(
            [{"name": "joined", "input": 7, "branches": ["a", "b"], "policy": "require_all", "merge": "nested"}],
            "coalesce",
            "coalesce",
        )


def test_nodes_from_runtime_list_rejects_coalesce_input_that_is_not_first_branch() -> None:
    with pytest.raises(RuntimeYamlImportError, match=r"coalesce\[0\]\.input must match its first branch input 'a'"):
        _nodes_from_runtime_list(
            [{"name": "joined", "input": "other", "branches": ["a", "b"], "policy": "require_all", "merge": "nested"}],
            "coalesce",
            "coalesce",
        )


@pytest.mark.parametrize("branches", [None, ["a"], {"left": "a"}])
def test_nodes_from_runtime_list_rejects_coalesce_without_two_branches(
    branches: list[str] | dict[str, str] | None,
) -> None:
    with pytest.raises(RuntimeYamlImportError, match=r"coalesce\[0\]\.branches must contain at least two input connections"):
        _nodes_from_runtime_list(
            [{"name": "joined", "branches": branches, "policy": "require_all", "merge": "nested"}],
            "coalesce",
            "coalesce",
        )


def test_outputs_from_runtime_sinks_rejects_non_mapping_sinks() -> None:
    with pytest.raises(RuntimeYamlImportError, match="sinks must be a mapping"):
        _outputs_from_runtime_sinks(["not", "a", "mapping"])


def test_composition_state_from_runtime_yaml_rejects_non_mapping_root() -> None:
    with pytest.raises(RuntimeYamlImportError, match="pipeline YAML must be a mapping"):
        composition_state_from_runtime_yaml("- not\n- a\n- mapping\n")


def test_composition_state_from_runtime_yaml_rejects_oversized_document() -> None:
    """Hardening: a paste over the character cap is rejected before parsing."""
    oversized = "sources:\n  source:\n    plugin: csv\n" + ("#" * (MAX_RUNTIME_YAML_IMPORT_CHARS + 1))
    with pytest.raises(RuntimeYamlImportError, match="exceeds the 262144 character import limit"):
        composition_state_from_runtime_yaml(oversized)


def test_composition_state_from_runtime_yaml_rejects_malformed_yaml_syntax() -> None:
    """Hardening: genuinely non-YAML input is a categorized error, not a raw parser echo."""
    not_yaml = "sources: [unterminated\n  plugin: csv"
    with pytest.raises(RuntimeYamlImportError, match=r"^YAML parse failed: \w+Error$") as exc_info:
        composition_state_from_runtime_yaml(not_yaml)
    # Egress discipline: the error must name the exception class only, never
    # echo the pasted content back to the caller.
    assert "unterminated" not in str(exc_info.value)
    assert "plugin" not in str(exc_info.value)


def test_composition_state_from_runtime_yaml_rejects_non_pipeline_mapping() -> None:
    """Hardening: a valid YAML mapping that describes no pipeline section at all
    must not silently import as an empty (destructive-replace) composition."""
    not_a_pipeline = "shopping_list:\n  - milk\n  - eggs\nnotes: just some random yaml\n"
    with pytest.raises(RuntimeYamlImportError, match="must define at least one pipeline section"):
        composition_state_from_runtime_yaml(not_a_pipeline)


def test_composition_state_from_runtime_yaml_rejects_empty_mapping() -> None:
    with pytest.raises(RuntimeYamlImportError, match="must define at least one pipeline section"):
        composition_state_from_runtime_yaml("{}\n")


@pytest.mark.parametrize(
    ("branches_yaml", "expected_branches", "expected_input"),
    [
        ("[control, treatment]", {"control": "control", "treatment": "treatment"}, "control"),
        (
            "\n      control_branch: control_scored\n      treatment_branch: treatment_scored",
            {"control_branch": "control_scored", "treatment_branch": "treatment_scored"},
            "control_scored",
        ),
    ],
)
def test_composition_state_from_runtime_yaml_imports_row_union_branches(
    branches_yaml: str,
    expected_branches: dict[str, str],
    expected_input: str,
) -> None:
    state = composition_state_from_runtime_yaml(
        f"""
row_unions:
  - name: variants
    branches: {branches_yaml}
    on_success: compared
"""
    )

    assert len(state.nodes) == 1
    row_union = state.nodes[0]
    assert row_union.node_type == "row_union"
    assert row_union.id == "variants"
    assert isinstance(row_union.branches, Mapping)
    assert row_union.branches == expected_branches
    assert row_union.input == expected_input
    assert row_union.on_success == "compared"
    assert row_union.timeout_seconds is None


def test_composition_state_from_runtime_yaml_imports_row_union_timeout_and_matching_input() -> None:
    state = composition_state_from_runtime_yaml(
        """
row_unions:
  - name: variants
    branches:
      control_branch: control_scored
      treatment_branch: treatment_scored
    input: control_scored
    on_success: compared
    timeout_seconds: 2.5
"""
    )

    row_union = state.nodes[0]
    assert row_union.input == "control_scored"
    assert row_union.timeout_seconds == 2.5


def test_composition_state_from_runtime_yaml_rejects_non_list_row_unions_section() -> None:
    with pytest.raises(RuntimeYamlImportError, match="row_unions must be a list"):
        composition_state_from_runtime_yaml(
            """
row_unions:
  variants:
    branches: [control, treatment]
    on_success: compared
"""
        )


@pytest.mark.parametrize(
    "branches_yaml",
    [
        "[control]",
        "[control, control]",
        "{control_branch: control_scored}",
    ],
)
def test_composition_state_from_runtime_yaml_rejects_row_union_without_two_unique_branches(branches_yaml: str) -> None:
    with pytest.raises(RuntimeYamlImportError, match=r"row_unions\[0\]\.branches must contain at least two unique branches"):
        composition_state_from_runtime_yaml(
            f"""
row_unions:
  - name: variants
    branches: {branches_yaml}
    on_success: compared
"""
        )


@pytest.mark.parametrize("on_success_line", ["", "    on_success: '   '"])
def test_composition_state_from_runtime_yaml_rejects_missing_or_blank_row_union_on_success(on_success_line: str) -> None:
    with pytest.raises(RuntimeYamlImportError, match=r"row_unions\[0\]\.on_success must be a non-empty string"):
        composition_state_from_runtime_yaml(
            f"""
row_unions:
  - name: variants
    branches: [control, treatment]
{on_success_line}
"""
        )


def test_composition_state_from_runtime_yaml_rejects_row_union_input_mismatch() -> None:
    with pytest.raises(RuntimeYamlImportError, match=r"row_unions\[0\]\.input must match its first branch input 'control'"):
        composition_state_from_runtime_yaml(
            """
row_unions:
  - name: variants
    branches: [control, treatment]
    input: other
    on_success: compared
"""
        )


@pytest.mark.parametrize("timeout_yaml", ["true", ".nan", ".inf", "-.inf", "0", "-1", "slow"])
def test_composition_state_from_runtime_yaml_rejects_invalid_row_union_timeout(timeout_yaml: str) -> None:
    with pytest.raises(
        RuntimeYamlImportError,
        match=r"row_unions\[0\]\.timeout_seconds must be a finite positive number",
    ):
        composition_state_from_runtime_yaml(
            f"""
row_unions:
  - name: variants
    branches: [control, treatment]
    on_success: compared
    timeout_seconds: {timeout_yaml}
"""
        )


def test_composition_state_from_runtime_yaml_maps_oversized_row_union_timeout_to_import_error() -> None:
    oversized_integer = "9" * 400

    with pytest.raises(
        RuntimeYamlImportError,
        match=r"row_unions\[0\]\.timeout_seconds must be a finite positive number",
    ):
        composition_state_from_runtime_yaml(
            f"""
row_unions:
  - name: variants
    branches: [control, treatment]
    on_success: compared
    timeout_seconds: {oversized_integer}
"""
        )


@pytest.mark.parametrize("field", ["plugin", "options", "policy", "merge", "condition", "unexpected"])
def test_composition_state_from_runtime_yaml_rejects_extra_or_inapplicable_row_union_fields(field: str) -> None:
    with pytest.raises(RuntimeYamlImportError, match=rf"row_unions\[0\] contains unknown or inapplicable field\(s\): \['{field}'\]"):
        composition_state_from_runtime_yaml(
            f"""
row_unions:
  - name: variants
    branches: [control, treatment]
    on_success: compared
    {field}: invalid
"""
        )


def test_composition_state_from_runtime_yaml_rejects_aliases() -> None:
    """Hardening: anchors/aliases are rejected outright (billion-laughs defense).

    A small document can otherwise compose into a vastly larger logical
    structure via alias object-sharing once something downstream (dict()
    copies, state.to_dict(), JSON persistence) walks every reference.
    """
    aliased = """
sources:
  source: &src
    plugin: csv
    on_success: out
    options:
      path: /data/blobs/input.csv
      on_validation_failure: discard
sinks:
  out:
    plugin: csv
    on_write_failure: discard
also: *src
"""
    with pytest.raises(RuntimeYamlImportError, match=r"^YAML parse failed: \w+Error$"):
        composition_state_from_runtime_yaml(aliased)


def test_composition_state_from_runtime_yaml_rejects_deeply_nested_document() -> None:
    """Hardening: deep-but-textually-small nesting must not crash with RecursionError.

    ~500 levels of ``a:\\n  a:\\n    a:\\n ...`` fits comfortably under
    MAX_RUNTIME_YAML_IMPORT_CHARS but exhausts CPython's default recursion
    limit inside PyYAML's pure-Python composer/constructor if uncaught.
    """
    depth = 500
    lines = ["  " * i + "a:" for i in range(depth)]
    lines.append("  " * depth + "1")
    deeply_nested = "\n".join(lines)
    assert len(deeply_nested) < MAX_RUNTIME_YAML_IMPORT_CHARS
    with pytest.raises(RuntimeYamlImportError, match=r"^YAML parse failed: \w+Error$"):
        composition_state_from_runtime_yaml(deeply_nested)


def test_composition_state_from_runtime_yaml_rejects_inline_blob_ref() -> None:
    with pytest.raises(RuntimeYamlImportError, match="blob_ref must be supplied via source_blob_ids"):
        composition_state_from_runtime_yaml(
            """
sources:
  source:
    plugin: csv
    on_success: out
    options:
      path: /data/blobs/input.csv
      blob_ref: 98b1357d-5aab-4fb3-85b4-5ad643912e84
sinks:
  out:
    plugin: csv
"""
        )


def test_composition_state_from_runtime_yaml_allows_terminal_aggregation() -> None:
    state = composition_state_from_runtime_yaml(
        """
sources:
  source:
    plugin: csv
    on_success: batch_in
    options:
      path: /data/blobs/input.csv
      on_validation_failure: discard
aggregations:
  - name: batch
    plugin: batch_top_k
    input: batch_in
    on_error: discard
    options:
      field: category
sinks:
  out:
    plugin: csv
    on_write_failure: discard
"""
    )

    assert state.nodes[0].node_type == "aggregation"
    assert state.nodes[0].on_success is None


def test_composition_state_from_runtime_yaml_preserves_optional_gate_error_route() -> None:
    state = composition_state_from_runtime_yaml(
        """
sources:
  source:
    plugin: csv
    on_success: gate_in
    options:
      path: /data/blobs/input.csv
      on_validation_failure: discard
gates:
  - name: threshold
    input: gate_in
    condition: row['amount'] > 500
    routes:
      true: high
      false: standard
    on_error: gate_errors
sinks:
  high:
    plugin: csv
    on_write_failure: discard
  standard:
    plugin: csv
    on_write_failure: discard
  gate_errors:
    plugin: csv
    on_write_failure: discard
"""
    )

    assert state.nodes[0].node_type == "gate"
    assert state.nodes[0].on_error == "gate_errors"


def test_composition_state_from_runtime_yaml_defaults_omitted_gate_error_route_to_none() -> None:
    state = composition_state_from_runtime_yaml(
        """
sources:
  source:
    plugin: csv
    on_success: gate_in
    options:
      path: /data/blobs/input.csv
      on_validation_failure: discard
gates:
  - name: threshold
    input: gate_in
    condition: row['amount'] > 500
    routes:
      true: high
      false: standard
sinks:
  high:
    plugin: csv
    on_write_failure: discard
  standard:
    plugin: csv
    on_write_failure: discard
"""
    )

    assert state.nodes[0].on_error is None


def test_composition_state_from_runtime_yaml_rejects_missing_transform_error_route() -> None:
    with pytest.raises(RuntimeYamlImportError, match=r"transforms\[0\]\.on_error"):
        composition_state_from_runtime_yaml(
            """
sources:
  source:
    plugin: csv
    on_success: transform_in
    options:
      path: /data/blobs/input.csv
      on_validation_failure: discard
transforms:
  - name: normalize
    plugin: field_mapper
    input: transform_in
    on_success: out
    options:
      mapping:
        body: text
sinks:
  out:
    plugin: csv
    on_write_failure: discard
"""
        )


def test_composition_state_from_runtime_yaml_rejects_missing_aggregation_error_route() -> None:
    with pytest.raises(RuntimeYamlImportError, match=r"aggregations\[0\]\.on_error"):
        composition_state_from_runtime_yaml(
            """
sources:
  source:
    plugin: csv
    on_success: batch_in
    options:
      path: /data/blobs/input.csv
      on_validation_failure: discard
aggregations:
  - name: batch
    plugin: batch_top_k
    input: batch_in
    on_success: out
    options:
      field: category
sinks:
  out:
    plugin: csv
    on_write_failure: discard
"""
        )


def test_composition_state_from_runtime_yaml_rejects_missing_sink_write_failure_route() -> None:
    with pytest.raises(RuntimeYamlImportError, match=r"sinks\.out\.on_write_failure"):
        composition_state_from_runtime_yaml(
            """
sources:
  source:
    plugin: csv
    on_success: out
    options:
      path: /data/blobs/input.csv
      on_validation_failure: discard
sinks:
  out:
    plugin: csv
"""
        )


def test_composition_state_from_runtime_yaml_rejects_unpreservable_coalesce_fields() -> None:
    with pytest.raises(RuntimeYamlImportError, match="unsupported coalesce field"):
        composition_state_from_runtime_yaml(
            """
sources:
  source:
    plugin: csv
    on_success: a
    options:
      path: /data/blobs/input.csv
      on_validation_failure: discard
coalesce:
  - name: joined
    branches:
      - a
      - b
    policy: quorum
    merge: nested
    quorum_count: 2
sinks:
  out:
    plugin: csv
"""
        )


def test_composition_state_from_runtime_yaml_preserves_coalesce_timeout() -> None:
    state = composition_state_from_runtime_yaml(
        """
coalesce:
  - name: joined
    branches: [a, b]
    policy: require_all
    merge: nested
    timeout_seconds: 4.25
"""
    )

    assert state.nodes[0].node_type == "coalesce"
    assert state.nodes[0].timeout_seconds == 4.25


def test_composition_state_from_runtime_yaml_maps_oversized_coalesce_timeout_to_import_error() -> None:
    oversized_integer = "9" * 400

    with pytest.raises(
        RuntimeYamlImportError,
        match=r"coalesce\[0\]\.timeout_seconds must be a finite positive number",
    ):
        composition_state_from_runtime_yaml(
            f"""
coalesce:
  - name: joined
    branches: [a, b]
    policy: require_all
    merge: nested
    timeout_seconds: {oversized_integer}
"""
        )


def test_composition_state_from_runtime_yaml_round_trips_runtime_sections() -> None:
    pipeline_yaml = """
sources:
  source:
    plugin: csv
    on_success: gate_in
    options:
      path: /data/blobs/input.csv
      schema:
        mode: observed
      on_validation_failure: discard
transforms:
  - name: normalize
    plugin: field_mapper
    input: gate_in
    on_success: main
    on_error: discard
    options:
      mapping:
        body: text
      schema:
        mode: observed
gates:
  - name: split
    input: main
    condition: text != ''
    routes:
      true: accepted
      false: rejected
aggregations:
  - name: batch
    plugin: batch_top_k
    input: accepted
    on_success: batch_out
    on_error: discard
    trigger:
      count: 10
    options:
      field: category
coalesce:
  - name: joined
    branches:
      - batch_out
      - rejected
    policy: best_effort
    merge: nested
    on_success: audit
sinks:
  audit:
    plugin: json
    options:
      path: outputs/audit.json
    on_write_failure: discard
  rejected:
    plugin: csv
    options:
      path: outputs/rejected.csv
    on_write_failure: discard
"""

    state = composition_state_from_runtime_yaml(pipeline_yaml)
    exported = yaml.safe_load(generate_yaml(state))

    assert exported["sources"]["source"]["plugin"] == "csv"
    assert exported["transforms"][0]["name"] == "normalize"
    assert exported["gates"][0]["routes"] == {"true": "accepted", "false": "rejected"}
    assert exported["aggregations"][0]["trigger"] == {"count": 10}
    assert exported["coalesce"][0]["on_success"] == "audit"
    assert set(exported["sinks"]) == {"audit", "rejected"}


def test_composition_state_from_runtime_yaml_reimports_generate_public_yaml_output() -> None:
    """Hardening regression guard: T-1's whole point is the export -> import
    round trip, so the hardening added in this pass (no-alias pre-scan,
    recursion-safety, non-pipeline-mapping gate) must not break re-importing
    what the composer's own public export (``generate_public_yaml``, the
    function ``GET /state/yaml`` actually calls) produces.

    Also empirically confirms ``generate_public_yaml`` never emits YAML
    anchors/aliases for a representative multi-node/multi-sink state --
    if it ever did (e.g. from a future change that shares an options dict
    object across nodes), the no-alias pre-scan added in this pass would
    reject the composer's own export, and this test would catch it.
    """
    state = CompositionState(
        version=1,
        sources={
            "source": SourceSpec(
                plugin="csv",
                on_success="gate_in",
                options={"path": "/data/blobs/input.csv"},
                on_validation_failure="discard",
            ),
        },
        nodes=(
            NodeSpec(
                id="normalize",
                node_type="transform",
                plugin="field_mapper",
                input="gate_in",
                on_success="main",
                on_error="discard",
                options={"mapping": {"body": "text"}},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
            NodeSpec(
                id="split",
                node_type="gate",
                plugin=None,
                input="main",
                on_success=None,
                on_error=None,
                options={},
                condition="text != ''",
                routes={"true": "accepted", "false": "rejected"},
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
            NodeSpec(
                id="batch",
                node_type="aggregation",
                plugin="batch_top_k",
                input="accepted",
                on_success="batch_out",
                on_error="discard",
                options={"field": "category"},
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
            OutputSpec(name="audit", plugin="json", options={"path": "outputs/audit.json"}, on_write_failure="discard"),
            OutputSpec(name="rejected", plugin="csv", options={"path": "outputs/rejected.csv"}, on_write_failure="discard"),
        ),
        metadata=PipelineMetadata(name="Round trip test", description=""),
    )

    exported_yaml = generate_public_yaml(state)

    # No anchor/alias markers -- confirms the no-alias pre-scan's rejection
    # of aliases (added for the billion-laughs defense) has no cost against
    # the composer's own export shape.
    assert "&" not in exported_yaml.split("\n")[0:1][0]  # sanity: not YAML-document-start noise
    assert not any(line.strip().split(":")[-1].strip().startswith(("&", "*")) for line in exported_yaml.splitlines() if ":" in line)

    reimported = composition_state_from_runtime_yaml(exported_yaml)
    assert set(reimported.sources) == {"source"}
    assert [n.id for n in reimported.nodes] == ["normalize", "split", "batch"]
    assert {o.name for o in reimported.outputs} == {"audit", "rejected"}


def test_composition_state_from_runtime_yaml_stages_llm_review_requirements() -> None:
    """Imported LLM nodes carry the same default pending review requirements
    every composer mutation path stages (elspeth-ae5160c3cb). Without them the
    run gate blocks fail-closed (requirement-None enumerator branch) while the
    interpretation-event writer boundary refuses to surface a resolvable card,
    leaving the imported pipeline permanently blocked."""
    state = composition_state_from_runtime_yaml(
        """
sources:
  source:
    plugin: csv
    on_success: score
    options:
      schema:
        mode: observed
transforms:
- name: score
  plugin: llm
  input: source
  on_success: main
  on_error: discard
  options:
    model: anthropic/claude-haiku-4.5
    prompt_template: 'Score this: {{ row.value }}'
sinks:
  main:
    plugin: csv
    options:
      path: outputs/out.csv
    on_write_failure: discard
"""
    )
    node = next(n for n in state.nodes if n.plugin == "llm")
    requirements = {r["kind"]: r for r in node.options["interpretation_requirements"]}
    assert set(requirements) == {"llm_prompt_template", "llm_model_choice"}
    pt = requirements["llm_prompt_template"]
    assert pt["status"] == "pending"
    assert pt["draft"] == "Score this: {{ row.value }}"
    assert pt["user_term"] == "llm_prompt_template:score"
    mc = requirements["llm_model_choice"]
    assert mc["status"] == "pending"
    assert mc["draft"] == "anthropic/claude-haiku-4.5"
    assert mc["user_term"] == "llm_model_choice:score"


def test_composition_state_from_runtime_yaml_does_not_stage_reviews_on_non_llm_nodes() -> None:
    """The auto-stagers are LLM-scoped no-ops: a non-llm transform imports
    with its options untouched."""
    state = composition_state_from_runtime_yaml(
        """
transforms:
- name: normalize
  plugin: field_normalizer
  input: source
  on_success: main
  on_error: discard
  options:
    field: category
"""
    )
    node = next(n for n in state.nodes if n.id == "normalize")
    assert node.options == {"field": "category"}


# --- Structural queue fan-in import (elspeth-a5b86149d4 / elspeth-6421ffa028) ---


def _sole_queue(state: CompositionState) -> NodeSpec:
    queues = [node for node in state.nodes if node.node_type == "queue"]
    assert len(queues) == 1, [node.node_type for node in state.nodes]
    return queues[0]


def test_queues_from_runtime_mapping_rejects_non_mapping_section() -> None:
    """Trust-boundary test_ref: a non-mapping ``queues`` value is rejected."""
    with pytest.raises(RuntimeYamlImportError, match="queues must be a mapping"):
        _queues_from_runtime_mapping(["not", "a", "mapping"])


def test_queues_from_runtime_mapping_returns_empty_for_missing_section() -> None:
    assert _queues_from_runtime_mapping(None) == []


def test_composition_state_from_runtime_yaml_recognizes_queues_only_section() -> None:
    """``queues`` is a first-class pipeline section: a document defining only a
    queue is a pipeline export, not an empty (destructive-replace) import."""
    state = composition_state_from_runtime_yaml("queues:\n  inbound: {}\n")
    queue = _sole_queue(state)
    assert queue.id == "inbound"
    assert queue.input == "inbound"


def test_composition_state_from_runtime_yaml_imports_empty_queue() -> None:
    state = composition_state_from_runtime_yaml(
        """
sources:
  orders:
    plugin: csv
    on_success: inbound
    options:
      schema:
        mode: observed
queues:
  inbound: {}
transforms:
- name: normalize
  plugin: passthrough
  input: inbound
  on_success: main
  on_error: discard
  options:
    schema:
      mode: observed
sinks:
  main:
    plugin: csv
    options:
      path: outputs/out.csv
    on_write_failure: discard
"""
    )
    queue = _sole_queue(state)
    assert queue.id == "inbound"
    assert queue.node_type == "queue"
    assert queue.plugin is None
    assert queue.input == "inbound"
    assert queue.on_success is None
    assert queue.options == {}
    assert queue_node_contract_error(queue) is None


def test_composition_state_from_runtime_yaml_imports_queue_with_description() -> None:
    state = composition_state_from_runtime_yaml(
        """
queues:
  inbound:
    description: Orders and refunds interleave here
"""
    )
    queue = _sole_queue(state)
    assert queue.options == {"description": "Orders and refunds interleave here"}
    assert queue_node_contract_error(queue) is None


def test_composition_state_from_runtime_yaml_rejects_non_mapping_queues_value() -> None:
    with pytest.raises(RuntimeYamlImportError, match="queues must be a mapping"):
        composition_state_from_runtime_yaml("queues:\n  - inbound\n")


def test_composition_state_from_runtime_yaml_rejects_non_mapping_queue_entry() -> None:
    with pytest.raises(RuntimeYamlImportError, match=r"queues\.inbound must be a mapping"):
        composition_state_from_runtime_yaml("queues:\n  inbound:\n    - not\n    - a\n    - mapping\n")


def test_composition_state_from_runtime_yaml_rejects_empty_queue_name() -> None:
    with pytest.raises(RuntimeYamlImportError, match="queues keys must be non-empty strings"):
        composition_state_from_runtime_yaml("queues:\n  '': {}\n")


def test_composition_state_from_runtime_yaml_rejects_unknown_queue_field() -> None:
    with pytest.raises(RuntimeYamlImportError, match=r"queues\.inbound contains unknown field\(s\): \['priority'\]"):
        composition_state_from_runtime_yaml("queues:\n  inbound:\n    priority: 5\n")


def test_composition_state_from_runtime_yaml_rejects_mixed_type_unknown_queue_fields() -> None:
    with pytest.raises(RuntimeYamlImportError, match=r"queues\.inbound contains unknown field\(s\): \[1, 'priority'\]"):
        composition_state_from_runtime_yaml("queues:\n  inbound:\n    priority: 5\n    1: invalid\n")


def test_composition_state_from_runtime_yaml_rejects_non_string_queue_description() -> None:
    with pytest.raises(RuntimeYamlImportError, match=r"queues\.inbound\.description must be a string"):
        composition_state_from_runtime_yaml("queues:\n  inbound:\n    description: 7\n")


def test_composition_state_from_runtime_yaml_imports_multi_source_queue_example() -> None:
    """The shipped fan-in example imports with its queue preserved and validates."""
    example = _REPO_ROOT / "examples" / "multi_source_queue" / "settings.yaml"
    state = composition_state_from_runtime_yaml(example.read_text(encoding="utf-8"))

    queue = _sole_queue(state)
    assert queue.id == "inbound"
    assert set(state.sources) == {"orders", "refunds"}
    assert [source.on_success for source in state.sources.values()] == ["inbound", "inbound"]

    result = state.validate()
    assert result.is_valid, [entry.message for entry in result.errors]


def test_composition_state_from_runtime_yaml_queue_options_unchanged_by_review_stamp() -> None:
    """Drift guard: the imported-node review-stamp map is a pure no-op for a
    queue (plugin=None), so its options survive byte-for-byte and it stays a
    canonical queue (elspeth-ae5160c3cb interplay with elspeth-a5b86149d4)."""
    bare = _sole_queue(composition_state_from_runtime_yaml("queues:\n  inbound: {}\n"))
    assert dict(bare.options) == {}
    assert queue_node_contract_error(bare) is None

    described = _sole_queue(composition_state_from_runtime_yaml("queues:\n  inbound:\n    description: interleave point\n"))
    assert dict(described.options) == {"description": "interleave point"}
    assert queue_node_contract_error(described) is None


# ---------------------------------------------------------------------------
# Every modelled section has an EXPLICIT outcome (elspeth-9482eda744)
# ---------------------------------------------------------------------------

# THE AUTHORITY. Deriving the parametrisation from the settings model — rather
# than restating a section list here — is what makes a newly added
# ElspethSettings section arrive as a NEW failing test case instead of being
# silently dropped by an importer nobody remembered to update.
_ELSPETH_SETTINGS_SECTIONS = frozenset(ElspethSettings.model_fields)

# One minimal, VALID document fragment per section. Hand-written on purpose:
# these are the probe inputs, not a restatement of the importer's own list, and
# each must be independently valid for its section's schema.
_SECTION_PROBE_YAML: dict[str, str] = {
    "aggregations": "aggregations:\n- name: agg\n  plugin: batch_stats\n  input: out\n  on_success: out\n  on_error: discard\n  options:\n    schema:\n      mode: observed\n",
    "checkpoint": "checkpoint:\n  enabled: true\n",
    "coalesce": "coalesce:\n- name: co\n  branches: {}\n  on_success: out\n",
    "collection_probes": "collection_probes:\n- collection: c\n  plugin: chroma\n  options: {}\n",
    "collectors": "collectors:\n- name: col\n  plugin: passthrough\n  input: out\n  on_success: out\n  on_error: discard\n  options: {}\n",
    "commencement_gates": 'commencement_gates:\n- name: ready\n  condition: "1 > 0"\n',
    "concurrency": "concurrency:\n  max_workers: 2\n",
    "default_llm_profile": "default_llm_profile: fast\n",
    "depends_on": "depends_on:\n- name: dep\n  path: other.yaml\n",
    "gates": "gates:\n- name: g\n  input: out\n  condition: \"True\"\n  routes:\n    'true': out\n",
    "landscape": "landscape:\n  url: sqlite:///runs/audit.db\n",
    "llm_profiles": "llm_profiles:\n  fast:\n    provider: openrouter\n    model: test\n",
    "max_bound_region_depth": "max_bound_region_depth: 3\n",
    "payload_store": "payload_store:\n  backend: filesystem\n",
    "queues": "queues:\n  inbound: {}\n",
    "rate_limit": "rate_limit:\n  requests_per_minute: 60\n",
    "replay_from": "replay_from: run-123\n",
    "retry": "retry:\n  max_attempts: 2\n",
    "row_unions": "row_unions:\n- name: ru\n  branches: []\n  on_success: out\n",
    "run_mode": "run_mode: normal\n",
    "scopes": "scopes:\n- name: sc\n  opener: out\n  closer: out\n",
    "sinks": "",  # already present in the base document
    "sources": "",  # already present in the base document
    "telemetry": "telemetry:\n  enabled: false\n",
    "transforms": "transforms:\n- name: t\n  plugin: passthrough\n  input: out\n  on_success: out\n  on_error: discard\n  options:\n    schema:\n      mode: observed\n",
}


def _section_survived(state: CompositionState, section: str) -> bool:
    """Did the section leave an observable trace in the imported state?

    Deliberately structural rather than a whitelist of section names: the
    question this test asks is "is it REPRESENTED", and the composition state
    represents a section as sources, nodes, or outputs.
    """
    if section in {"sources", "source"}:
        return bool(state.sources)
    if section == "sinks":
        return bool(state.outputs)
    return bool(state.nodes) and any(_node_matches_section(node, section) for node in state.nodes)


def _node_matches_section(node: NodeSpec, section: str) -> str | bool:
    """A node kind bearing this section's name (transforms -> transform, ...)."""
    return node.node_type == section.rstrip("s") or node.node_type == section


def _minimal_pipeline_doc() -> str:
    """A valid one-source, one-sink pipeline the importer accepts."""
    return (
        "sources:\n"
        "  primary:\n"
        "    plugin: csv\n"
        "    on_success: out\n"
        "    options:\n"
        "      path: in.csv\n"
        "      schema:\n"
        "        mode: observed\n"
        "sinks:\n"
        "  out:\n"
        "    plugin: json\n"
        "    on_write_failure: discard\n"
        "    options:\n"
        "      path: out.jsonl\n"
        "      schema:\n"
        "        mode: observed\n"
    )


@pytest.mark.parametrize("section", sorted(_ELSPETH_SETTINGS_SECTIONS))
def test_every_modelled_section_is_imported_or_refused_by_name(section: str) -> None:
    """PER SECTION: a document carrying it either survives import, or is refused NAMING it.

    The defect this pins (elspeth-9482eda744): ``_PIPELINE_SECTION_KEYS`` was a
    hand-written subset of ``ElspethSettings.model_fields``, and every section
    outside it was discarded with no error, no warning, and no partial-import
    signal. A shipped example (examples/chroma_rag_indexed/query_pipeline.yaml)
    lost its ``commencement_gates`` — an admission control gating the run on a
    non-empty corpus — and the import reported SUCCESS.

    Silence reading as fidelity is the defect, not the omission. An importer
    may legitimately decline a section; it may not pretend it was never there.

    WHY PARAMETRISED OVER THE AUTHORITY RATHER THAN A SET COMPARISON: a
    set-vs-set assertion between two hand-written frozensets passes when a
    mutation adds a member to BOTH — the hole that survives in
    ``yaml_generator``'s node-type drift check. Driving the parametrisation
    from ``ElspethSettings.model_fields`` means a NEW settings section arrives
    here as a NEW test case that must be classified, and the behavioural
    assertion below cannot be satisfied by a matching restatement.
    """
    from elspeth.web.composer.yaml_importer import _INTENTIONALLY_DROPPED_SECTIONS

    doc = _minimal_pipeline_doc() + _SECTION_PROBE_YAML[section]

    if section in _INTENTIONALLY_DROPPED_SECTIONS:
        # THIRD OUTCOME. A ratified decision to drop the section is not the
        # defect: ``landscape`` is omitted from the composer's own export on
        # purpose (yaml_generator.py:392, security fix S1), so an imported
        # audit URL must NOT reach composer state, and refusing the document
        # instead would make 81 of the 94 shipped examples unimportable.
        # What the defect actually is, is an UNDOCUMENTED drop — so the bar
        # here is that the exemption cites the decision that authorises it.
        composition_state_from_runtime_yaml(doc)
        assert _INTENTIONALLY_DROPPED_SECTIONS[section].strip(), (
            f"Section {section!r} is dropped deliberately, which is legitimate — but the exemption "
            f"must record WHY, so the next reader can tell a ratified decision from an oversight."
        )
        return

    try:
        state = composition_state_from_runtime_yaml(doc)
    except RuntimeYamlImportError as exc:
        assert section in str(exc), (
            f"Section {section!r} is refused, which is legitimate — but the refusal must NAME it "
            f"so the author knows what was rejected. Got: {exc}"
        )
        return

    # Imported rather than refused: the section's content must be REPRESENTED,
    # not swallowed. A bare "no exception" is exactly the silent-drop defect.
    assert _section_survived(state, section), (
        f"Section {section!r} imported without error but left no trace in the composition state. "
        f"That is the silent drop this test exists to forbid: refuse it by name, or carry it."
    )


def test_a_newly_modelled_section_is_refused_rather_than_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE SIXTEENTH SECTION: an unclassified settings field must not pass through.

    This is the test that makes the DERIVATION load-bearing rather than
    decorative. The parametrised sweep above passes equally well against a
    hand-written list of the fifteen known sections — the very restatement that
    produced this defect — because today those fifteen are the whole set. Only a
    section nobody has classified separates the two implementations.

    Verified by mutation: pointing ``_reject_unimportable_sections`` at
    ``_DECLINED_SECTION_REASONS`` instead of ``ElspethSettings.model_fields``
    leaves the sweep green and turns THIS test red — the new section passes
    straight through and is silently dropped, exactly as the original fifteen
    were.
    """
    from elspeth.core.config import ElspethSettings
    from elspeth.web.composer import yaml_importer

    monkeypatch.setitem(ElspethSettings.model_fields, "brand_new_section", ElspethSettings.model_fields["run_mode"])

    doc = _minimal_pipeline_doc() + "brand_new_section: 1\n"

    with pytest.raises(yaml_importer.RuntimeYamlImportError, match="brand_new_section"):
        composition_state_from_runtime_yaml(doc)
