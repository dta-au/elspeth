"""Launch-time guard for LLM fanout and provider spend risk.

The guard evaluates the Tier-1 composition snapshot immediately before run
creation.  When an LLM transform is downstream of a fanout producer, execution
must pause until the caller acknowledges the provider-call risk with the
deterministic token returned by this module.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict

from elspeth.contracts.freeze import freeze_fields
from elspeth.contracts.plugin_capabilities import PluginCapability
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.composer._producer_resolver import published_success_connection
from elspeth.web.composer.state import CompositionState, NodeSpec, SourceSpec, _coalesce_branch_connections
from elspeth.web.paths import resolve_data_path
from elspeth.web.plugin_policy.coverage import node_has_capability

FANOUT_GUARD_ERROR_TYPE = "execution_fanout_ack_required"
FANOUT_GUARD_AUDIT_COMMENT = "elspeth_execution_fanout_guard"
LLM_FANOUT_HIGH_CALL_THRESHOLD = 100

_RiskLevel = Literal["medium", "high"]
_ProducerKind = Literal["source", "node"]


class ExecutionFanoutRiskPayload(TypedDict):
    """Transport shape for one execution fanout risk."""

    node_id: str
    provider: str
    model: str | None
    credential_ref: str | None
    estimated_provider_calls: int | None
    provider_calls_per_row: int
    upstream_fanout: list[str]
    risk_level: _RiskLevel
    message: str


class ExecutionFanoutGuardPayload(TypedDict):
    """Transport shape for an execution fanout acknowledgement guard."""

    ack_token: str
    risk_level: _RiskLevel
    summary: str
    risks: list[ExecutionFanoutRiskPayload]


@dataclass(frozen=True, slots=True)
class ExecutionFanoutRisk:
    """One LLM launch risk requiring operator acknowledgement."""

    node_id: str
    provider: str
    model: str | None
    credential_ref: str | None
    estimated_provider_calls: int | None
    provider_calls_per_row: int
    upstream_fanout: Sequence[str]
    risk_level: _RiskLevel
    message: str

    def __post_init__(self) -> None:
        # ``upstream_fanout`` declared as Sequence[str]; producers may
        # pass a list literal which is mutable through the attribute
        # reference without a freeze guard, defeating ``frozen=True``.
        # All scalars are immutable; only the sequence needs the guard.
        freeze_fields(self, "upstream_fanout")

    def to_dict(self) -> ExecutionFanoutRiskPayload:
        return {
            "node_id": self.node_id,
            "provider": self.provider,
            "model": self.model,
            "credential_ref": self.credential_ref,
            "estimated_provider_calls": self.estimated_provider_calls,
            "provider_calls_per_row": self.provider_calls_per_row,
            "upstream_fanout": list(self.upstream_fanout),
            "risk_level": self.risk_level,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class ExecutionFanoutGuard:
    """Structured precondition response for high-fanout LLM execution."""

    ack_token: str
    risk_level: _RiskLevel
    summary: str
    risks: Sequence[ExecutionFanoutRisk]

    def __post_init__(self) -> None:
        # ``risks`` declared as Sequence[ExecutionFanoutRisk]. The risk
        # elements are themselves frozen (with their own freeze guards
        # above), but the outer sequence may be a mutable list at the
        # call site — without deep_freeze the guard's ``frozen=True``
        # claim leaks ``risks.append(...)`` mutability.
        freeze_fields(self, "risks")

    def to_dict(self) -> ExecutionFanoutGuardPayload:
        return {
            "ack_token": self.ack_token,
            "risk_level": self.risk_level,
            "summary": self.summary,
            "risks": [risk.to_dict() for risk in self.risks],
        }


class ExecutionFanoutGuardRequired(Exception):
    """Raised when a run requires an LLM fanout acknowledgement."""

    def __init__(self, guard: ExecutionFanoutGuard) -> None:
        self.guard = guard
        super().__init__(guard.summary)


@dataclass(frozen=True, slots=True)
class _Producer:
    kind: _ProducerKind
    node: NodeSpec | None = None
    source_name: str | None = None
    source: SourceSpec | None = None


def _producer_key(producer: _Producer) -> str:
    """Stable identity for cycle guards and deterministic predecessor order.

    Keyed on producer identity (``source:<name>`` / ``node:<id>``), never on a
    connection name — two producers may publish the same declared queue name.
    """
    if producer.kind == "source":
        return f"source:{producer.source_name}"
    if producer.node is None:
        raise RuntimeError("Node producer missing node reference")
    return f"node:{producer.node.id}"


@dataclass(frozen=True)
class _ProducerIndex:
    """Queue-aware producer resolution.

    ``by_connection`` is the ordinary single-producer map (with each declared
    queue installed as the canonical producer of its own id).
    ``queue_predecessors`` holds, per queue id, the distinct upstream producers
    that publish into that queue — in deterministic producer-id order.
    """

    by_connection: Mapping[str, _Producer]
    queue_predecessors: Mapping[str, tuple[_Producer, ...]]

    def __post_init__(self) -> None:
        freeze_fields(self, "by_connection", "queue_predecessors")


@dataclass(frozen=True, slots=True)
class _FanoutTrace:
    markers: tuple[str, ...]
    source_markers: tuple[str, ...]
    source_estimated_rows: int | None
    has_unknown_cardinality: bool
    # A marker whose output multiplier is genuinely unprovable (token-creating
    # transform, transform-mode aggregation, collector). A fork gate is NOT
    # unbounded: it duplicates each row once per branch, so along any single
    # branch the source-to-token multiplier stays exactly 1.
    has_unbounded_fanout: bool
    # A row_union with more than one branch, or a queue with more than one
    # predecessor, RECOMBINES streams additively. Below a fork of the SAME
    # source this would double-count rows the source dedup counted once, so a
    # trace that saw both a fork marker and a combining fan-in must refuse the
    # per-row arithmetic. Fan-in of distinct sources with no fork stays summed
    # exactly as before.
    has_combining_fan_in: bool


def evaluate_execution_fanout_guard(
    state: CompositionState,
    *,
    data_dir: str | Path,
) -> ExecutionFanoutGuard | None:
    """Return a guard when the composition can fan out into LLM calls.

    Direct source-to-LLM pipelines are allowed when the source cardinality is
    known and below ``LLM_FANOUT_HIGH_CALL_THRESHOLD``.  Any token-creating
    transform, transform-mode aggregation, or collector upstream of an LLM is
    treated as unbounded.  A fork gate has a PROVEN multiplier — each source
    row yields exactly one token per branch — so fork-only topologies keep
    their per-row and total call arithmetic (elspeth session 39578c6f review);
    a fork whose copies are later recombined by a row_union or a
    multi-predecessor queue falls back to unknown, because the recombination
    would double-count rows the source dedup counted once.
    """

    producers = _build_producer_index(state)
    nodes_by_id = {node.id: node for node in state.nodes}
    risks: list[ExecutionFanoutRisk] = []
    all_deterministic = True

    for node in state.nodes:
        if node.node_type != "transform" or not node_has_capability(node, PluginCapability.LLM):
            continue

        trace = _trace_upstream_fanout(
            input_label=node.input,
            producers=producers,
            nodes_by_id=nodes_by_id,
            data_dir=Path(data_dir),
        )
        provider_calls_per_row = _provider_calls_per_row(node.options)
        # A trace is DETERMINISTIC when every fanout marker is a fork gate and
        # no combining fan-in sits in the walk: each source row then reaches
        # this node as exactly one token, so per-row and total call counts are
        # provable. Fork markers spoil nothing on their own; a fork whose
        # copies are recombined (row_union / multi-predecessor queue) would
        # double-count the deduped source and stays unknown.
        deterministic_fanout = not trace.has_unbounded_fanout and not (trace.markers and trace.has_combining_fan_in)
        estimated_provider_calls = (
            trace.source_estimated_rows * provider_calls_per_row
            if trace.source_estimated_rows is not None and deterministic_fanout
            else None
        )

        requires_guard = (
            trace.has_unknown_cardinality
            or bool(trace.markers)
            or (estimated_provider_calls is not None and estimated_provider_calls > LLM_FANOUT_HIGH_CALL_THRESHOLD)
        )
        if not requires_guard:
            continue

        risk_level: _RiskLevel = (
            "high" if estimated_provider_calls is None or estimated_provider_calls >= LLM_FANOUT_HIGH_CALL_THRESHOLD * 10 else "medium"
        )
        provider = _string_option(node.options, "provider") or "unknown"
        model = _string_option(node.options, "model") or _string_option(node.options, "deployment_name")
        risks.append(
            ExecutionFanoutRisk(
                node_id=node.id,
                provider=provider,
                model=model,
                credential_ref=_credential_ref(node.options),
                estimated_provider_calls=estimated_provider_calls,
                provider_calls_per_row=provider_calls_per_row,
                upstream_fanout=trace.markers if trace.markers else trace.source_markers,
                risk_level=risk_level,
                message=_risk_message(
                    node_id=node.id,
                    provider=provider,
                    model=model,
                    estimated_provider_calls=estimated_provider_calls,
                    provider_calls_per_row=provider_calls_per_row,
                    markers=trace.markers,
                    deterministic_fanout=deterministic_fanout,
                ),
            )
        )
        if not deterministic_fanout:
            all_deterministic = False

    if not risks:
        return None

    risk_dicts = [risk.to_dict() for risk in risks]
    ack_token = stable_hash(
        {
            "kind": "execution_fanout_guard_v1",
            "composition_state": state.to_dict(),
            "risks": risk_dicts,
        }
    )[:32]
    return ExecutionFanoutGuard(
        ack_token=ack_token,
        risk_level="high" if any(risk.risk_level == "high" for risk in risks) else "medium",
        summary=_guard_summary(risks, all_deterministic=all_deterministic),
        risks=tuple(risks),
    )


def annotate_pipeline_yaml_with_fanout_guard(
    pipeline_yaml: str,
    guard: ExecutionFanoutGuard,
) -> str:
    """Persist an accepted launch guard in the run's YAML launch record."""

    payload = {
        "kind": "execution_fanout_guard_v1",
        "accepted": True,
        "ack_token": guard.ack_token,
        "risk_level": guard.risk_level,
        "summary": guard.summary,
        "risks": [risk.to_dict() for risk in guard.risks],
    }
    return f"# {FANOUT_GUARD_AUDIT_COMMENT}: {canonical_json(payload)}\n{pipeline_yaml}"


def _build_producer_index(state: CompositionState) -> _ProducerIndex:
    queue_ids = {node.id for node in state.nodes if node.node_type == "queue"}
    by_connection: dict[str, _Producer] = {}
    queue_predecessors: dict[str, dict[str, _Producer]] = {queue_id: {} for queue_id in queue_ids}

    def register(label: str, producer: _Producer) -> None:
        # A declared queue accepts many producers under its id: route them into
        # its predecessor set instead of the single-producer map, keyed on
        # stable producer identity so duplicates dedupe deterministically.
        if label in queue_ids and _producer_key(producer) != f"node:{label}":
            predecessors = queue_predecessors[label]
            producer_key = _producer_key(producer)
            if producer_key not in predecessors:
                predecessors[producer_key] = producer
            return
        by_connection[label] = producer

    for source_name, source in state.sources.items():
        if source.on_success != "discard":
            register(source.on_success, _Producer(kind="source", source_name=source_name, source=source))

    for node in state.nodes:
        # DERIVED from ``published_success_connection``, never restated. A
        # non-terminal coalesce, an aggregation that omits ``on_success``, and
        # every queue all have ``on_success is None`` yet are fully connected —
        # each publishes under its own node id. Reading ``node.on_success``
        # directly left all three unregistered, and ``walk_label`` below does a
        # ``.get()`` that returns silently on a miss, so the upstream trace
        # simply stopped and the LLM fanout guard was never raised
        # (elspeth-8190d4e4cf).
        for label in (published_success_connection(node), node.on_error):
            if label is not None and label != "discard":
                register(label, _Producer(kind="node", node=node))
        if node.routes is not None:
            for label in node.routes.values():
                if label != "discard":
                    register(label, _Producer(kind="node", node=node))
        if node.fork_to is not None:
            for label in node.fork_to:
                if label != "discard":
                    register(label, _Producer(kind="node", node=node))

    # No separate queue install: a queue publishes under its own id, so
    # ``published_success_connection`` already registered it in the loop above,
    # and ``register``'s divert only routes AWAY producers whose key is not
    # ``node:<queue id>`` — the queue itself always lands in ``by_connection``
    # regardless of the order its predecessors are seen in. Re-installing it
    # here would be the same hand restatement this function just stopped making.

    frozen_predecessors = {
        queue_id: tuple(predecessors[key] for key in sorted(predecessors)) for queue_id, predecessors in queue_predecessors.items()
    }
    return _ProducerIndex(by_connection=by_connection, queue_predecessors=frozen_predecessors)


def _trace_upstream_fanout(
    *,
    input_label: str,
    producers: _ProducerIndex,
    nodes_by_id: Mapping[str, NodeSpec],
    data_dir: Path,
) -> _FanoutTrace:
    markers: list[str] = []
    source_markers: list[str] = []
    source_estimated_rows: int | None = None
    unknown_source_seen = False
    has_unknown_cardinality = False
    has_unbounded_fanout = False
    has_combining_fan_in = False
    # Cycle guard keyed on stable producer identity, NOT connection name — a
    # queue's predecessors are distinct producers that share the queue's name.
    visited: set[str] = set()

    def record_source(producer: _Producer) -> None:
        nonlocal source_estimated_rows, unknown_source_seen, has_unknown_cardinality
        if producer.source is None or producer.source_name is None:
            raise RuntimeError("Source producer missing source reference")
        estimated_rows = _estimate_source_rows(producer.source, data_dir=data_dir)
        marker_prefix = f"source:{producer.source_name}:{producer.source.plugin}:estimated_rows="
        if estimated_rows is None:
            # An unknown-cardinality source keeps the sum unknowable and pins
            # the risk high; it is never allowed to silently vanish.
            unknown_source_seen = True
            has_unknown_cardinality = True
            source_estimated_rows = None
            source_markers.append(f"{marker_prefix}unknown")
            return
        if unknown_source_seen:
            source_estimated_rows = None
        elif source_estimated_rows is not None:
            source_estimated_rows += estimated_rows
        else:
            source_estimated_rows = estimated_rows
        source_markers.append(f"{marker_prefix}{estimated_rows}")

    def walk_label(label: str) -> None:
        # An unregistered label is a real state: a source or node may publish
        # under a name nothing upstream produces. Membership form keeps that
        # answer explicit rather than reading it out of a silent default.
        if label in producers.by_connection:
            walk_producer(producers.by_connection[label])

    def walk_producer(producer: _Producer) -> None:
        nonlocal has_unbounded_fanout, has_combining_fan_in
        key = _producer_key(producer)
        if key in visited:
            return
        visited.add(key)

        if producer.kind == "source":
            record_source(producer)
            return

        node = producer.node
        if node is None:
            raise RuntimeError("Node producer missing node reference")

        if node.node_type == "queue":
            # A queue fans in every predecessor; traverse them all so no
            # upstream cardinality or token-creating path is lost behind it.
            # ``_build_producer_index`` seeds an entry for EVERY queue node in
            # the same state this walk traverses, so a missing entry is index
            # corruption, not an empty queue — an empty-tuple default would
            # silently truncate the upstream trace and drop the guard.
            if node.id not in producers.queue_predecessors:
                raise RuntimeError(f"Queue node {node.id!r} missing from the producer index")
            predecessors = producers.queue_predecessors[node.id]
            if len(predecessors) > 1:
                has_combining_fan_in = True
            for predecessor in predecessors:
                walk_producer(predecessor)
            return

        marker = _fanout_marker_for_node(node)
        if marker is not None:
            markers.append(marker)
            if node.node_type != "gate":
                # Every non-gate marker arm (token-creating transform,
                # transform-mode aggregation, collector) has an unprovable
                # multiplier; a fork gate's is exactly 1 per branch.
                has_unbounded_fanout = True

        if node.node_type in ("coalesce", "row_union"):
            # Coalesce keeps its legacy adapter input traversal. A row_union's
            # input is only the schema-required alias of its first branch, so
            # every real consuming path comes from branches.
            if node.node_type == "coalesce" and node.input:
                walk_label(node.input)
            branch_connections = _coalesce_branch_connections(node.branches)
            if node.node_type == "row_union" and len(set(branch_connections)) > 1:
                # A row_union CONCATENATES branch streams; a coalesce merges a
                # row-group back into one token and combines nothing.
                has_combining_fan_in = True
            for branch in branch_connections:
                walk_label(branch)
            return

        walk_label(node.input)

    walk_label(input_label)
    return _FanoutTrace(
        markers=tuple(dict.fromkeys(markers)),
        source_markers=tuple(dict.fromkeys(source_markers)),
        source_estimated_rows=source_estimated_rows,
        has_unknown_cardinality=has_unknown_cardinality,
        has_unbounded_fanout=has_unbounded_fanout,
        has_combining_fan_in=has_combining_fan_in,
    )


def _fanout_marker_for_node(node: NodeSpec) -> str | None:
    if node.node_type == "transform":
        if node.plugin is None:
            raise RuntimeError(f"Transform node {node.id!r} has no plugin")
        transform_cls = get_shared_plugin_manager().get_transform_by_name(node.plugin)
        if transform_cls.creates_tokens:
            return f"transform:{node.id}:{node.plugin}"
        return None

    if node.node_type == "aggregation" and node.output_mode == "transform":
        return f"aggregation:{node.id}:output_mode=transform"

    if node.node_type == "collector":
        # UNCONDITIONAL, and that is the derived answer rather than a lazy one
        # (elspeth-df8082552d). Every other arm above narrows on a declaration
        # that bounds output cardinality — ``creates_tokens`` for a transform,
        # ``output_mode`` for an aggregation, ``fork_to`` for a gate. A
        # collector has no such declaration and no such bound:
        # ``CollectorExecutor`` flushes
        # ``output_rows = (result.row,) if result.row is not None else
        # tuple(result.rows or ())`` and hands it straight to
        # ``collect_tokens(output_rows=…)``, which mints a fresh EXPAND group
        # per output row. Any batch-aware plugin may return ``success_multi``
        # of arbitrary length, so the multiplier is unbounded — precisely the
        # case this module's docstring says to treat as unbounded "unless a
        # later implementation can prove a tighter source-to-output
        # multiplier". Nothing proves one today.
        #
        # TRAP — do NOT "tighten" this arm to ``transform_cls.creates_tokens``
        # by analogy with the transform arm. That flag is a ROW-STAGE token-
        # minting switch (base.py ~:409: it governs whether the row processor
        # mints new token_ids for a ``success_multi`` return, and when False
        # the processor expects same-count passthrough). The collector release
        # path never reads it — ``collector.py`` contains zero references to
        # ``creates_tokens`` — and ``batch_replicate`` is the counter-example
        # that would break silently: it INHERITS ``creates_tokens=False`` (the
        # flag appears nowhere in ``batch_replicate.py`` — verify by reading the
        # attribute off the class, not by grepping the file), is
        # collector-eligible, and multiplies M = sum(copies). Keying on that
        # flag returns None for it and reinstates exactly the gap this arm
        # closes.
        if node.plugin is None:
            raise RuntimeError(f"Collector node {node.id!r} has no plugin")
        return f"collector:{node.id}:{node.plugin}"

    if node.node_type == "gate" and node.fork_to is not None and len(node.fork_to) > 1:
        return f"gate:{node.id}:fork_to={len(node.fork_to)}"

    return None


def _provider_calls_per_row(options: Mapping[str, Any]) -> int:
    queries = options["queries"] if "queries" in options else None
    if queries is None:
        return 1
    if isinstance(queries, Mapping):
        return max(len(queries), 1)
    if isinstance(queries, Sequence) and not isinstance(queries, str | bytes | bytearray):
        return max(len(queries), 1)
    return 1


def _estimate_source_rows(source: SourceSpec, *, data_dir: Path) -> int | None:
    if source.plugin == "llm":
        # One authored prompt can emit at most one row; validation discard,
        # provider failure, or shutdown may reduce that count to zero.
        return 1
    path = _source_path(source, data_dir=data_dir)
    if path is None:
        return _remote_source_limit(source)

    try:
        if source.plugin == "text":
            return _count_text_source_rows(path, source.options)
        if source.plugin == "csv":
            return _count_csv_source_rows(path, source.options)
        if source.plugin == "json":
            return _count_json_source_rows(path, source.options)
    except (OSError, UnicodeDecodeError, csv.Error, json.JSONDecodeError):
        return None
    return None


def _source_path(source: SourceSpec, *, data_dir: Path) -> Path | None:
    raw_path = source.options["path"] if "path" in source.options else source.options["file"] if "file" in source.options else None
    if not isinstance(raw_path, str):
        return None
    return resolve_data_path(raw_path, str(data_dir))


def _remote_source_limit(source: SourceSpec) -> int | None:
    for key in ("top", "limit", "max_rows"):
        raw_value = source.options[key] if key in source.options else None
        if isinstance(raw_value, int):
            return raw_value
    return None


def _count_text_source_rows(path: Path, options: Mapping[str, Any]) -> int:
    encoding = _string_option(options, "encoding") or "utf-8"
    skip_blank_lines = bool(options["skip_blank_lines"]) if "skip_blank_lines" in options else True
    strip_whitespace = bool(options["strip_whitespace"]) if "strip_whitespace" in options else True
    count = 0
    with path.open(encoding=encoding, errors="surrogateescape", newline="") as handle:
        for raw_line in handle:
            value = raw_line.rstrip("\r\n")
            if strip_whitespace:
                value = value.strip()
            if skip_blank_lines and value == "":
                continue
            count += 1
    return count


def _count_csv_source_rows(path: Path, options: Mapping[str, Any]) -> int | None:
    encoding = _string_option(options, "encoding") or "utf-8"
    delimiter = _string_option(options, "delimiter") or ","
    skip_rows = options["skip_rows"] if "skip_rows" in options and isinstance(options["skip_rows"], int) else 0
    columns = options["columns"] if "columns" in options else None
    with path.open(encoding=encoding, errors="surrogateescape", newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter, strict=True)
        for _ in range(skip_rows):
            next(reader, None)
        if columns is None and next(reader, None) is None:
            return 0
        return sum(1 for _ in reader)


def _count_json_source_rows(path: Path, options: Mapping[str, Any]) -> int | None:
    encoding = _string_option(options, "encoding") or "utf-8"
    raw_format = _string_option(options, "format")
    fmt = raw_format or ("jsonl" if path.suffix == ".jsonl" else "json")
    data_key = _string_option(options, "data_key")

    if fmt == "jsonl":
        with path.open(encoding=encoding) as handle:
            return sum(1 for line in handle if line.strip())

    with path.open(encoding=encoding) as handle:
        payload = json.load(handle)
    if data_key is not None:
        if not isinstance(payload, Mapping):
            return None
        payload = payload[data_key] if data_key in payload else None
    if isinstance(payload, list):
        return len(payload)
    return None


def _string_option(options: Mapping[str, Any], key: str) -> str | None:
    value = options[key] if key in options else None
    if isinstance(value, str) and value.strip():
        return value
    return None


def _credential_ref(options: Mapping[str, Any]) -> str | None:
    raw_api_key = options["api_key"] if "api_key" in options else None
    if isinstance(raw_api_key, Mapping):
        secret_ref = raw_api_key["secret_ref"] if "secret_ref" in raw_api_key else None
        if isinstance(secret_ref, str) and secret_ref.strip():
            return f"secret_ref:{secret_ref}"
    if isinstance(raw_api_key, str) and raw_api_key.strip():
        return "inline_api_key"
    return None


def _risk_message(
    *,
    node_id: str,
    provider: str,
    model: str | None,
    estimated_provider_calls: int | None,
    provider_calls_per_row: int,
    markers: Sequence[str],
    deterministic_fanout: bool,
) -> str:
    provider_label = _provider_label(provider)
    model_text = f" model {model}" if model is not None else ""
    if estimated_provider_calls is None:
        if deterministic_fanout:
            # Fork-only topology over an unestimable source: the total is
            # unknown but the per-row count is provable — say it, so the
            # operator can sanity-check the multiplier before acknowledging.
            return (
                f"LLM transform '{node_id}' makes {provider_calls_per_row} {provider_label}{model_text} "
                "call(s) per source row; the source row count could not be estimated."
            )
        source_text = " after fanout" if markers else ""
        return f"LLM transform '{node_id}' may make an unknown number of {provider_label}{model_text} calls{source_text}."
    if markers:
        return (
            f"LLM transform '{node_id}' may make {estimated_provider_calls} {provider_label}{model_text} call(s) "
            f"({provider_calls_per_row} per source row through deterministic fork fan-out)."
        )
    return f"LLM transform '{node_id}' may make {estimated_provider_calls} {provider_label}{model_text} call(s)."


def _guard_summary(risks: Sequence[ExecutionFanoutRisk], *, all_deterministic: bool) -> str:
    if len(risks) == 1:
        risk = risks[0]
        if risk.estimated_provider_calls is None and all_deterministic:
            calls = f"{risk.provider_calls_per_row} provider call(s) per source row (source row count unknown)"
        elif risk.estimated_provider_calls is None:
            calls = "an unknown number of provider calls"
        else:
            calls = f"{risk.estimated_provider_calls} provider call(s)"
        model = f" model {risk.model}" if risk.model is not None else ""
        return f"Confirm LLM fanout before execution: node '{risk.node_id}' uses {risk.provider}{model} and may make {calls}."
    if all_deterministic:
        # Every node's per-row multiplier is provably 1, so the pipeline-level
        # per-source-row cost is the plain sum — the number that lets an
        # operator notice "that is not what I meant" before spending.
        per_row_total = sum(risk.provider_calls_per_row for risk in risks)
        estimates = [risk.estimated_provider_calls for risk in risks]
        if all(estimate is not None for estimate in estimates):
            total = sum(estimate for estimate in estimates if estimate is not None)
            return (
                f"Confirm LLM fanout before execution: {len(risks)} LLM nodes make {per_row_total} "
                f"provider call(s) per source row (estimated {total} total)."
            )
        return (
            f"Confirm LLM fanout before execution: {len(risks)} LLM nodes make {per_row_total} "
            "provider call(s) per source row; the source row count could not be estimated."
        )
    return f"Confirm LLM fanout before execution: {len(risks)} LLM nodes may make high-cardinality provider calls."


def _provider_label(provider: str) -> str:
    if provider == "unknown":
        return "provider"
    return provider
