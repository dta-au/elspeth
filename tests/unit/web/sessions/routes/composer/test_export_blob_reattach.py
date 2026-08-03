"""Unit tests for _reattach_guided_blob_refs (elspeth-b5ee205720).

A guided blob-backed source has ``blob_ref`` stripped from its committed
options (the manual set_source path can't prove ``path == storage_path``); it
survives only in the schema-8 GuidedSession ``reviewed_sources`` snapshot. Public
export custody verification and path omission key off
``source.options["blob_ref"]``. This helper reconstitutes ``blob_ref`` into the
private export working copy from the snapshot, mirroring the cross-reference in
redact_guided_snapshot_storage_paths; the public response still omits the UUID.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.guided.resolved import SourceResolved
from elspeth.web.composer.guided.state_machine import (
    GuidedSession,
    TerminalKind,
    TerminalReason,
    TerminalState,
)
from elspeth.web.composer.state import CompositionState, PipelineMetadata, SourceSpec
from elspeth.web.composer.yaml_generator import generate_public_pipeline_dict
from elspeth.web.sessions.routes.composer.state import _reattach_guided_blob_refs

BLOB_PATH = "/data/blobs/sess-1/abc12300-0000-4000-8000-000000000000_data.csv"
BLOB_REF = "abc12300-0000-4000-8000-000000000000"


def _guided_with_snapshot(*, blob_ref: str | None, path: str, name: str = "source") -> GuidedSession:
    options: dict[str, object] = {"path": path, "schema": {"mode": "observed"}}
    if blob_ref is not None:
        options["blob_ref"] = blob_ref
    snap = SourceResolved(
        name=name,
        plugin="csv",
        options=options,
        observed_columns=("data.csv",),
        sample_rows=(),
        on_validation_failure="discard",
    )
    stable_id = "11111111-1111-4111-8111-111111111111"
    return replace(GuidedSession.initial(), source_order=(stable_id,), reviewed_sources={stable_id: snap})


def _state(*, source_options: dict[str, object], guided_session: GuidedSession | None) -> CompositionState:
    return CompositionState(
        sources={
            "source": SourceSpec(
                plugin="csv",
                on_success="main",
                options=source_options,
                on_validation_failure="discard",
            )
        },
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
        guided_session=guided_session,
    )


def test_reattaches_blob_ref_from_guided_snapshot() -> None:
    state = _state(
        source_options={"path": BLOB_PATH, "schema": {"mode": "observed"}},
        guided_session=_guided_with_snapshot(blob_ref=BLOB_REF, path=BLOB_PATH),
    )
    out = _reattach_guided_blob_refs(state)
    assert out.sources["source"].options["blob_ref"] == BLOB_REF
    # The reattached working copy keeps the path; omission happens in yaml gen.
    assert out.sources["source"].options["path"] == BLOB_PATH
    # Non-mutating: the input state's source is untouched.
    assert "blob_ref" not in state.sources["source"].options


def test_reattaches_blob_ref_when_reviewed_snapshot_uses_public_blob_sentinel() -> None:
    state = _state(
        source_options={"path": BLOB_PATH, "schema": {"mode": "observed"}},
        guided_session=_guided_with_snapshot(blob_ref=BLOB_REF, path=f"blob:{BLOB_REF}"),
    )

    out = _reattach_guided_blob_refs(state)

    assert out.sources["source"].options == {
        "path": BLOB_PATH,
        "schema": {"mode": "observed"},
        "blob_ref": BLOB_REF,
    }
    public_options = generate_public_pipeline_dict(out)["sources"]["source"]["options"]
    assert "path" not in public_options
    assert "blob_ref" not in public_options
    assert BLOB_REF not in repr(public_options)
    assert "blob_ref" not in state.sources["source"].options


def test_reattaches_blob_ref_from_guided_native_sentinel_without_duplicate_identity_key() -> None:
    state = _state(
        source_options={"path": BLOB_PATH, "schema": {"mode": "observed"}},
        guided_session=_guided_with_snapshot(blob_ref=None, path=f"blob:{BLOB_REF}"),
    )

    out = _reattach_guided_blob_refs(state)

    assert out.sources["source"].options == {
        "path": BLOB_PATH,
        "schema": {"mode": "observed"},
        "blob_ref": BLOB_REF,
    }


@pytest.mark.parametrize(
    "sentinel",
    (
        "blob:",
        "blob:not-a-uuid",
        "blob:ABC12300-0000-4000-8000-000000000000",
    ),
)
def test_rejects_malformed_guided_native_blob_sentinel_without_duplicate_identity_key(
    sentinel: str,
) -> None:
    state = _state(
        source_options={"path": BLOB_PATH},
        guided_session=_guided_with_snapshot(blob_ref=None, path=sentinel),
    )

    with pytest.raises(AuditIntegrityError, match="canonical UUID"):
        _reattach_guided_blob_refs(state)


def test_rejects_guided_native_blob_sentinel_for_absent_live_source_name() -> None:
    state = _state(
        source_options={"path": BLOB_PATH},
        guided_session=_guided_with_snapshot(
            blob_ref=None,
            path=f"blob:{BLOB_REF}",
            name="foreign_source",
        ),
    )

    with pytest.raises(AuditIntegrityError, match="guided blob source mapping"):
        _reattach_guided_blob_refs(state)


def test_rejects_guided_native_blob_sentinel_mixed_with_private_path_carrier() -> None:
    guided = _guided_with_snapshot(blob_ref=None, path=f"blob:{BLOB_REF}")
    stable_id = guided.source_order[0]
    snapshot = guided.reviewed_sources[stable_id]
    guided = replace(
        guided,
        reviewed_sources={
            stable_id: replace(
                snapshot,
                options={
                    **snapshot.options,
                    "file": BLOB_PATH,
                },
            )
        },
    )
    state = _state(
        source_options={"path": BLOB_PATH, "file": BLOB_PATH},
        guided_session=guided,
    )

    with pytest.raises(AuditIntegrityError, match="mixes public sentinels and private paths"):
        _reattach_guided_blob_refs(state)


def test_rejects_public_blob_sentinel_that_differs_from_retained_blob_ref() -> None:
    state = _state(
        source_options={"path": BLOB_PATH},
        guided_session=_guided_with_snapshot(
            blob_ref=BLOB_REF,
            path="blob:def45600-0000-4000-8000-000000000000",
        ),
    )

    with pytest.raises(AuditIntegrityError, match="sentinel and blob_ref differ"):
        _reattach_guided_blob_refs(state)


def test_reattached_state_omits_path_and_blob_identity_from_public_projection() -> None:
    state = _state(
        source_options={"path": BLOB_PATH, "schema": {"mode": "observed"}},
        guided_session=_guided_with_snapshot(blob_ref=BLOB_REF, path=BLOB_PATH),
    )
    out = _reattach_guided_blob_refs(state)
    # Public YAML now omits the storage path (blob_ref present triggers the
    # existing omit) and strips the web-only blob_ref itself.
    doc = generate_public_pipeline_dict(out)
    src_opts = doc["sources"]["source"]["options"]
    assert "path" not in src_opts
    assert "blob_ref" not in src_opts
    # Reattachment remains private input to custody verification and projection.
    assert out.sources["source"].options["blob_ref"] == BLOB_REF
    assert BLOB_REF not in repr(src_opts)


def test_untouched_without_guided_session() -> None:
    state = _state(source_options={"path": BLOB_PATH}, guided_session=None)
    assert _reattach_guided_blob_refs(state) is state


def test_untouched_when_snapshot_has_no_blob_ref() -> None:
    state = _state(
        source_options={"path": BLOB_PATH},
        guided_session=_guided_with_snapshot(blob_ref=None, path=BLOB_PATH),
    )
    assert _reattach_guided_blob_refs(state) is state


def test_rejects_operator_typed_source_reusing_reviewed_name() -> None:
    state = _state(
        source_options={"path": "/tmp/operator/typed.csv"},
        guided_session=_guided_with_snapshot(blob_ref=BLOB_REF, path=BLOB_PATH),
    )

    with pytest.raises(AuditIntegrityError, match="guided blob source mapping"):
        _reattach_guided_blob_refs(state)


def test_exited_guided_session_does_not_leak_freeform_source_replacement() -> None:
    freeform_path = "/tmp/operator/replacement.csv"
    guided = replace(
        _guided_with_snapshot(blob_ref=BLOB_REF, path=BLOB_PATH),
        terminal=TerminalState(
            kind=TerminalKind.EXITED_TO_FREEFORM,
            reason=TerminalReason.USER_PRESSED_EXIT,
            pipeline_yaml=None,
        ),
    )
    state = _state(
        source_options={"path": freeform_path, "schema": {"mode": "observed"}},
        guided_session=guided,
    )

    assert _reattach_guided_blob_refs(state) is state
    public_options = generate_public_pipeline_dict(state)["sources"]["source"]["options"]
    assert public_options == {
        "schema": {"mode": "observed"},
        "on_validation_failure": "discard",
    }
    assert freeform_path not in repr(public_options)


def test_completed_guided_session_retains_reviewed_source_authority() -> None:
    guided = replace(
        _guided_with_snapshot(blob_ref=BLOB_REF, path=BLOB_PATH),
        terminal=TerminalState(
            kind=TerminalKind.COMPLETED,
            reason=None,
            pipeline_yaml="sources: {}\n",
        ),
    )
    state = _state(
        source_options={"path": BLOB_PATH, "schema": {"mode": "observed"}},
        guided_session=guided,
    )

    out = _reattach_guided_blob_refs(state)

    assert out.sources["source"].options["blob_ref"] == BLOB_REF


def test_allows_retired_reviewed_binding_when_name_and_path_are_absent() -> None:
    state = _state(
        source_options={"path": "/tmp/operator/typed.csv"},
        guided_session=_guided_with_snapshot(blob_ref=BLOB_REF, path=BLOB_PATH, name="retired"),
    )

    assert _reattach_guided_blob_refs(state) is state


def test_preserves_source_that_already_has_blob_ref() -> None:
    state = _state(
        source_options={"path": BLOB_PATH, "blob_ref": BLOB_REF},
        guided_session=_guided_with_snapshot(blob_ref=BLOB_REF, path=BLOB_PATH),
    )
    out = _reattach_guided_blob_refs(state)
    # A source that already carries blob_ref needs no reattachment; identity holds.
    assert out is state
    assert out.sources["source"].options["blob_ref"] == BLOB_REF


def test_reattaches_each_plural_reviewed_source_by_stable_snapshot_name() -> None:
    second_path = "/data/blobs/sess-1/def45600-0000-4000-8000-000000000000_data.csv"
    first = SourceResolved(
        name="first",
        plugin="csv",
        options={"path": BLOB_PATH, "blob_ref": BLOB_REF},
        observed_columns=("value",),
        sample_rows=(),
        on_validation_failure="discard",
    )
    second = SourceResolved(
        name="second",
        plugin="csv",
        options={"path": second_path, "blob_ref": "def45600-0000-4000-8000-000000000000"},
        observed_columns=("value",),
        sample_rows=(),
        on_validation_failure="discard",
    )
    guided = replace(
        GuidedSession.initial(),
        source_order=(
            "11111111-1111-4111-8111-111111111111",
            "22222222-2222-4222-8222-222222222222",
        ),
        reviewed_sources={
            "11111111-1111-4111-8111-111111111111": first,
            "22222222-2222-4222-8222-222222222222": second,
        },
    )
    state = CompositionState(
        sources={
            "first": SourceSpec(
                plugin="csv",
                on_success="main",
                options={"path": BLOB_PATH},
                on_validation_failure="discard",
            ),
            "second": SourceSpec(
                plugin="csv",
                on_success="main",
                options={"path": second_path},
                on_validation_failure="discard",
            ),
        },
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
        guided_session=guided,
    )

    out = _reattach_guided_blob_refs(state)

    assert out.sources["first"].options["blob_ref"] == BLOB_REF
    assert out.sources["second"].options["blob_ref"] == "def45600-0000-4000-8000-000000000000"


def test_reattaches_two_explicitly_reviewed_sources_sharing_one_blob_path() -> None:
    stable_ids = (
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
    )
    reviewed = {
        stable_id: SourceResolved(
            name=name,
            plugin="csv",
            options={"path": BLOB_PATH, "blob_ref": BLOB_REF},
            observed_columns=("value",),
            sample_rows=(),
            on_validation_failure="discard",
        )
        for stable_id, name in zip(stable_ids, ("first", "second"), strict=True)
    }
    guided = replace(
        GuidedSession.initial(),
        source_order=stable_ids,
        reviewed_sources=reviewed,
    )
    state = CompositionState(
        sources={
            name: SourceSpec(
                plugin="csv",
                on_success="main",
                options={"path": BLOB_PATH},
                on_validation_failure="discard",
            )
            for name in ("first", "second")
        },
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
        guided_session=guided,
    )

    out = _reattach_guided_blob_refs(state)

    assert out.sources["first"].options["blob_ref"] == BLOB_REF
    assert out.sources["second"].options["blob_ref"] == BLOB_REF


def test_rejects_ambiguous_live_sources_sharing_reviewed_blob_path() -> None:
    guided = _guided_with_snapshot(blob_ref=BLOB_REF, path=BLOB_PATH, name="missing")
    state = CompositionState(
        sources={
            name: SourceSpec(
                plugin="csv",
                on_success="main",
                options={"path": BLOB_PATH},
                on_validation_failure="discard",
            )
            for name in ("first", "second")
        },
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
        guided_session=guided,
    )

    with pytest.raises(AuditIntegrityError, match="guided blob source mapping"):
        _reattach_guided_blob_refs(state)
