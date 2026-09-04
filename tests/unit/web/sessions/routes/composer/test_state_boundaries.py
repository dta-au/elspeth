"""Tier-3 boundary tests for the composer state-route helpers.

``_source_options_reference_blob_storage`` reads web-authored source options:
missing or non-string path values are skipped, but a hostile string value
propagates ``resolve_data_path``'s failure (e.g. an embedded NUL byte) rather
than being coerced — the ``@trust_boundary`` honesty test below pins that.
"""

from __future__ import annotations

import pytest

from elspeth.web.sessions.routes.composer.state import _source_options_reference_blob_storage


def test_source_options_reference_blob_storage_raises_on_nul_byte_path(tmp_path) -> None:
    with pytest.raises(ValueError):
        _source_options_reference_blob_storage({"path": "blobs/test-session/x\x00y"}, data_dir=str(tmp_path))


def test_source_options_reference_blob_storage_skips_non_string_values(tmp_path) -> None:
    assert _source_options_reference_blob_storage({"path": 7, "file": None}, data_dir=str(tmp_path)) is False


def test_reject_unbound_blob_storage_sources_raises_400_on_unbound_blob_path(tmp_path) -> None:
    """A source pointing into session blob storage without a blob_ref binding is rejected."""
    from fastapi import HTTPException

    from elspeth.web.composer.state import CompositionState, PipelineMetadata, SourceSpec
    from elspeth.web.sessions.routes.composer.state import _reject_unbound_blob_storage_sources

    state = CompositionState(
        sources={
            "source": SourceSpec(
                plugin="csv",
                on_success="main",
                options={"path": str(tmp_path / "blobs" / "test-session" / "x.csv")},
                on_validation_failure="discard",
            )
        },
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )
    with pytest.raises(HTTPException) as exc_info:
        _reject_unbound_blob_storage_sources(state, data_dir=str(tmp_path))
    assert exc_info.value.status_code == 400


def _single_source_state(options: dict) -> object:
    from elspeth.web.composer.state import CompositionState, PipelineMetadata, SourceSpec

    return CompositionState(
        sources={
            "source": SourceSpec(
                plugin="csv",
                on_success="main",
                options=options,
                on_validation_failure="discard",
            )
        },
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def test_reject_disallowed_source_paths_raises_400_outside_allowlist(tmp_path) -> None:
    """A direct boundary witness pins BOTH arms of the source-path contract.

    Arm 1 (reject): a string path resolving outside the allowlist raises 400.
    Arm 2 (skip): a non-string path value is skipped rather than coerced —
    the arm the boundary's ``suppresses=("R1", "R5")`` covers. Both arms are
    asserted here because the reject arm alone stays green if the
    ``isinstance(value, str)`` gate is deleted or inverted, so it cannot pin
    the skip. ``SourceSpec.__post_init__`` deep-freezes ``options``, so this
    reads the same ``mappingproxy`` the route sees.
    """
    from fastapi import HTTPException

    from elspeth.web.sessions.routes.composer.state import _reject_disallowed_source_paths

    data_dir = tmp_path / "data"
    with pytest.raises(HTTPException) as exc_info:
        _reject_disallowed_source_paths(
            _single_source_state({"path": str(tmp_path / "outside.csv")}),
            data_dir=str(data_dir),
            session_id="test-session",
        )
    assert exc_info.value.status_code == 400
    # Non-string and absent path values are skipped, not coerced and not
    # rejected: an int, a nested mapping, and None all pass cleanly, and so
    # does a source carrying no path option at all.
    for skipped_options in ({"path": 7}, {"file": None}, {"path": {"nested": "x"}}, {}):
        _reject_disallowed_source_paths(
            _single_source_state(skipped_options),
            data_dir=str(data_dir),
            session_id="test-session",
        )
    assert _reject_disallowed_source_paths.__trust_boundary__.test_ref == (  # type: ignore[attr-defined]
        "tests/unit/web/sessions/routes/composer/test_state_boundaries.py::test_reject_disallowed_source_paths_raises_400_outside_allowlist"
    )


def _llm_node_state_with_requirements(requirements: list) -> object:
    from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata

    return CompositionState(
        sources={},
        nodes=(
            NodeSpec(
                id="score",
                node_type="transform",
                plugin="llm",
                input="source",
                on_success="main",
                on_error="discard",
                options={
                    "model": "anthropic/claude-haiku-4.5",
                    "prompt_template": "Score this: {{ row.value }}",
                    "interpretation_requirements": requirements,
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
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def test_reject_malformed_interpretation_requirements_raises_400() -> None:
    """Hand-written requirement rows the interpretation schema would refuse
    are rejected with HTTP 400 BEFORE persistence (elspeth-ae5160c3cb), and
    the error names the node without echoing row content."""
    from fastapi import HTTPException

    from elspeth.web.sessions.routes.composer.state import _reject_malformed_interpretation_requirements

    state = _llm_node_state_with_requirements([{"kind": "not_a_kind", "user_term": "x", "status": "pending"}])
    with pytest.raises(HTTPException) as exc_info:
        _reject_malformed_interpretation_requirements(state)
    assert exc_info.value.status_code == 400
    assert "score" in exc_info.value.detail
    assert "not_a_kind" not in exc_info.value.detail


def test_reject_malformed_source_interpretation_requirements_raises_400() -> None:
    """Source review rows cross the same YAML boundary as node rows."""
    from fastapi import HTTPException

    from elspeth.web.sessions.routes.composer.state import _reject_malformed_interpretation_requirements

    state = _single_source_state({"interpretation_requirements": [{"kind": "not_a_kind", "status": "pending"}]})
    with pytest.raises(HTTPException) as exc_info:
        _reject_malformed_interpretation_requirements(state)
    assert exc_info.value.status_code == 400
    assert "source" in exc_info.value.detail
    assert "not_a_kind" not in exc_info.value.detail


def test_reject_malformed_interpretation_requirements_passes_staged_rows() -> None:
    """Rows shaped like the importer's own auto-stagers pass the gate."""
    from elspeth.web.sessions.routes.composer.state import _reject_malformed_interpretation_requirements

    state = _llm_node_state_with_requirements(
        [
            {
                "id": "prompt_template_review:score",
                "kind": "llm_prompt_template",
                "user_term": "llm_prompt_template:score",
                "status": "pending",
                "draft": "Score this: {{ row.value }}",
            }
        ]
    )
    _reject_malformed_interpretation_requirements(state)


def _marked_export_yaml(marker_line: str) -> str:
    return f"{marker_line}\nsources:\n  source:\n    plugin: csv\n    options: {{}}\n"


def test_redaction_marker_source_names_parses_only_exporter_marker_lines() -> None:
    from elspeth.web.composer.yaml_generator import PUBLIC_EXPORT_REDACTED_SOURCE_MARKER_PREFIX
    from elspeth.web.sessions.routes.composer.state import _redaction_marker_source_names

    text = (
        f"{PUBLIC_EXPORT_REDACTED_SOURCE_MARKER_PREFIX}source stripped=blob_ref,path\n"
        f"{PUBLIC_EXPORT_REDACTED_SOURCE_MARKER_PREFIX}other_source stripped=path\n"
        "# redacted-source: two tokens stripped=path\n"  # multi-token name: ignored
        "# some unrelated comment\n"
        "sources: {}\n"
    )
    assert _redaction_marker_source_names(text) == frozenset({"source", "other_source"})
    assert _redaction_marker_source_names("sources: {}\n") == frozenset()


def test_reject_redacted_sources_without_rebind_400s_path_absent_shape() -> None:
    """The path-ABSENT redacted-export shape gets re-bind guidance, not a raw
    Pydantic 'path Field required' from the strict lane (elspeth-06f92da0d9)."""
    from fastapi import HTTPException

    from elspeth.web.composer.yaml_generator import PUBLIC_EXPORT_REDACTED_SOURCE_MARKER_PREFIX
    from elspeth.web.sessions.routes.composer.state import _reject_redacted_sources_without_rebind

    state = _single_source_state({"schema": {"mode": "observed"}})
    yaml_text = _marked_export_yaml(f"{PUBLIC_EXPORT_REDACTED_SOURCE_MARKER_PREFIX}source stripped=blob_ref,path")

    with pytest.raises(HTTPException) as exc_info:
        _reject_redacted_sources_without_rebind(state, yaml_text=yaml_text)

    assert exc_info.value.status_code == 400
    assert "custody-redacted" in exc_info.value.detail
    assert "source_blob_ids" in exc_info.value.detail


def test_reject_redacted_sources_passes_rebound_and_repathed_sources() -> None:
    from elspeth.web.composer.yaml_generator import PUBLIC_EXPORT_REDACTED_SOURCE_MARKER_PREFIX
    from elspeth.web.sessions.routes.composer.state import _reject_redacted_sources_without_rebind

    yaml_text = _marked_export_yaml(f"{PUBLIC_EXPORT_REDACTED_SOURCE_MARKER_PREFIX}source stripped=blob_ref,path")

    # blob_ref restored by _state_with_imported_source_blobs: bound, passes.
    _reject_redacted_sources_without_rebind(
        _single_source_state({"blob_ref": "20b944e3-fd46-434f-b9a2-4fb508db30f0", "path": "/data/x.csv"}),
        yaml_text=yaml_text,
    )
    # Hand-re-added path option: passes here; path guards police the value.
    _reject_redacted_sources_without_rebind(
        _single_source_state({"path": "inputs/x.csv"}),
        yaml_text=yaml_text,
    )


def test_reject_redacted_sources_is_inert_without_a_marker() -> None:
    from elspeth.web.sessions.routes.composer.state import _reject_redacted_sources_without_rebind

    # Hand-written path-absent YAML with no exporter marker: pre-existing
    # behaviour stands (strict preflight refuses; persists is_valid=False).
    _reject_redacted_sources_without_rebind(
        _single_source_state({"schema": {"mode": "observed"}}),
        yaml_text="sources:\n  source:\n    plugin: csv\n    options: {}\n",
    )
