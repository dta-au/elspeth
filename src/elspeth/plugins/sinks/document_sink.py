"""Verbatim single-value local document sink.

``text`` writes one LF-delimited record per row and therefore diverts any value
carrying CR or LF. That invariant is correct for line-oriented records and is
exactly what makes ``text`` unable to publish a generated document: an
announcement, report, or summary is one value whose line breaks are content,
not record separators.

This sink is the other half of that pair. It writes one value to one file
byte-for-byte — no framing, no escaping, no trailing newline — and it publishes
only when the run delivers exactly one value to the target. Joining several
rows into one document is the ``report_assemble`` aggregation's job, so this
sink deliberately offers no separator knob: two joiners for one concern would
drift apart, and a silent concatenation would invent a frame the sink exists to
avoid.
"""

from __future__ import annotations

import codecs
import keyword
from collections.abc import Iterator, Mapping
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from elspeth.contracts import CallType, Determinism, PluginSchema
from elspeth.contracts.contexts import SinkContext
from elspeth.contracts.diversion import SinkWriteResult
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.plugin_assistance import PluginAssistance
from elspeth.contracts.plugin_semantics import InputSemanticRequirements
from elspeth.contracts.sink_effects import (
    SINK_EFFECT_PROTOCOL_VERSION,
    ResolvedSinkEffectMode,
    RestrictedSinkEffectContext,
    SinkEffectCommitResult,
    SinkEffectExecutionPurpose,
    SinkEffectInputKind,
    SinkEffectInspection,
    SinkEffectInspectionRequest,
    SinkEffectPipelineMembersInput,
    SinkEffectPlan,
    SinkEffectPrepareRequest,
    SinkEffectReconcileResult,
)
from elspeth.plugins.infrastructure.base import BaseSink
from elspeth.plugins.infrastructure.config_base import LocalFileSinkConfig, OutputCollisionPolicy
from elspeth.plugins.infrastructure.output_paths import resolve_output_collision_path, validate_output_collision_policy_mode
from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config
from elspeth.plugins.sinks._diversion_attribution import DiversionAttribution, build_diversion_attribution
from elspeth.plugins.sinks._local_file_effects import (
    commit_local_effect,
    inspect_local_effect,
    predecessor_local_path,
    prepare_local_effect,
    reconcile_local_effect,
)

# The remedy is part of the reason on purpose: a prohibition that names no
# alternative is what left "generate an announcement and write it to a file"
# with no correct answer in the first place.
_ONE_VALUE_REMEDY = (
    "Combine rows into one value with the report_assemble aggregation first, or use the text sink to write one line per row."
)


class DocumentSinkConfig(LocalFileSinkConfig):
    """Configuration for verbatim single-value document output."""

    field: str = Field(description="String field whose whole value becomes the file contents.")
    encoding: Literal["utf-8", "ascii", "latin-1", "cp1252"] = Field(
        default="utf-8",
        description="Character encoding used for the emitted document.",
    )
    mode: Literal["write"] = Field(
        default="write",
        description="Write a new output. Appending is not offered: concatenating unframed documents would invent a record separator.",
    )

    @field_validator("field")
    @classmethod
    def _validate_field(cls, value: str) -> str:
        if not value.isidentifier() or keyword.iskeyword(value):
            raise ValueError(f"field {value!r} must be a non-keyword Python identifier")
        return value

    @field_validator("encoding")
    @classmethod
    def _validate_encoding(cls, value: str) -> str:
        try:
            canonical = codecs.lookup(value).name
        except LookupError as exc:
            raise ValueError(f"unknown encoding: {value!r}") from exc
        if canonical not in {"utf-8", "ascii", "iso8859-1", "cp1252"}:
            raise ValueError("encoding must be one of utf-8, ascii, latin-1, or cp1252")
        return value

    @model_validator(mode="after")
    def _validate_collision_mode(self) -> DocumentSinkConfig:
        validate_output_collision_policy_mode(
            plugin_name="DocumentSink",
            mode=self.mode,
            collision_policy=self.collision_policy,
        )
        return self


class DocumentSink(BaseSink):
    """Write one configured field's whole value to a file, byte-for-byte."""

    name = "document"
    determinism = Determinism.IO_WRITE
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:8b80c271b195f617"
    config_model = DocumentSinkConfig
    supports_resume = False
    effect_protocol_version = SINK_EFFECT_PROTOCOL_VERSION
    effect_call_type = CallType.FILESYSTEM
    supported_effect_modes = frozenset({"write"})
    supported_effect_input_kinds = frozenset({SinkEffectInputKind.PIPELINE_MEMBERS})

    usage_when_to_use: str = (
        "Use when one row carries a whole generated document — an announcement, report, summary, or assembled markdown — "
        "that must reach the file exactly as produced, with its own line breaks preserved and no trailing newline added."
    )
    usage_when_not_to_use: str = (
        "Do not use for one record per row — that is the text sink, which writes each value as its own LF-delimited line. "
        "Do not use to concatenate several rows into one file: this sink publishes only for a run that delivers exactly one "
        "value to it and diverts every row it can see beyond the first, so join rows with the report_assemble aggregation "
        "first. Do not use for a field that can be empty: a zero-byte document is diverted rather than published, because an "
        "empty value would otherwise write a file or not depending on whether the target already existed."
    )
    example_use: str = """sinks:
  announcement:
    plugin: document
    options:
      path: outputs/announcement.txt
      field: announcement_text
      collision_policy: auto_increment
      schema:
        mode: fixed
        fields:
          - "announcement_text: str"
"""
    capability_tags: tuple[str, ...] = ("document", "file", "multiline", "single-value")

    @classmethod
    def probe_config(cls) -> dict[str, Any]:
        """Minimal config for the semantic-satisfiability invariant.

        Touches nothing on disk: ``resolved_path`` only resolves a Path and the
        schema factory builds a model in memory.
        """
        return {
            "path": "document_sink_probe.txt",
            "field": "document_sink_probe_body",
            "schema": {"mode": "fixed", "fields": ["document_sink_probe_body: str"]},
        }

    @classmethod
    def _resolve_sink_effect_mode(
        cls,
        config: Mapping[str, object],
        *,
        purpose: SinkEffectExecutionPurpose,
    ) -> ResolvedSinkEffectMode | None:
        # Unlike text and json, no purpose switches this sink to append:
        # supports_resume is False, so configure_for_resume() never runs and
        # the executed mode is "write" for every purpose.
        cfg = DocumentSinkConfig.from_dict(dict(config), plugin_name=cls.name)
        del purpose
        return ResolvedSinkEffectMode(cfg.mode)

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        cfg = DocumentSinkConfig.from_dict(config, plugin_name=self.name)
        self._path = cfg.resolved_path()
        self._requested_path = self._path
        self._field = cfg.field
        self._encoding = cfg.encoding
        self._mode = cfg.mode
        self._collision_policy: OutputCollisionPolicy | None = cfg.collision_policy
        self._schema_config = cfg.schema_config
        self._schema_class: type[PluginSchema] = create_schema_from_config(
            self._schema_config,
            "DocumentSinkRowSchema",
            allow_coercion=False,
        )
        self.input_schema = self._schema_class
        self.declared_required_fields = self._schema_config.get_effective_required_fields() | {self._field}
        # Rows handed to this output, keyed by effect id, for the run named by
        # _delivery_run_id. Keyed rather than summed so that re-preparing one
        # effect — which happens whenever a RESERVED effect's prepare raises
        # and re-enters — overwrites its entry instead of inflating the total.
        self._delivery_run_id: str | None = None
        self._delivered_by_effect: dict[str, int] = {}

    def _record_delivery(self, *, run_id: str, effect_id: str, member_count: int) -> int:
        """Tally rows delivered to this output and return the running total.

        The tally spans every effect this sink instance prepares for ``run_id``
        and resets when a new run reaches the instance. One instance serves the
        whole flush loop of a run context, so in a single-worker run — the
        ordinary case — the total is the run's true row count. Effects claimed
        by a different worker have their own instance and are not counted here,
        which is why the caller treats this as a lower bound rather than as the
        run's whole delivery.
        """
        if self._delivery_run_id != run_id:
            self._delivery_run_id = run_id
            self._delivered_by_effect = {}
        self._delivered_by_effect[effect_id] = member_count
        return sum(self._delivered_by_effect.values())

    def inspect_effect(
        self,
        request: SinkEffectInspectionRequest,
        ctx: RestrictedSinkEffectContext,
    ) -> SinkEffectInspection:
        del ctx
        predecessor_path = predecessor_local_path(request)
        if predecessor_path is not None:
            self._path = predecessor_path
        else:
            self._path = resolve_output_collision_path(self._requested_path, self._collision_policy)
        return inspect_local_effect(target_path=self._path, request=request)

    def prepare_effect(
        self,
        request: SinkEffectPrepareRequest,
        ctx: RestrictedSinkEffectContext,
    ) -> SinkEffectPlan:
        if type(request.effect_input) is not SinkEffectPipelineMembersInput:
            raise TypeError("DocumentSink effects require pipeline member input")
        members = request.effect_input.members
        current_by_effect_id = {member.member_effect_id: member for member in members}
        predecessor_declared = bool(request.inspection.evidence["predecessor_declared"])
        # A replace-mode rebuild serializes the full cumulative snapshot for the
        # target and never includes baseline file bytes (see continuation_emission).
        # So the one-value rule is a question about the whole run's delivery to
        # this output, not about this effect alone: two single-row effects put
        # two members in the snapshot and must not publish either.
        emitted_members = tuple(request.effect_input.target_snapshot_members)
        delivered = self._record_delivery(run_id=ctx.run_id, effect_id=request.effect_id, member_count=len(members))
        # One sink can produce several effects per run — the flush loop groups
        # pending tokens by outcome (engine/orchestrator/sink_flush.py) and runs
        # once per drain — so neither available count sees every row on its own:
        #   * the cumulative snapshot carries accepted predecessor members, from
        #     any worker, but silently drops diverted ones (_target_snapshot_members
        #     in engine/executors/sink_effects.py skips prepared_disposition
        #     "diverted"). A run whose earlier effects all diverted therefore
        #     hands this effect a snapshot indistinguishable from a fresh
        #     single-row run;
        #   * the per-run tally counts every member this instance was handed,
        #     diverted or not, and so closes exactly that blind spot.
        # Both are lower bounds on the run's true delivery, and requiring BOTH to
        # equal one is what makes the rule hold however rows are BATCHED into
        # effects — [2] then [1], [1] then [2] and [3] as one effect all refuse
        # to publish, because the tally sees the diverted rows the snapshot drops.
        #
        # SCOPE, precisely: that argument covers one sink INSTANCE. It does not
        # generalise, and calling it "fail-closed" without qualification would be
        # wrong — a row invisible to BOTH counts leaves both reading one while the
        # true delivery is larger, and this publishes. That is reachable only
        # across instances: the tally is per-instance in-memory state, so rows
        # another instance diverted are in neither its snapshot (dropped) nor this
        # tally (foreign). No local signal survives to detect it — a diverted row
        # leaves nothing in the shared snapshot to notice.
        #
        # What keeps that unreachable today is supports_resume = False plus the
        # CLI's pre-database resume refusal, i.e. an invariant enforced OUTSIDE
        # this file. test_one_value_rule_depends_on_this_sink_never_resuming pins
        # that coupling; do not flip supports_resume without making the tally
        # durable first (elspeth-694f771c69).
        publishable = len(emitted_members) == 1 and delivered == 1
        # The tightest lower bound on rows delivered to this output: the two
        # counts see different rows, so the larger is the closer of the two.
        # Reporting len(emitted_members) alone under-counted every run whose
        # earlier effects diverted — a [2] then [2] run said "2 rows" for a
        # delivery of four.
        row_count_seen = max(len(emitted_members), delivered)

        accepted: list[int] = []
        payload: bytes | None = None
        # (row, ordinal, reason) in ascending ordinal order. Collected first and
        # applied in one place below so every diversion side effect — the
        # failsink write, the ordinal partition, and the audit attribution —
        # happens together and cannot drift apart.
        pending_diversions: list[tuple[dict[str, Any], int, str]] = []

        if publishable:
            snapshot_member = emitted_members[0]
            current_member = current_by_effect_id.get(snapshot_member.member_effect_id)
            row = deep_thaw(snapshot_member.row)
            missing = object()
            value = row.get(self._field, missing)
            reason: str | None = None
            if type(value) is not str:
                reason = f"Document field {self._field!r} must be a string"
            elif not value:
                # An accepted row that stages zero bytes is reported accepted
                # but does not reliably produce a file: with no predecessor the
                # shared helper classes the plan NO_PUBLICATION/"virtual" and
                # writes nothing, while an existing target instead falls through
                # to atomic_replace and truncates it. Publication that depends
                # on what happened to be on disk is the dishonesty, and a
                # genuinely empty file is not reachable from here — the branch
                # that decides this lives in _local_file_effects.py, which four
                # sinks share. So the empty value is diverted: one outcome for
                # one input, whatever the target's prior state, and the row is
                # never reported accepted for a document that was never written.
                reason = f"Document field {self._field!r} is empty; this sink publishes no file for a zero-byte document"
            else:
                try:
                    # Verbatim: CR and LF are content here, and no record
                    # separator is appended — the file is the value.
                    payload = value.encode(self._encoding)
                except UnicodeEncodeError:
                    reason = f"Document value is not representable in configured codec {self._encoding}"
            if reason is not None:
                if current_member is None:
                    raise ValueError(f"Predecessor document snapshot is incompatible: {reason}")
                pending_diversions.append((row, current_member.ordinal, reason))
                payload = None
            elif current_member is not None:
                accepted.append(current_member.ordinal)
        else:
            # The decision is global, not per-row, so it is settled before any
            # byte is staged. Rows an earlier effect already published are not
            # in this effect's members and are left alone: a committed row
            # cannot honestly be reported as diverted.
            #
            # That leaves one residual this sink cannot close from here. When a
            # run's first effect carries exactly one row it publishes — at that
            # moment the run has legitimately delivered one value, and there is
            # no lookahead: commit_effect writes inside the effect and flush()
            # is a no-op. If later effects then deliver more rows, those rows
            # divert with the reason below, but the first document stays on
            # disk. Nothing here can retract it, and raising instead would not
            # remove the file — it would only turn a documented partial into a
            # run failure. What this sink can guarantee is that it never
            # publishes for a delivery it can already see is larger than one.
            reason = f"Document sinks publish exactly one value per file; this output received {row_count_seen} rows. {_ONE_VALUE_REMEDY}"
            for member in sorted(members, key=lambda candidate: candidate.ordinal):
                pending_diversions.append((deep_thaw(member.row), member.ordinal, reason))

        diverted: list[int] = []
        diversion_attribution: list[DiversionAttribution] = []
        for diverted_row, ordinal, diverted_reason in pending_diversions:
            self._divert_row(diverted_row, row_index=ordinal, reason=diverted_reason)
            diverted.append(ordinal)
            diversion_attribution.append(build_diversion_attribution(ordinal=ordinal, reason=diverted_reason))

        def chunks() -> Iterator[bytes]:
            if payload is not None:
                yield payload

        return prepare_local_effect(
            effect_id=request.effect_id,
            input_kind=request.input_kind,
            inspection=request.inspection,
            chunks=chunks(),
            row_count=len(members),
            accepted_ordinals=accepted,
            diverted_ordinals=diverted,
            encoding=self._encoding,
            format_name="document",
            stream_sequence=1 if predecessor_declared else 0,
            diversion_attribution=diversion_attribution,
        )

    def commit_effect(self, plan: SinkEffectPlan, ctx: RestrictedSinkEffectContext) -> SinkEffectCommitResult:
        del ctx
        return commit_local_effect(plan)

    def reconcile_effect(self, plan: SinkEffectPlan, ctx: RestrictedSinkEffectContext) -> SinkEffectReconcileResult:
        del ctx
        return reconcile_local_effect(plan)

    def write(self, rows: list[dict[str, Any]], ctx: SinkContext) -> SinkWriteResult:
        del rows, ctx
        raise RuntimeError("DocumentSink publication requires the recoverable sink effect coordinator") from None

    def flush(self) -> None:
        """No-op: ``commit_effect`` performs synchronous publication.

        Direct ``write`` publication is forbidden. The recoverable sink-effect
        coordinator owns inspection, intent recording, synchronous commit, and
        reconciliation, so there is no independent buffered state to flush.
        """
        pass

    def close(self) -> None:
        """No-op: file handles are opened and closed inside the effect commit path."""

    def input_semantic_requirements(self) -> InputSemanticRequirements:
        """Accept every text framing, including UNCONSTRAINED — that is the point.

        This sink writes the value verbatim, so a line break is content and no
        framing can be wrong for it. ``TextFraming.UNCONSTRAINED`` is listed
        explicitly rather than by leaving the dimension unconstrained, because
        accepting the generative claim is precisely what this sink exists to
        do: ``llm -> document`` must be SATISFIED at authoring time, in the
        same breath as ``llm -> text`` becomes a CONFLICT (ADR-039).

        NOT_TEXT is excluded: this sink encodes a ``str``, so a producer that
        positively claims a non-text value is a real contradiction, and
        ``accepted_value_types={STR}`` says the same thing on the other axis.

        ``accepted_content_kinds`` is DELIBERATELY empty — "dimension
        unconstrained" as a decision, not as an omission. A verbatim writer has
        no opinion on whether the document is prose, markdown, or HTML; that is
        the user's choice of file. Constraining it would also be actively
        wrong here: a generative producer declares ``content_kind=UNKNOWN``
        because prose-versus-markdown is not statically decidable, and any
        non-empty set downgrades that edge to UNKNOWN — turning the one
        composition this sink exists to bless into a mere advisory.

        ``unknown_policy=WARN``: an undeclared producer must not be blocked.

        The accepted set is DERIVED by subtraction, not enumerated. Enumerating
        it would be fail-closed against this sink's own intent: a future
        ``TextFraming`` member would be absent, so the sink that exists to
        accept every framing would CONFLICT on it — and unlike an UNKNOWN, a
        positive conflicting claim cannot be softened by ``unknown_policy``.
        Subtraction inverts the default, so a new member is accepted unless
        someone states a reason to exclude it. (``TextSink``'s ``{COMPACT}`` is
        also fail-closed, but there fail-closed MATCHES the intent.)
        """
        from elspeth.contracts.plugin_semantics import (
            FieldSemanticRequirement,
            InputSemanticRequirements,
            SemanticValueType,
            TextFraming,
            UnknownSemanticPolicy,
        )

        return InputSemanticRequirements(
            fields=(
                FieldSemanticRequirement(
                    field_name=self._field,
                    accepted_content_kinds=frozenset(),
                    # UNKNOWN is not an acceptance: compare_semantic short-circuits
                    # an UNKNOWN fact to an UNKNOWN outcome whatever the set says,
                    # so listing it would be inert. NOT_TEXT is the real exclusion.
                    accepted_text_framings=frozenset(TextFraming) - {TextFraming.NOT_TEXT, TextFraming.UNKNOWN},
                    accepted_value_types=frozenset({SemanticValueType.STR}),
                    requirement_code="document.field.verbatim_text",
                    unknown_policy=UnknownSemanticPolicy.WARN,
                    configured_by=("field",),
                ),
            )
        )

    @classmethod
    def get_agent_assistance(cls, *, issue_code: str | None = None) -> PluginAssistance | None:
        if issue_code == "document.field.verbatim_text":
            return PluginAssistance(
                plugin_name=cls.name,
                issue_code=issue_code,
                summary=(
                    "This sink writes one row's whole string value to the file, so the configured field must hold text. "
                    "A producer that emits a non-string value into it has nothing this sink can encode."
                ),
                suggested_fixes=("Point field at a string-valued field, or convert the value to a string upstream before this sink.",),
            )
        if issue_code is None:
            return PluginAssistance(
                plugin_name=cls.name,
                issue_code=None,
                summary="Write one row's whole string value to a file byte-for-byte, line breaks and all.",
                composer_hints=(
                    "This is the sink for generated or multiline text — an LLM announcement, a report, an assembled "
                    "markdown document. The value is written exactly as produced, with no quoting, no \\n escaping, and "
                    "no trailing newline added.",
                    "Set field to the field holding the whole document; its value must be a string.",
                    "It publishes exactly one value per file, counted over the whole run, not per batch. If the run delivers "
                    "more than one row here, every row the sink can still act on is diverted and no file is written — put "
                    "report_assemble upstream to combine rows into one value first.",
                    "The value must be a non-empty string: an empty one is diverted, because a zero-byte document cannot be "
                    "published honestly — it would leave no file at all, or silently truncate an existing one.",
                    "Use the text sink instead when each row is its own record and should become its own line; text "
                    "rejects any value containing CR or LF, which is why it cannot carry a generated document.",
                    "Choose only utf-8, ascii, latin-1, or cp1252; a value not representable in the configured encoding is "
                    "diverted without leaking its content.",
                    "There is no append mode and no resume: concatenating unframed documents would invent a record "
                    "separator this sink exists to avoid.",
                ),
            )
        return None
