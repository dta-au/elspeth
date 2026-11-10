"""file_inject transform: what each row gains, and what makes a row fail.

The config half (parsing, refusals, caps, declared types) is covered by
``test_file_inject_config.py``. Everything here happens per row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import yaml

from elspeth.contracts.schema_contract import PipelineRow
from elspeth.plugins.transforms import file_inject
from elspeth.plugins.transforms.file_inject import FileInject
from elspeth.testing import make_pipeline_row
from tests.fixtures.factories import make_source_context

if TYPE_CHECKING:
    from elspeth.contracts.plugin_context import PluginContext
    from elspeth.contracts.schema_contract import SchemaContract

OBSERVED: dict[str, Any] = {"mode": "observed"}

GUIDE_A = "Guidance for kind A.\n"
GUIDE_B = "Guidance for kind B.\n"


def build_all(files: dict[str, Any] | None = None, **extra: Any) -> FileInject:
    if files is None:
        files = {
            "style": {"content": "Be brief.\n", "format": "text", "output_field": "style_guide"},
            "rubric": {"content": '{"max": 5, "bands": [1, 2]}', "format": "json", "output_field": "rubric"},
        }
    return FileInject({"schema": OBSERVED, "mode": "all", "files": files, **extra})


def build_select(files: dict[str, Any] | None = None, **extra: Any) -> FileInject:
    if files is None:
        files = {"A": {"content": GUIDE_A, "format": "text"}, "B": {"content": GUIDE_B, "format": "text"}}
    return FileInject({"schema": OBSERVED, "mode": "select", "key_field": "kind", "output_field": "guidance", "files": files, **extra})


@pytest.fixture
def ctx() -> PluginContext:
    return make_source_context()


def test_all_mode_writes_every_field(ctx: PluginContext) -> None:
    result = build_all().process(make_pipeline_row({"id": 1}), ctx)

    assert result.status == "success"
    assert result.row is not None
    assert result.row.to_dict() == {"id": 1, "style_guide": "Be brief.\n", "rubric": {"max": 5, "bands": [1, 2]}}
    assert result.success_reason == {
        "action": "enriched",
        "fields_added": ["rubric", "style_guide"],
        "metadata": {"mode": "all"},
    }


def test_select_writes_matching_entry(ctx: PluginContext) -> None:
    transform = build_select()

    result = transform.process(make_pipeline_row({"id": 1, "kind": "B"}), ctx)

    assert result.status == "success"
    assert result.row is not None
    assert result.row.to_dict() == {"id": 1, "kind": "B", "guidance": GUIDE_B}
    assert result.success_reason == {
        "action": "enriched",
        "fields_added": ["guidance"],
        "metadata": {"mode": "select", "matched": True, "on_miss": "fail"},
    }


def test_select_miss_fail_is_row_error_listing_entries(ctx: PluginContext) -> None:
    result = build_select().process(make_pipeline_row({"kind": "gloves"}), ctx)

    assert result.status == "error"
    assert result.retryable is False
    assert result.reason is not None
    assert result.reason["reason"] == "file_inject_miss"
    assert result.reason["field"] == "kind"
    assert "['A', 'B']" in result.reason["error"]
    # The key is row data and the guidance is bound content: neither is quoted.
    assert "gloves" not in repr(result.reason)
    assert "Guidance" not in repr(result.reason)


def test_select_miss_null_and_default(ctx: PluginContext) -> None:
    nulled = build_select(on_miss="null").process(make_pipeline_row({"kind": "gloves"}), ctx)

    assert nulled.status == "success"
    assert nulled.row is not None
    assert nulled.row.to_dict() == {"kind": "gloves", "guidance": None}
    assert nulled.success_reason is not None
    assert nulled.success_reason["metadata"] == {"mode": "select", "matched": False, "on_miss": "null"}

    defaulted = build_select(on_miss="default", default_content="No guidance.").process(make_pipeline_row({"kind": "gloves"}), ctx)

    assert defaulted.status == "success"
    assert defaulted.row is not None
    assert defaulted.row.to_dict() == {"kind": "gloves", "guidance": "No guidance."}
    assert defaulted.success_reason is not None
    assert defaulted.success_reason["fields_added"] == ["guidance"]
    assert defaulted.success_reason["metadata"] == {"mode": "select", "matched": False, "on_miss": "default"}


@pytest.mark.parametrize("key", [" A", "A ", "a"])
def test_select_case_and_whitespace_are_misses(ctx: PluginContext, key: str) -> None:
    files = {"A": {"content": GUIDE_A, "format": "text"}}

    failed = build_select(files).process(make_pipeline_row({"kind": key}), ctx)
    assert failed.status == "error"
    assert failed.reason is not None
    assert failed.reason["reason"] == "file_inject_miss"

    nulled = build_select(files, on_miss="null").process(make_pipeline_row({"kind": key}), ctx)
    assert nulled.row is not None
    assert nulled.row["guidance"] is None


@pytest.mark.parametrize("miss_policy", [{}, {"on_miss": "null"}, {"on_miss": "default", "default_content": "fallback"}])
def test_select_missing_key_field_is_error_regardless_of_on_miss(ctx: PluginContext, miss_policy: dict[str, Any]) -> None:
    result = build_select(**miss_policy).process(make_pipeline_row({"id": 1}), ctx)

    assert result.status == "error"
    assert result.retryable is False
    assert result.reason is not None
    assert result.reason["reason"] == "missing_field"
    assert result.reason["field"] == "kind"


@pytest.mark.parametrize("miss_policy", [{}, {"on_miss": "null"}, {"on_miss": "default", "default_content": "fallback"}])
@pytest.mark.parametrize(("key", "type_name"), [(1, "int"), (None, "NoneType"), (True, "bool")])
def test_select_non_string_key_is_error_regardless_of_on_miss(
    ctx: PluginContext, miss_policy: dict[str, Any], key: object, type_name: str
) -> None:
    # Entry names that spell the non-string keys: a coercing match would hit.
    files = {name: {"content": GUIDE_A, "format": "text"} for name in ("1", "None", "True")}

    result = build_select(files, **miss_policy).process(make_pipeline_row({"kind": key}), ctx)

    assert result.status == "error"
    assert result.retryable is False
    assert result.reason is not None
    assert result.reason["reason"] == "type_mismatch"
    assert result.reason["field"] == "kind"
    assert result.reason["expected"] == "str"
    assert result.reason["actual"] == type_name


def test_output_field_already_on_row_is_row_error(ctx: PluginContext) -> None:
    selected = build_select().process(make_pipeline_row({"kind": "A", "guidance": "the row's own text"}), ctx)

    assert selected.status == "error"
    assert selected.retryable is False
    assert selected.reason is not None
    assert selected.reason["reason"] == "invalid_input"
    assert selected.reason["field"] == "guidance"
    assert "the row's own text" not in repr(selected.reason)

    every = build_all().process(make_pipeline_row({"id": 1, "rubric": None}), ctx)

    assert every.status == "error"
    assert every.reason is not None
    assert every.reason["reason"] == "invalid_input"
    assert every.reason["field"] == "rubric"
    assert "Be brief" not in repr(every.reason)


@pytest.fixture
def handed_rows(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The plain dicts the transform hands to ``PipelineRow``, in order.

    ``PipelineRow`` detaches and freezes what it is given, so an emitted row
    cannot show whether two rows were handed the same object. This records the
    data one step earlier, where sharing is still visible.
    """
    handed: list[dict[str, Any]] = []

    def recording_row(data: dict[str, Any], contract: SchemaContract) -> PipelineRow:
        handed.append(data)
        return PipelineRow(data, contract)

    monkeypatch.setattr(file_inject, "PipelineRow", recording_row)
    return handed


def test_rows_do_not_share_mutable_values(ctx: PluginContext, handed_rows: list[dict[str, Any]]) -> None:
    files = {
        "rubric": {"content": '{"bands": [1, 2]}', "format": "json", "output_field": "rubric"},
        "lines": {"content": '{"n": 1}\n', "format": "jsonl", "output_field": "lines"},
    }
    transform = build_all(files)

    transform.process(make_pipeline_row({"id": 1}), ctx)
    handed_rows[0]["rubric"]["bands"].append(99)
    handed_rows[0]["lines"][0]["n"] = 99
    second = transform.process(make_pipeline_row({"id": 2}), ctx)

    assert handed_rows[1]["rubric"] == {"bands": [1, 2]}
    assert handed_rows[1]["lines"] == [{"n": 1}]
    assert second.row is not None
    assert second.row.to_dict() == {"id": 2, "rubric": {"bands": [1, 2]}, "lines": [{"n": 1}]}


def test_select_does_not_share_an_entry_or_the_default_between_rows(ctx: PluginContext, handed_rows: list[dict[str, Any]]) -> None:
    transform = build_select(
        {"A": {"content": '{"bands": [1, 2]}', "format": "json"}},
        on_miss="default",
        default_content={"bands": [0]},
    )

    for kind, expected in (("A", {"bands": [1, 2]}), ("gloves", {"bands": [0]})):
        handed_rows.clear()
        transform.process(make_pipeline_row({"kind": kind}), ctx)
        handed_rows[0]["guidance"]["bands"].append(99)
        transform.process(make_pipeline_row({"kind": kind}), ctx)

        assert handed_rows[1]["guidance"] == expected


def test_output_schema_declares_and_guarantees_added_fields() -> None:
    every = build_all()

    assert every.declared_output_fields == frozenset({"style_guide", "rubric"})
    config = every._output_schema_config
    assert config is not None
    assert config.guaranteed_fields == ("rubric", "style_guide")
    assert config.fields is not None
    declared = {field.name: (field.field_type, field.required, field.nullable) for field in config.fields}
    assert declared == {"rubric": ("any", True, False), "style_guide": ("str", True, False)}
    assert {"rubric", "style_guide"} <= set(every.output_schema.model_fields)

    selected = build_select(on_miss="null")

    assert selected.declared_output_fields == frozenset({"guidance"})
    selected_config = selected._output_schema_config
    assert selected_config is not None
    assert selected_config.guaranteed_fields == ("guidance",)
    assert selected_config.fields is not None
    assert [(field.name, field.field_type, field.required, field.nullable) for field in selected_config.fields] == [
        ("guidance", "str", True, True)
    ]
    # key_field is read, output_field is written.
    assert selected.declared_input_fields == frozenset({"kind"})


def test_emitted_contract_carries_the_added_fields(ctx: PluginContext) -> None:
    result = build_select(on_miss="null").process(make_pipeline_row({"kind": "A"}), ctx)

    assert result.row is not None
    contract_field = {field.normalized_name: field for field in result.row.contract.fields}["guidance"]
    assert contract_field.python_type is str
    assert contract_field.required is True
    assert contract_field.nullable is True


def test_probe_config_constructs(ctx: PluginContext) -> None:
    transform = FileInject(FileInject.probe_config())

    assert transform.name == "file_inject"
    # The invariant harness drives the probe transform with these rows; they
    # must reach the success path, or the harness would assert over an error.
    (probe_row,) = transform.forward_invariant_probe_rows(make_pipeline_row({"id": 1}))
    result = transform.process(probe_row, ctx)
    assert result.status == "success"
    assert result.row is not None
    assert set(result.row.to_dict()) == set(probe_row.to_dict()) | transform.declared_output_fields
    assert result.success_reason is not None
    assert result.success_reason["metadata"] == {"mode": "select", "matched": True, "on_miss": "fail"}


def test_probe_rows_are_unchanged_in_mode_all() -> None:
    probe = make_pipeline_row({"id": 1})

    assert build_all().forward_invariant_probe_rows(probe) == [probe]


def test_agent_assistance_teaches_wiring_and_the_upload_rule() -> None:
    assistance = FileInject.get_agent_assistance()

    assert assistance is not None
    assert assistance.plugin_name == "file_inject"
    hints = " ".join(assistance.composer_hints)
    assert "mode: all writes every files entry onto every row" in hints
    assert "mode: select writes one entry" in hints
    assert "never inferred" in hints
    assert "content_file" in hints
    # The composer path, in the order the planner must follow it.
    steps = (
        "tell them to save it as a file and upload it",
        "find the ready upload with list_blobs and get_blob_metadata",
        "attach it with wire_blob_inline_ref at 'node:<node_id>.options.files.<entry_name>.content'",
        # A node cannot exist without content, so creating one carries the marker in the same call.
        'set that entry\'s content to {"blob_ref": "<blob_id>", "mode": "inline_content", "sha256": "<content_hash>"}',
    )
    positions = [hints.find(step) for step in steps]
    assert -1 not in positions, positions
    assert positions == sorted(positions)
    assert "Do not use create_blob to supply file_inject content" in hints
    assert "do not write literal text into content" in hints
    # The composer refuses default_content, so no hint may offer it as a way to continue there.
    assert "The composer does not accept default_content: use on_miss null or fail there" in hints
    assert "or default with default_content, to continue" not in hints
    assert all(len(hint) <= 280 for hint in assistance.composer_hints)
    assert FileInject.get_agent_assistance(issue_code="anything") is None


def test_example_use_is_yaml_that_builds_the_plugin() -> None:
    options = yaml.safe_load(FileInject.example_use)["transform"]["options"]

    assert options["files"]["invoice"]["content"] == "Check the totals against the line items.\n"
    assert options["files"]["contract"]["content"].endswith("signatures.\n")
    transform = FileInject(options)
    result = transform.process(make_pipeline_row({"document_kind": "invoice"}), make_source_context())
    assert result.row is not None
    assert result.row.to_dict()["review_guidance"] == "Check the totals against the line items.\n"
