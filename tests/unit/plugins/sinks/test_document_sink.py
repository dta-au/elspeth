"""Tests for the verbatim single-value local document sink.

The effect-protocol cases run against a real filesystem — staging, atomic
commit, reconciliation, and the accepted/diverted ordinal partition — because
the one-value rule is enforced inside ``prepare_effect`` and is only observable
through the durable plan it produces.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from elspeth.contracts.errors import SinkEffectCapabilityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.contracts.plugin_semantics import SemanticValueType, TextFraming, UnknownSemanticPolicy
from elspeth.contracts.sink_effects import (
    ResolvedSinkEffectMode,
    RestrictedSinkEffectContext,
    SinkEffectDescriptorMode,
    SinkEffectExecutionPurpose,
    SinkEffectInputKind,
    SinkEffectInspectionRequest,
    SinkEffectMember,
    SinkEffectPipelineMembersInput,
    SinkEffectPrepareRequest,
    SinkEffectReconcileKind,
)
from elspeth.engine._error_hash import compute_error_hash
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.infrastructure.preflight import plugin_preflight_mode
from elspeth.plugins.infrastructure.runtime_factory import validate_sink_effect_type_capability
from elspeth.plugins.sinks.document_sink import DocumentSink, DocumentSinkConfig
from tests.fixtures.base_classes import inject_write_failure

_SCHEMA = {"mode": "observed"}
_CTX = RestrictedSinkEffectContext(
    run_id="run-1",
    run_started_at=datetime(2026, 7, 16, tzinfo=UTC),
    operation_id="operation-1",
    sink_node_id="sink-1",
)
_MULTILINE = "Announcement\n\nWe are pleased to report:\r\n  * one\n  * two\n"


def _config(path: Path, **overrides: object) -> dict[str, object]:
    config: dict[str, object] = {
        "path": str(path),
        "field": "announcement_text",
        "encoding": "utf-8",
        "schema": _SCHEMA,
    }
    config.update(overrides)
    return config


def _sink(path: Path, **overrides: object) -> DocumentSink:
    return inject_write_failure(DocumentSink(_config(path, **overrides)))


def _member(ordinal: int, row: dict[str, object], *, generation: str = "token") -> SinkEffectMember:
    row_bytes = canonical_json(row).encode("utf-8")
    return SinkEffectMember(
        ordinal=ordinal,
        token_id=f"{generation}-{ordinal}",
        row_id=f"{generation}-row-{ordinal}",
        ingest_sequence=ordinal,
        lineage_json="[]",
        lineage_hash=sha256(b"[]").hexdigest(),
        payload_hash=sha256(row_bytes).hexdigest(),
        row=row,
        member_effect_id=sha256(f"member-{generation}-{ordinal}-{row_bytes!r}".encode()).hexdigest(),
    )


def _prepare(
    sink: DocumentSink,
    *,
    effect_id: str,
    rows: list[dict[str, object]],
    predecessor=None,
    predecessor_rows: list[dict[str, object]] | None = None,
):
    members = tuple(_member(index, row) for index, row in enumerate(rows))
    if predecessor_rows:
        # Mirror the coordinator's cumulative snapshot: finalized predecessor
        # members first, then the current partition, with contiguous ordinals.
        predecessor_members = tuple(_member(index, row, generation="predecessor") for index, row in enumerate(predecessor_rows))
        snapshot = tuple(replace(member, ordinal=ordinal) for ordinal, member in enumerate((*predecessor_members, *members)))
    else:
        snapshot = members
    inspection = sink.inspect_effect(
        SinkEffectInspectionRequest(effect_id=effect_id, target="{}", predecessor_descriptor=predecessor),
        _CTX,
    )
    return sink.prepare_effect(
        SinkEffectPrepareRequest(
            effect_id=effect_id,
            effect_input=SinkEffectPipelineMembersInput(members=members, target_snapshot_members=snapshot),
            inspection=inspection,
        ),
        _CTX,
    )


def _expected_diversion_attribution(sink: DocumentSink) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "ordinal": diversion.row_index,
            "reason_hash": stable_hash({"diversion_reason": diversion.reason}),
            "error_hash": compute_error_hash(diversion.reason),
        }
        for diversion in sink._get_diversions()
    )


# ── configuration ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("encoding", ["utf-8", "ascii", "latin-1", "cp1252"])
def test_document_sink_accepts_only_supported_ascii_compatible_encodings(encoding: str) -> None:
    parsed = DocumentSinkConfig.from_dict(_config(Path("out.txt"), encoding=encoding), plugin_name="document")
    assert parsed.encoding == encoding


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32", "utf-7", "shift_jis", "unknown-codec"])
def test_document_sink_rejects_encodings_outside_closed_set(encoding: str) -> None:
    with pytest.raises(PluginConfigError, match="encoding"):
        DocumentSinkConfig.from_dict(_config(Path("out.txt"), encoding=encoding), plugin_name="document")


@pytest.mark.parametrize("field", ["", "two words", "with-hyphen", "9lives", "class"])
def test_document_sink_rejects_non_identifier_or_keyword_field(field: str) -> None:
    with pytest.raises(PluginConfigError, match="identifier"):
        DocumentSinkConfig.from_dict(_config(Path("out.txt"), field=field), plugin_name="document")


def test_document_sink_offers_no_append_mode() -> None:
    """Append is refused at the schema, not merely discouraged in prose."""
    with pytest.raises(PluginConfigError):
        DocumentSinkConfig.from_dict(_config(Path("out.txt"), mode="append"), plugin_name="document")

    assert DocumentSinkConfig.model_json_schema()["properties"]["mode"]["const"] == "write"


def test_document_sink_rejects_append_collision_policy() -> None:
    with pytest.raises(PluginConfigError, match="collision_policy"):
        DocumentSinkConfig.from_dict(_config(Path("out.txt"), collision_policy="append_or_create"), plugin_name="document")


def test_document_sink_is_not_resumable_and_refuses_resume_configuration(tmp_path: Path) -> None:
    """supports_resume is False because configure_for_resume means "switch to append"."""
    sink = _sink(tmp_path / "out.txt")

    assert sink.supports_resume is False
    with pytest.raises(NotImplementedError):
        sink.configure_for_resume()


@pytest.mark.parametrize("purpose", list(SinkEffectExecutionPurpose))
def test_every_purpose_resolves_the_write_mode_actually_executed(purpose: SinkEffectExecutionPurpose) -> None:
    """No purpose switches this sink to append, so every purpose resolves "write"."""
    resolved = DocumentSink._resolve_sink_effect_mode(_config(Path("/tmp/out.txt")), purpose=purpose)

    assert resolved == ResolvedSinkEffectMode("write")


def test_preflight_construction_never_mutates_or_resolves_output(tmp_path: Path) -> None:
    path = tmp_path / "out.txt"
    path.write_bytes(b"winner")

    with plugin_preflight_mode(True):
        sink = DocumentSink(_config(path, collision_policy="auto_increment"))

    assert sink._path == path
    assert list(tmp_path.iterdir()) == [path]


def test_document_sink_assistance_names_the_remedy_and_the_text_sibling() -> None:
    assistance = DocumentSink.get_agent_assistance(issue_code=None)

    assert assistance is not None
    hints = " ".join(assistance.composer_hints)
    assert "report_assemble" in hints
    assert "text sink" in hints
    assert "multiline" in hints
    assert "no trailing newline" in hints


# ── the single accepted value ────────────────────────────────────────────────


def test_single_multiline_row_is_published_verbatim_without_a_trailing_newline(tmp_path: Path) -> None:
    """The g11 case: a generated multiline announcement reaches the file intact."""
    target = tmp_path / "announcement.txt"
    sink = _sink(target)

    plan = _prepare(sink, effect_id="a1" * 32, rows=[{"announcement_text": _MULTILINE}])

    assert plan.descriptor_mode is SinkEffectDescriptorMode.PRECOMPUTED
    assert plan.safe_evidence["accepted_ordinals"] == (0,)
    assert plan.safe_evidence["diverted_ordinals"] == ()
    assert plan.safe_evidence["publication_kind"] == "atomic_replace"

    result = sink.commit_effect(plan, _CTX)

    # Byte-for-byte: CR and LF are content, and nothing is appended.
    assert target.read_bytes() == _MULTILINE.encode("utf-8")
    assert not target.read_text().endswith("\n\n")
    assert result.accepted_ordinals == (0,)
    assert sink.reconcile_effect(plan, _CTX).kind is SinkEffectReconcileKind.APPLIED_WITH_EXACT_DESCRIPTOR


def test_staging_is_effect_addressed_and_consumed_by_commit(tmp_path: Path) -> None:
    target = tmp_path / "announcement.txt"
    sink = _sink(target)

    plan = _prepare(sink, effect_id="a2" * 32, rows=[{"announcement_text": "staged"}])
    staging = Path(str(plan.safe_evidence["staging_path"]))

    assert staging.exists()
    assert staging.parent == target.parent
    assert "a2" * 32 in staging.name
    assert not target.exists()

    sink.commit_effect(plan, _CTX)

    assert not staging.exists()
    assert target.read_bytes() == b"staged"


def test_reconcile_reports_not_applied_before_commit(tmp_path: Path) -> None:
    sink = _sink(tmp_path / "out.txt")
    plan = _prepare(sink, effect_id="a3" * 32, rows=[{"announcement_text": "pending"}])

    assert sink.reconcile_effect(plan, _CTX).kind is SinkEffectReconcileKind.NOT_APPLIED


def test_an_effect_is_never_issued_for_zero_rows() -> None:
    """Why the sink carries no zero-row branch: the protocol refuses one.

    ``prepare_effect`` therefore only ever sees a snapshot of one member (the
    published case) or two or more (the refused case).
    """
    with pytest.raises(ValueError, match="members must be non-empty"):
        SinkEffectPipelineMembersInput(members=(), target_snapshot_members=())


def test_a_rejected_single_value_publishes_no_file_at_all(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    sink = _sink(target)

    plan = _prepare(sink, effect_id="a4" * 32, rows=[{"announcement_text": 42}])

    assert plan.descriptor_mode is SinkEffectDescriptorMode.NO_PUBLICATION
    assert plan.safe_evidence["publication_kind"] == "virtual"
    assert plan.safe_evidence["accepted_ordinals"] == ()
    assert not target.exists()


# ── the one-value rule ───────────────────────────────────────────────────────


def test_many_rows_in_one_effect_divert_every_row_and_publish_nothing(tmp_path: Path) -> None:
    """No arbitrary winner: with no principled single value, nothing is published."""
    target = tmp_path / "out.txt"
    sink = _sink(target)

    plan = _prepare(
        sink,
        effect_id="b1" * 32,
        rows=[{"announcement_text": "first"}, {"announcement_text": "second"}, {"announcement_text": "third"}],
    )

    assert plan.descriptor_mode is SinkEffectDescriptorMode.NO_PUBLICATION
    assert plan.safe_evidence["accepted_ordinals"] == ()
    assert plan.safe_evidence["diverted_ordinals"] == (0, 1, 2)
    assert plan.safe_evidence["diversion_attribution"] == _expected_diversion_attribution(sink)
    assert not target.exists()


def test_multi_row_diversion_reason_names_the_remedy(tmp_path: Path) -> None:
    """g11's lesson: a prohibition that names no alternative is the defect."""
    sink = _sink(tmp_path / "out.txt")

    _prepare(sink, effect_id="b2" * 32, rows=[{"announcement_text": "a"}, {"announcement_text": "b"}])

    reasons = {diversion.reason for diversion in sink._get_diversions()}
    assert len(reasons) == 1
    reason = reasons.pop()
    assert "exactly one value" in reason
    assert "received 2 rows" in reason
    assert "report_assemble" in reason
    assert "text sink" in reason


def test_second_effect_diverts_its_own_row_and_leaves_the_published_document_intact(tmp_path: Path) -> None:
    """The one-value rule spans the run: a write-mode rebuild sees the cumulative snapshot.

    Two single-row effects put two members in the target snapshot. The first
    effect's row is already durably published and cannot honestly be reported as
    diverted, so only the current effect's row is diverted and the committed
    document is left exactly as it was.
    """
    target = tmp_path / "out.txt"
    first = _sink(target)
    first_result = first.commit_effect(
        _prepare(first, effect_id="c1" * 32, rows=[{"announcement_text": "published"}]),
        _CTX,
    )
    assert target.read_bytes() == b"published"

    second = _sink(target)
    second_plan = _prepare(
        second,
        effect_id="c2" * 32,
        rows=[{"announcement_text": "rejected"}],
        predecessor=first_result.descriptor,
        predecessor_rows=[{"announcement_text": "published"}],
    )

    assert second_plan.descriptor_mode is SinkEffectDescriptorMode.NO_PUBLICATION
    assert second_plan.safe_evidence["publication_kind"] == "inherited"
    assert second_plan.safe_evidence["accepted_ordinals"] == ()
    assert second_plan.safe_evidence["diverted_ordinals"] == (0,)
    assert [diversion.row_index for diversion in second._get_diversions()] == [0]

    # The already-published document is untouched, and the second value never
    # reaches the file.
    assert target.read_bytes() == b"published"


# ── per-value rejections ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected_reason_fragment"),
    [
        (42, "must be a string"),
        (None, "must be a string"),
        ({"nested": "dict"}, "must be a string"),
    ],
)
def test_non_string_value_is_diverted_not_coerced(tmp_path: Path, value: object, expected_reason_fragment: str) -> None:
    target = tmp_path / "out.txt"
    sink = _sink(target)

    plan = _prepare(sink, effect_id="d1" * 32, rows=[{"announcement_text": value}])

    assert plan.safe_evidence["accepted_ordinals"] == ()
    assert plan.safe_evidence["diverted_ordinals"] == (0,)
    assert expected_reason_fragment in sink._get_diversions()[0].reason
    assert not target.exists()


def test_unrepresentable_value_is_diverted_without_leaking_content(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    sink = _sink(target, encoding="ascii")

    plan = _prepare(sink, effect_id="d2" * 32, rows=[{"announcement_text": "café secret"}])

    assert plan.safe_evidence["diverted_ordinals"] == (0,)
    reason = sink._get_diversions()[0].reason
    assert "ascii" in reason
    assert "secret" not in reason
    assert not target.exists()


def test_missing_field_is_diverted(tmp_path: Path) -> None:
    sink = _sink(tmp_path / "out.txt")

    plan = _prepare(sink, effect_id="d3" * 32, rows=[{"unrelated": "value"}])

    assert plan.safe_evidence["diverted_ordinals"] == (0,)
    assert "must be a string" in sink._get_diversions()[0].reason


# ── protocol conformance ─────────────────────────────────────────────────────


def test_publication_requires_the_effect_coordinator(tmp_path: Path) -> None:
    sink = _sink(tmp_path / "out.txt")

    with pytest.raises(RuntimeError, match="effect coordinator"):
        sink.write([{"announcement_text": "direct"}], None)  # type: ignore[arg-type]


def test_effect_protocol_metadata_declares_write_only_pipeline_members() -> None:
    assert DocumentSink.supported_effect_modes == frozenset({"write"})
    assert DocumentSink.supported_effect_input_kinds == frozenset({SinkEffectInputKind.PIPELINE_MEMBERS})


def test_admission_gate_accepts_the_declared_capability_and_refuses_the_rest() -> None:
    """Run the validator that reads the declarations, not just the declarations.

    ``_resolve_sink_effect_mode`` answers "write" for every purpose including
    AUDIT_EXPORT, which is only safe because the input-kind gate refuses the
    audit-export snapshot outright.
    """
    validate_sink_effect_type_capability(DocumentSink, "write", SinkEffectInputKind.PIPELINE_MEMBERS)

    with pytest.raises(SinkEffectCapabilityError):
        validate_sink_effect_type_capability(DocumentSink, "append", SinkEffectInputKind.PIPELINE_MEMBERS)

    with pytest.raises(SinkEffectCapabilityError):
        validate_sink_effect_type_capability(DocumentSink, "write", SinkEffectInputKind.AUDIT_EXPORT_SNAPSHOT)


class TestDocumentSinkSemanticRequirement:
    """This sink exists so a generative producer HAS a correct destination.

    ``llm -> text`` became a hard authoring CONFLICT in the same change; that
    is only a repair if ``llm -> document`` is SATISFIED in the same breath.
    """

    def _requirement(self, path: Path):
        requirements = DocumentSink(_config(path)).input_semantic_requirements()
        assert len(requirements.fields) == 1
        return requirements.fields[0]

    def test_requirement_is_declared_on_the_configured_field(self, tmp_path: Path) -> None:
        requirement = self._requirement(tmp_path / "out.txt")
        assert requirement.field_name == "announcement_text"
        assert requirement.requirement_code == "document.field.verbatim_text"
        assert requirement.configured_by == ("field",)

    def test_accepts_unconstrained_framing(self, tmp_path: Path) -> None:
        """The point of the sink, as a declared fact rather than as prose.

        A generative producer claims UNCONSTRAINED (ADR-039). Accepting it is
        what turns ``llm -> document`` into a SATISFIED edge instead of an
        advisory that still rests on unknown_policy.
        """
        requirement = self._requirement(tmp_path / "out.txt")
        assert TextFraming.UNCONSTRAINED in requirement.accepted_text_framings

    def test_accepts_every_text_bearing_framing(self, tmp_path: Path) -> None:
        """Verbatim output means a line break is content; no framing is wrong."""
        requirement = self._requirement(tmp_path / "out.txt")
        assert requirement.accepted_text_framings == frozenset(
            {
                TextFraming.UNCONSTRAINED,
                TextFraming.COMPACT,
                TextFraming.NEWLINE_FRAMED,
                TextFraming.LINE_COMPATIBLE,
            }
        )

    def test_rejects_a_positively_non_text_producer(self, tmp_path: Path) -> None:
        """NOT_TEXT is a real contradiction: this sink encodes a str."""
        requirement = self._requirement(tmp_path / "out.txt")
        assert TextFraming.NOT_TEXT not in requirement.accepted_text_framings
        assert requirement.accepted_value_types == frozenset({SemanticValueType.STR})

    def test_content_kind_is_deliberately_unconstrained(self, tmp_path: Path) -> None:
        """Non-empty here would defeat the sink's own purpose.

        A generative producer declares content_kind=UNKNOWN because prose vs
        markdown is not statically decidable, and ANY non-empty set downgrades
        that edge to UNKNOWN — turning the one composition this sink exists to
        bless back into a mere advisory.
        """
        requirement = self._requirement(tmp_path / "out.txt")
        assert requirement.accepted_content_kinds == frozenset()

    def test_unknown_policy_is_warn(self, tmp_path: Path) -> None:
        requirement = self._requirement(tmp_path / "out.txt")
        assert requirement.unknown_policy is UnknownSemanticPolicy.WARN

    def test_probe_config_constructs_standalone(self) -> None:
        sink = DocumentSink(DocumentSink.probe_config())
        try:
            assert sink.input_semantic_requirements().fields[0].requirement_code == "document.field.verbatim_text"
        finally:
            sink.close()


def test_document_sink_accepts_the_multiline_value_text_refuses(tmp_path: Path) -> None:
    """The pair, stated as one fact: the framings differ by exactly UNCONSTRAINED.

    TextSink's set is {COMPACT}; DocumentSink's is a superset that adds every
    newline-bearing member. That difference IS the g11 repair — the same value
    that text must divert, document must accept.
    """
    from elspeth.plugins.sinks.text_sink import TextSink

    text_framings = (
        TextSink(
            {
                "path": str(tmp_path / "lines.txt"),
                "field": "announcement_text",
                "schema": _SCHEMA,
            }
        )
        .input_semantic_requirements()
        .fields[0]
        .accepted_text_framings
    )
    document_framings = DocumentSink(_config(tmp_path / "doc.txt")).input_semantic_requirements().fields[0].accepted_text_framings

    assert text_framings == frozenset({TextFraming.COMPACT})
    assert text_framings < document_framings, "document must accept strictly more framings than text"
    assert document_framings - text_framings == frozenset(
        {
            TextFraming.UNCONSTRAINED,
            TextFraming.NEWLINE_FRAMED,
            TextFraming.LINE_COMPATIBLE,
        }
    )
