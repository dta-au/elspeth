"""Implicit-decision report must never record a blob's internal storage path.

elspeth-b5180a9630 (R2-F11): ``build_implicit_decisions_report`` flattens every
source option verbatim into ``composer_meta.implicit_decisions.entries[].value``.
For a blob-backed source that meant the absolute ``/var/lib/elspeth/blobs/...``
storage path entered ``composer_meta`` at WRITE time, downstream of the
``sources``-keyed ``redact_source_storage_path`` projection and outside the
guided-only ``private_path_projections`` pass — so every state response and the
convergence-422 body disclosed it.

The fix is a can't-regress boundary rather than another outbound projection:
when a source's options carry the structural ``blob_ref`` marker, the
storage-path carrier keys are recorded as the ``blob:<blob_ref>`` wire sentinel
(the guided schema_form precedent, ``BLOB_REF_PATH_PREFIX``). A raw path never
enters ``composer_meta``, so no outbound serializer can regress field-by-field.
"""

from __future__ import annotations

import json

import pytest

from elspeth.contracts.freeze import deep_freeze
from elspeth.web.composer.implicit_decisions import build_implicit_decisions_report
from elspeth.web.composer.redaction import REDACTED_BLOB_SOURCE_PATH
from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata, SourceSpec

_BLOB_REF = "9f2b3c1d-4e5a-4b6c-8d7e-0f1a2b3c4d5e"
_STORAGE_PATH = "/var/lib/elspeth/blobs/9f2b3c1d-4e5a-4b6c-8d7e-0f1a2b3c4d5e/input.csv"


def _state_with_source_options(options: dict[str, object], *, plugin: str = "csv") -> CompositionState:
    return CompositionState(
        source=SourceSpec(
            plugin=plugin,
            options=deep_freeze(options),
            on_success="rows",
            on_validation_failure="discard",
        ),
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(name="blob source"),
        version=2,
    )


def _entries_by_path(state: CompositionState) -> dict[str, dict[str, object]]:
    report = build_implicit_decisions_report(state)
    return {str(entry["path"]): dict(entry) for entry in report["entries"]}


def test_blob_backed_source_path_records_blob_ref_sentinel() -> None:
    """A ``path`` carrier alongside ``blob_ref`` is recorded as ``blob:<ref>``."""
    by_path = _entries_by_path(_state_with_source_options({"blob_ref": _BLOB_REF, "path": _STORAGE_PATH}))

    assert by_path["source.path"]["value"] == f"blob:{_BLOB_REF}"
    # The marker itself is unchanged — it was never the leak, and
    # ``redact_source_storage_path`` has never masked it either.
    assert by_path["source.blob_ref"]["value"] == _BLOB_REF


def test_blob_backed_source_file_carrier_records_blob_ref_sentinel() -> None:
    """``file`` is the equivalent storage-path carrier and is masked too."""
    by_path = _entries_by_path(_state_with_source_options({"blob_ref": _BLOB_REF, "file": _STORAGE_PATH}))

    assert by_path["source.file"]["value"] == f"blob:{_BLOB_REF}"


def test_blob_backed_source_report_serializes_without_the_storage_path() -> None:
    """No serialization of the whole report may contain the raw path."""
    state = _state_with_source_options(
        {
            "blob_ref": _BLOB_REF,
            "path": _STORAGE_PATH,
            "file": _STORAGE_PATH,
            "schema": {"mode": "fixed", "fields": ["url: str"]},
        }
    )

    serialized = json.dumps(build_implicit_decisions_report(state))

    assert _STORAGE_PATH not in serialized
    assert "/var/lib/elspeth/blobs/" not in serialized


def test_source_without_blob_ref_keeps_its_path_verbatim() -> None:
    """A manually authored (YAML/operator) source is not blob-backed.

    Its ``path`` is operator-supplied configuration, not an internal storage
    location, and the disclosure report must keep reporting it — mirrors
    ``test_redact_source_storage_path_leaves_manual_file_without_blob_ref``.
    """
    by_path = _entries_by_path(_state_with_source_options({"path": "data/input.csv"}))

    assert by_path["source.path"]["value"] == "data/input.csv"


def test_nested_path_option_is_not_masked() -> None:
    """Only the TOP-LEVEL carriers are blob storage paths.

    A nested ``<something>.path`` is an unrelated plugin option; masking it
    would destroy disclosure content and diverge from the sibling
    ``redact_source_storage_path``, which also masks top-level keys only.
    """
    by_path = _entries_by_path(
        _state_with_source_options(
            {
                "blob_ref": _BLOB_REF,
                "path": _STORAGE_PATH,
                "archive": {"path": "relative/inner.csv"},
            }
        )
    )

    assert by_path["source.path"]["value"] == f"blob:{_BLOB_REF}"
    assert by_path["source.archive.path"]["value"] == "relative/inner.csv"


@pytest.mark.parametrize(
    "blob_ref",
    [
        pytest.param({"nested": "surprise"}, id="mapping"),
        pytest.param(17, id="int"),
        pytest.param("", id="empty-string"),
        pytest.param("not-a-uuid", id="non-uuid-string"),
        pytest.param(_STORAGE_PATH, id="path-shaped-string"),
        pytest.param(f"../../{_BLOB_REF}", id="traversal-shaped-string"),
        pytest.param(_BLOB_REF.upper(), id="non-canonical-uuid-casing"),
    ],
)
def test_blob_ref_that_is_not_a_canonical_uuid_degrades_to_the_generic_sentinel(blob_ref: object) -> None:
    """``options`` is composer/LLM-authored (Tier 3): validate, then degrade.

    A ``str`` check alone is NOT sufficient. Every other consumer of this marker
    requires a canonical UUID (the YAML-export guard, the guided reviewed-source
    reader), and a path-shaped ``blob_ref`` passed through a bare ``str`` check
    would ride out as ``blob:/var/lib/elspeth/blobs/...`` — the leak reopened
    inside the sentinel that exists to close it. Anything failing validation
    must degrade to the generic sentinel rather than be interpolated.
    """
    by_path = _entries_by_path(_state_with_source_options({"blob_ref": blob_ref, "path": _STORAGE_PATH}))

    assert by_path["source.path"]["value"] == REDACTED_BLOB_SOURCE_PATH
    # Degrade, never escalate: a read-side disclosure projection must not turn a
    # corrupt-state anomaly into a 500 on every state read (contrast the
    # write-side custodian in tools/blobs.py, which raises on the same shape).
    assert "/var/lib/elspeth/blobs/" not in json.dumps(by_path["source.path"]["value"])


def test_named_sources_each_project_their_own_blob_ref() -> None:
    """Multiple blob-backed sources collapse onto ``source.path`` by design.

    ``_source_entries`` hard-codes the ``source.`` prefix (the guided
    projection at ``redaction.py`` keys on exactly ``source.path`` /
    ``source.file``). Whichever entry wins, neither may be a raw path.
    """
    other_ref = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
    state = CompositionState(
        sources={
            "left": SourceSpec(
                plugin="csv",
                options=deep_freeze({"blob_ref": _BLOB_REF, "path": _STORAGE_PATH}),
                on_success="left_rows",
                on_validation_failure="discard",
            ),
            "right": SourceSpec(
                plugin="csv",
                options=deep_freeze({"blob_ref": other_ref, "path": f"/var/lib/elspeth/blobs/{other_ref}/right.csv"}),
                on_success="right_rows",
                on_validation_failure="discard",
            ),
        },
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(name="two blob sources"),
        version=2,
    )

    values = {str(entry["value"]) for entry in build_implicit_decisions_report(state)["entries"] if entry["path"] == "source.path"}

    assert values == {f"blob:{_BLOB_REF}", f"blob:{other_ref}"}
    assert "/var/lib/elspeth/blobs/" not in json.dumps(build_implicit_decisions_report(state))


@pytest.mark.parametrize("field", ["provider", "model", "temperature", "pool_size"])
def test_llm_source_model_binding_options_use_model_category(field: str) -> None:
    state = _state_with_source_options(
        {
            "provider": "openrouter",
            "model": "openai/gpt-4o-mini",
            "temperature": 0.2,
            "pool_size": 2,
            "prompt_template": "Write one briefing.",
        },
        plugin="llm",
    )

    entry = _entries_by_path(state)[f"source.{field}"]

    assert entry == {
        "path": f"source.{field}",
        "value": state.sources["source"].options[field],
        "category": "model",
        "provenance": "picked",
    }


def test_llm_source_non_model_options_and_routing_keep_source_contract() -> None:
    state = _state_with_source_options(
        {
            "provider": "openrouter",
            "prompt_template": "Write one briefing.",
            "response_field": "briefing",
            "schema": {"mode": "observed"},
        },
        plugin="llm",
    )

    by_path = _entries_by_path(state)

    assert by_path["source.prompt_template"] == {
        "path": "source.prompt_template",
        "value": "Write one briefing.",
        "category": "source",
        "provenance": "composer_selected",
    }
    assert by_path["source.response_field"]["category"] == "source"
    assert by_path["source.schema.mode"]["category"] == "source"
    assert by_path["source.on_validation_failure"] == {
        "path": "source.on_validation_failure",
        "value": "discard",
        "category": "error_routing",
        "provenance": "picked",
        "candidate_alternatives": ["discard", "named_sink"],
    }


def test_authored_blob_guaranteed_fields_attribute_to_content_not_planner() -> None:
    """The bind-time guarantee stamp exists only on LLM-authored bound blobs
    (SOURCE_AUTHORING_KEY marks the evidence class — John's ruling 2026-08-27),
    and the report must attribute it as ``derived_from_content``."""
    by_path = _entries_by_path(
        _state_with_source_options(
            {
                "blob_ref": _BLOB_REF,
                "path": _STORAGE_PATH,
                "source_authoring": {
                    "modality": "llm_generated",
                    "content_hash": "a" * 64,
                    "review_event_id": None,
                    "resolved_kind": None,
                },
                "schema": {"mode": "observed", "guaranteed_fields": ["colour"]},
            }
        )
    )

    entry = by_path["source.schema.guaranteed_fields"]
    assert entry["provenance"] == "derived_from_content"
    assert "content" in str(entry["note"])
    # The sibling schema key keeps ordinary provenance.
    assert by_path["source.schema.mode"]["provenance"] == "composer_selected"


def test_uploaded_blob_guaranteed_fields_keep_ordinary_provenance() -> None:
    """A verbatim/uploaded bound blob never auto-declares, so a guarantee on
    one is the author's own claim — never attributed to content."""
    by_path = _entries_by_path(
        _state_with_source_options(
            {
                "blob_ref": _BLOB_REF,
                "path": _STORAGE_PATH,
                "schema": {"mode": "observed", "guaranteed_fields": ["colour"]},
            }
        )
    )

    entry = by_path["source.schema.guaranteed_fields"]
    assert entry["provenance"] == "composer_selected"
    assert "note" not in entry


def test_blob_rows_guarantee_attributes_to_plugin_contract() -> None:
    """The blob_rows fixed-row-field stamp is server-derived plugin-contract
    truth (adjudicated 2026-08-27), not the planner's assertion."""
    by_path = _entries_by_path(
        _state_with_source_options(
            {
                "blobs": [
                    {
                        "blob_id": _BLOB_REF,
                        "payload_ref": "a" * 64,
                        "filename": "doc.png",
                        "mime_type": "image/png",
                        "size_bytes": 10,
                    }
                ],
                "schema": {
                    "mode": "observed",
                    "guaranteed_fields": ["blob_id", "blob_ref", "blob_filename", "blob_mime_type", "blob_size_bytes"],
                },
            },
            plugin="blob_rows",
        )
    )

    entry = by_path["source.schema.guaranteed_fields"]
    assert entry["provenance"] == "derived_from_content"
    assert "plugin" in str(entry["note"])


def test_non_blob_source_guaranteed_fields_keep_ordinary_provenance() -> None:
    """Without a blob binding there is no content evidence to attribute."""
    by_path = _entries_by_path(
        _state_with_source_options(
            {
                "path": "/data/input.csv",
                "schema": {"mode": "observed", "guaranteed_fields": ["colour"]},
            }
        )
    )

    entry = by_path["source.schema.guaranteed_fields"]
    assert entry["provenance"] == "composer_selected"
    assert "note" not in entry


def test_server_stamped_source_metadata_attributes_to_the_server() -> None:
    """``source_authoring.*`` and ``interpretation_requirements`` are written
    by ELSPETH's provenance and review machinery, never chosen by the planner
    (elspeth-c67fbbbd83: v11 showed them recorded as ``composer_selected``)."""
    by_path = _entries_by_path(
        _state_with_source_options(
            {
                "blob_ref": _BLOB_REF,
                "path": _STORAGE_PATH,
                "delimiter": ";",
                "source_authoring": {
                    "modality": "llm_generated",
                    "content_hash": "a" * 64,
                    "review_event_id": None,
                    "resolved_kind": None,
                },
                "interpretation_requirements": [
                    {
                        "id": "source_review:inline_source_data",
                        "kind": "invented_source",
                        "user_term": "inline_source_data",
                        "status": "pending",
                        "draft": "a,b\n1,2\n",
                        "event_id": None,
                        "accepted_value": None,
                        "accepted_artifact_hash": None,
                        "resolved_prompt_template_hash": None,
                    }
                ],
                "schema": {"mode": "observed"},
            }
        )
    )

    assert by_path["source.source_authoring.modality"]["provenance"] == "server_stamped"
    assert by_path["source.source_authoring.content_hash"]["provenance"] == "server_stamped"
    assert by_path["source.interpretation_requirements"]["provenance"] == "server_stamped"
    # The path/file sentinel entries keep being emitted verbatim —
    # ``redact_guided_snapshot_storage_paths`` keys on those exact paths.
    assert by_path["source.path"]["value"] == f"blob:{_BLOB_REF}"
    # Sibling ordinary options are untouched.
    assert by_path["source.delimiter"]["provenance"] == "composer_selected"


def test_server_stamped_node_metadata_attributes_to_the_server() -> None:
    """The node-side server-owned keys (``resolved_prompt_template_hash``,
    ``prompt_template_parts``) attribute honestly too, and only when rooted at
    the TOP-LEVEL options segment."""
    state = CompositionState(
        source=None,
        nodes=(
            NodeSpec(
                id="model",
                node_type="transform",
                plugin="llm",
                input="rows",
                on_success="out",
                on_error="discard",
                options=deep_freeze(
                    {
                        "prompt_template": "Tone: warm",
                        "resolved_prompt_template_hash": "b" * 64,
                        "prompt_template_parts": [{"kind": "text", "text": "Tone: warm"}],
                        "nested": {"resolved_prompt_template_hash": "not-server-owned"},
                        "schema": {"mode": "observed"},
                    }
                ),
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(name="node metadata"),
        version=2,
    )
    report = build_implicit_decisions_report(state)
    by_path = {str(entry["path"]): dict(entry) for entry in report["entries"]}

    assert by_path["node.model.options.resolved_prompt_template_hash"]["provenance"] == "server_stamped"
    assert by_path["node.model.options.prompt_template_parts"]["provenance"] == "server_stamped"
    # A NESTED key that merely reuses a server-owned name is an ordinary
    # plugin option — the stamp applies to the top-level options segment only.
    assert by_path["node.model.options.nested.resolved_prompt_template_hash"]["provenance"] == "composer_selected"
    assert by_path["node.model.options.prompt_template"]["provenance"] == "composer_selected"
