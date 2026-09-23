"""Composer tool-call audit primitives (L0).

Captures the per-tool-call decision trail produced when an LLM (or operator)
drives the elspeth-composer to build a pipeline graph. The completed pipeline
YAML is the artefact; this module's records are the *decision trail* — every
tool invocation that produced the artefact, including its arguments, result,
status, and version delta.

Two surfaces consume :class:`ComposerToolInvocation`:

1. The standalone composer MCP server appends one JSONL line per invocation
   to a per-session events sidecar.
2. The web composer service buffers complete invocation records. Its compose
   loop persists redacted assistant/tool responses, state revisions, and
   rejection evidence during phase P4. Those tool rows do not contain the
   invocation envelope; successful P4 clears the corresponding exception
   replay trail. Guided settlement and legacy route drains instead persist
   redacted invocation envelopes in chat-message ``tool_calls`` under the
   ``_kind=audit`` discriminator, using ``role=audit`` or linked ``role=tool``
   rows as appropriate.

Buffered invocation fields and durable response fields are distinct contracts.
P4 response content can retain a successful dispatch's Composer version and
redacted failure classification, while state bindings identify persisted
revisions. It does not separately persist every invocation field, such as
``version_before`` or dispatch timing. Session revision numbers and Composer
dispatch versions are separate version domains.

Layer: L0 (contracts). Imports nothing above. Canonical-JSON serialization
and SHA-256 hashing happen at L3 construction sites (recorders/dispatchers),
not from this module — that keeps the leaf clean.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, TypedDict


class PipelineDispatchAuditPayload(TypedDict):
    """Closed durable binding for one successful pipeline dispatch."""

    tool_call_id: str
    tool_name: str
    status: str
    arguments_hash: str
    result_hash: str


class ComposerToolStatus(StrEnum):
    """Outcome of a single composer tool dispatch.

    SUCCESS    — handler completed (the underlying tool may have returned
                 ``success=False`` semantically; that is still a successful
                 dispatch — the audit record carries the full result payload
                 so an auditor can read the semantic outcome).
    ARG_ERROR  — Tier-3 boundary failure. Either ``ToolArgumentError`` was
                 raised by a handler, or pre-dispatch validation rejected
                 the LLM-supplied arguments (JSON decode failure, non-dict
                 arguments, missing schema-required paths).
    CANCELLED  — dispatch was intentionally cancelled by its coordinator.
                 This is a lifecycle outcome, not a plugin defect.
    PLUGIN_CRASH — any exception class other than ``ToolArgumentError``
                 escaped the handler. Plugins are system code, so this is
                 a Tier-1/2 plugin bug (see
                 docs/guides/data-trust-and-error-handling.md §Plugin Ownership);
                 the audit record fixes the time and arguments at which
                 the bug fired.
    """

    SUCCESS = "success"
    ARG_ERROR = "arg_error"
    CANCELLED = "cancelled"
    PLUGIN_CRASH = "plugin_crash"


class ToolArgumentErrorCategory(StrEnum):
    """Closed, value-free reason an ``ARG_ERROR`` dispatch was rejected.

    ``error_class`` names the exception class actually raised; this names
    the stage and kind of the rejection, so shape errors (which a provider
    grammar could have prevented) are distinguishable from value errors
    (which no grammar prevents).

    Wire stage — the provider's argument text never reached the semantic
    contract:

    WIRE_JSON_INVALID  — ``bounded_json_loads`` rejected the text as JSON.
    WIRE_JSON_BOUNDS   — the JSON exceeded the bounded-decoder limits.
    WIRE_NOT_OBJECT    — valid JSON, but not a JSON object.
    WIRE_ENVELOPE      — the web ``set_pipeline`` ``{"pipeline": {...}}``
                         envelope was malformed.

    Semantic stage — the arguments were checked against the tool contract:

    CANONICALIZATION      — the arguments are not canonical JSON.
    MISSING_REQUIRED_PATH — a schema-required (nested) path is absent.
    SCHEMA_SHAPE          — the flat Draft 2020-12 schema failed on a
                            keyword a provider grammar can express.
    SCHEMA_BOUND          — the schema failed on a keyword no provider
                            grammar expresses (length, ``not``, ...).
    MODEL_VALIDATION      — the pydantic arguments model rejected them.

    Value stage — the arguments were well-formed but not acceptable:

    PROMPT_BUDGET                      — advisor prompt-size cap.
    DISCOVERY_ONLY                     — a mutation tool on a read-only path.
    DUPLICATE_RESOLVED_INTERPRETATION  — interpretation already resolved.
    RATE_CAP_PER_SESSION_DAY / RATE_CAP_PER_TERM — interpretation caps.
    SEMANTIC_RULE                      — any other handler rule.
    """

    WIRE_JSON_INVALID = "wire_json_invalid"
    WIRE_JSON_BOUNDS = "wire_json_bounds"
    WIRE_NOT_OBJECT = "wire_not_object"
    WIRE_ENVELOPE = "wire_envelope"
    CANONICALIZATION = "canonicalization"
    MISSING_REQUIRED_PATH = "missing_required_path"
    SCHEMA_SHAPE = "schema_shape"
    SCHEMA_BOUND = "schema_bound"
    MODEL_VALIDATION = "model_validation"
    PROMPT_BUDGET = "prompt_budget"
    DISCOVERY_ONLY = "discovery_only"
    DUPLICATE_RESOLVED_INTERPRETATION = "duplicate_resolved_interpretation"
    RATE_CAP_PER_SESSION_DAY = "rate_cap_per_session_day"
    RATE_CAP_PER_TERM = "rate_cap_per_term"
    SEMANTIC_RULE = "semantic_rule"


@dataclass(frozen=True, slots=True)
class ComposerToolInvocation:
    """One composer tool dispatch as recorded for audit.

    Field semantics
    ---------------

    ``arguments_canonical`` / ``arguments_hash``
        RFC 8785 canonical JSON of the LLM-supplied arguments and its
        SHA-256 hex digest. ``arguments_hash == sha256(arguments_canonical)``
        is a Tier-1 invariant: a verifier reading this record back from
        durable storage MUST recompute the digest and crash on mismatch
        (silent coercion of the audit trail is evidence tampering).

    ``authority_arguments_canonical`` / ``authority_arguments_hash``
        Optional set-pipeline-only semantic binding. The generic arguments
        pair retains the exact tool payload shape; this second pair projects
        order-sensitive Composer fields into an unambiguous canonical form.
        Non-``set_pipeline`` records omit both fields from :meth:`to_dict`.

    ``result_canonical`` / ``result_hash``
        Same pair for the dispatch result. ``None`` when the dispatch did
        not complete (``ARG_ERROR`` pre-dispatch sites, ``CANCELLED``,
        ``PLUGIN_CRASH``).

    ``status``
        See :class:`ComposerToolStatus`.

    ``error_class`` / ``error_message``
        Populated on ``ARG_ERROR``, ``CANCELLED``, and ``PLUGIN_CRASH``.
        ``error_class`` is the name of the exception class actually raised
        (or constructed) at the recording site, never a hand-written label.
        ``error_message``
        is already-redacted at the dispatch boundary — for
        ``ToolArgumentError`` this is ``exc.args[0]``, which the structured
        constructor composes from the safe-by-design ``(argument, expected,
        actual_type)`` triple. ``CANCELLED`` stores only a closed reason code.
        For ``PLUGIN_CRASH`` callers MUST NOT pass
        ``str(exc)`` because plugin exception messages can carry secrets,
        DB URLs, or filesystem paths; pass only ``type(exc).__name__`` and
        a sanitized summary.

    ``version_before`` / ``version_after``
        :attr:`CompositionState.version` immediately before and after the
        dispatch. ``version_after is None`` on paths that did not complete
        (``ARG_ERROR`` pre-dispatch, ``CANCELLED``, ``PLUGIN_CRASH``).
        ``version_after == version_before`` on cache hits and on dispatches
        that did not mutate state.

    ``cache_hit``
        ``True`` when the dispatch was served from the per-compose-call
        discovery cache without re-running the handler. Cache hits are
        recorded because the LLM made a *new* decision based on the
        cached payload; the original recording (when the cache was
        populated) belongs to a different decision.

    ``started_at`` / ``finished_at`` / ``latency_ms``
        UTC wall-clock window around the dispatch. ``latency_ms`` is
        derived from ``time.monotonic_ns`` at the dispatch site, not from
        the wall-clock pair — wall clocks can step backwards.

    ``actor``
        Stable string identifying who drove the dispatch.
        ``"composer-mcp:cli"`` or ``"composer-web:user-{user_id}"``.

    ``error_category``
        The closed :class:`ToolArgumentErrorCategory` of an ``ARG_ERROR``.
        Required on ``ARG_ERROR`` and ``None`` on every other status.

    Immutability and the cross-field check
    --------------------------------------
    Every field is a scalar, ``StrEnum``, ``datetime``, or ``str|None``, so
    ``frozen=True`` alone is sufficient. Deep-freezing exists because
    ``frozen=True`` leaves container contents mutable through the attribute
    reference; a scalar-only record has no container to reach through, so
    no freeze guard is defined. ``__post_init__`` exists only for the
    status/category cross-field check: an argument rejection must say which
    closed category rejected it, and no other status may carry one.
    """

    tool_call_id: str
    tool_name: str
    arguments_canonical: str
    arguments_hash: str
    result_canonical: str | None
    result_hash: str | None
    status: ComposerToolStatus
    error_class: str | None
    error_message: str | None
    version_before: int
    version_after: int | None
    started_at: datetime
    finished_at: datetime
    latency_ms: int
    actor: str
    cache_hit: bool = False
    authority_arguments_canonical: str | None = None
    authority_arguments_hash: str | None = None
    error_category: ToolArgumentErrorCategory | None = None

    def __post_init__(self) -> None:
        if self.error_category is not None and type(self.error_category) is not ToolArgumentErrorCategory:
            raise TypeError("ComposerToolInvocation.error_category must be a ToolArgumentErrorCategory")
        if self.status is ComposerToolStatus.ARG_ERROR and self.error_category is None:
            raise ValueError("ComposerToolInvocation with status ARG_ERROR requires an error_category")
        if self.status is not ComposerToolStatus.ARG_ERROR and self.error_category is not None:
            raise ValueError(f"ComposerToolInvocation with status {self.status.value} must not carry an error_category")

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly dict for sidecar serialization.

        ``started_at``/``finished_at`` are emitted as ISO-8601 strings so
        the dict is directly ``json.dumps``-able. ``status`` becomes its
        string value. The output shape is the canonical sidecar payload
        used by standalone-MCP JSONL lines and as input to web-composer
        invocation-envelope projections. Web storage applies its redaction
        policy before persisting those envelopes under ``_kind=audit``.
        Compose-loop P4 response rows use a separate projection and do not
        serialize this complete record.
        """
        raw = asdict(self)
        raw["status"] = self.status.value
        raw["error_category"] = self.error_category.value if self.error_category is not None else None
        raw["started_at"] = self.started_at.isoformat()
        raw["finished_at"] = self.finished_at.isoformat()
        if self.authority_arguments_canonical is None and self.authority_arguments_hash is None:
            del raw["authority_arguments_canonical"]
            del raw["authority_arguments_hash"]
        return raw


class ComposerToolRecorder(Protocol):
    """Append-only sink for :class:`ComposerToolInvocation` records.

    Implementations:

    - Standalone MCP: writes one JSONL line per invocation to a per-session
      events sidecar (``{scratch}/{session_id}.events.jsonl``). When the
      session_id is unresolved (the very first ``new_session`` call), the
      recorder buffers in memory and flushes on first resolution.
    - Web composer: in-memory buffer consumed by compose-loop processing,
      guided settlement, and legacy route drains. P4 commits redacted
      response/state/rejection evidence inside the loop; already persisted
      tool turns do not replay their invocations through exception carriers.
      Guided settlement and legacy drains persist redacted invocation
      envelopes through their own transactional storage paths.

    Recorder calls happen synchronously from the dispatch site. Every
    code path through the dispatcher MUST call ``record(...)`` before
    returning — audit primacy is contractual: audit fires first,
    synchronously, and an unrecorded dispatch did not happen (see the
    ``logging-telemetry-policy`` skill §The Primacy Test).
    The standalone MCP and web-composer
    dispatch sites both implement the same try/finally shape used by
    ``AuditedLLMClient.chat_completion`` to make "audit fires before
    return" structurally enforceable.
    """

    def record(self, invocation: ComposerToolInvocation) -> None:
        """Persist one invocation. Called once per dispatch."""
        ...

    def resolve_session(self, session_id: str) -> None:
        """Hint that the session_id is now resolved.

        Recorders that buffer pre-resolution records use this hook to
        flush. In-memory recorders (e.g.
        :class:`~elspeth.web.composer.audit.BufferingRecorder`) should
        implement this as a no-op — there is nothing to flush.

        Lifting this onto the Protocol (rather than calling
        ``isinstance(recorder, JsonlEventRecorder)`` from the server)
        keeps the abstraction whole: any future recorder
        implementation can opt into pre-resolution behaviour without
        the dispatch site needing to know.
        """
        ...
