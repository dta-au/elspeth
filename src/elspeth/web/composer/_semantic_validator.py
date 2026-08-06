"""Generic semantic-contract validator.

For each consumer node with declared input_semantic_requirements(),
walks back to the effective upstream producer (using ProducerResolver),
asks the producer for its output_semantics(), and compares facts to
requirements per field. Emits structured SemanticEdgeContract records
and high-severity ValidationEntry errors on CONFLICT or on UNKNOWN +
FAIL policy.

UNKNOWN + WARN emits an advisory ValidationEntry instead of an error.
WARN is the honest policy wherever a required property is not statically
decidable for some legitimate producer: blocking would make the consumer
unwireable rather than fail-closed, while dropping it silently would hide
a real contract gap. The warning discloses the gap and leaves the
runtime's ``on_error`` route to handle a value that turns out wrong.
Both current consumers are WARN — ``json_explode`` because no producer
can declare ``SemanticValueType.LIST`` at all, and ``line_explode``
because a generative producer's line framing is knowable only per row
(ADR-039). Reserve FAIL for a requirement every legitimate producer can
declare; ``tests/invariants/test_semantic_requirements_are_satisfiable``
enforces that a FAIL requirement has at least one possible producer.

``unknown_policy`` never governs a CONFLICT. A CONFLICT is a fact-level
contradiction and blocks under every policy, because ``compare_semantic``
short-circuits it ahead of UNKNOWN. That is precisely what makes the
relaxed policies safe: relaxing one cannot unblock a wrong composition,
only a silent one.

Consumers are transform NODES and sink OUTPUTS. Sinks are not in
``state.nodes`` — ``NodeType`` has no sink member — so they get their own
loop over ``state.outputs``, and their producers come from
``ProducerResolver.sink_producers`` because sink-targeted edges are
deliberately absent from the connection map. A sink may have several
producers (fan-in); the rule is conservative — CONFLICT if ANY producer
conflicts.

This module reuses ProducerResolver rather than adding another walk-back.
``state._walk_producer_entry_to_real_producer`` and its
``_walk_to_real_producer`` wrapper remain a second implementation, kept
because they emit skip-with-warning entries and stop at coalesce/row_union
boundaries the resolver traverses. A change to producer walking must be
mirrored across both, never assumed unique.

Pass-through propagation is intentionally NOT performed. A pass-through
transform between a declared producer and a declared consumer breaks the
chain, so the consumer sees outcome=UNKNOWN and its own ``unknown_policy``
grades it. This is by design: it forces a real propagation API decision
rather than an ad hoc one.

Layer: L3.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from elspeth.contracts.plugin_semantics import (
    FieldSemanticFacts,
    FieldSemanticRequirement,
    OutputSemanticDeclaration,
    SemanticEdgeContract,
    SemanticOutcome,
    UnknownSemanticPolicy,
    compare_semantic,
)
from elspeth.web.composer._producer_resolver import (
    ProducerEntry,
    ProducerResolver,
    is_source_producer_id,
)
from elspeth.web.composer._validation_probe import prepare_validation_probe_options
from elspeth.web.composer.state import (
    CompositionState,
    NodeSpec,
    OutputSpec,
    SchemaContractDetail,
    Severity,
    SourceSpec,
    ValidationEntry,
    _is_config_probe_exception,
    _is_sink_config_probe_exception,
    _is_source_config_probe_exception,
)

if TYPE_CHECKING:
    from elspeth.plugins.infrastructure.base import BaseSink, BaseSource, BaseTransform


def _instantiate_consumer(node: NodeSpec) -> BaseTransform | None:
    """Construct a consumer transform instance to read its requirements.

    The caller owns every non-None instance and must close it in ``finally``.

    Returns None when:
    - node.plugin is unset (typed-None on a draft node)
    - construction fails with one of the established probe-tolerant exception
      types (PluginConfigError, PluginNotFoundError, TemplateError,
      UnknownPluginTypeError, or "Invalid configuration for transform"
      ValueError). This mirrors the producer-probe tolerance in
      ``_safe_output_semantics`` and the existing schema-contract probe
      tolerance in ``state._check_schema_contracts``.

    Why both probes share tolerance: ``CompositionState.validate()`` has an
    established contract that draft pipelines (incomplete config, unregistered
    plugins) do not crash validation — see
    ``test_contract_probe_constructor_exception_falls_back_instead_of_crashing``.
    The semantic validator runs inside ``validate()`` and must respect that
    contract. Phase 3 semantic-validation fixtures (``_wardline_state``)
    construct valid producer+consumer configs and assert specific outcomes
    (``len(errors) == 1``, ``outcome == CONFLICT``), so they never enter the
    tolerant branch — silent skip would surface as a positive-assertion
    failure, preserving the vacuousness guard.

    Unexpected exceptions PROPAGATE — a plugin method raising mid-construction
    is a system bug per CLAUDE.md plugin-as-system-code policy.
    """
    from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager

    if node.plugin is None:
        return None
    try:
        return cast(
            "BaseTransform",
            get_shared_plugin_manager().create_transform(
                node.plugin,
                prepare_validation_probe_options(node.options),
            ),
        )
    except Exception as exc:
        if _is_config_probe_exception(exc):
            return None
        raise


def _source_spec_for(producer: ProducerEntry, sources: Mapping[str, SourceSpec]) -> SourceSpec | None:
    """Return the SourceSpec a source producer id names, or None.

    Inverse of ``source_producer_id``: the legacy id ``"source"`` keys the
    default named source, ``"source:<name>"`` keys ``<name>``.
    """
    source_name = "source" if producer.producer_id == "source" else producer.producer_id.removeprefix("source:")
    return sources.get(source_name)


def _instantiate_source_producer(producer: ProducerEntry, sources: Mapping[str, SourceSpec]) -> BaseSource | None:
    """Construct a source instance to read its facts.

    ``on_validation_failure`` is a ``SourceSpec`` field rather than a plugin
    option, but every source config model REQUIRES it — so probing with
    ``producer.options`` alone raises a draft-config error for every source
    and the probe would report abstention while checking nothing. It is
    merged back in here, at the probe, rather than into ``ProducerEntry``:
    the entry must keep naming the node's real options for its other readers.
    """
    from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager

    if producer.plugin_name is None:
        return None
    source_spec = _source_spec_for(producer, sources)
    if source_spec is None:
        return None
    probe_options = prepare_validation_probe_options(source_spec.options)
    probe_options["on_validation_failure"] = source_spec.on_validation_failure
    return cast(
        "BaseSource",
        get_shared_plugin_manager().create_source(producer.plugin_name, probe_options),
    )


def _instantiate_transform_producer(producer: ProducerEntry) -> BaseTransform | None:
    """Construct a producer transform instance to read its facts."""
    from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager

    if producer.plugin_name is None:
        return None
    return cast(
        "BaseTransform",
        get_shared_plugin_manager().create_transform(
            producer.plugin_name,
            prepare_validation_probe_options(producer.options),
        ),
    )


def _safe_output_semantics(
    producer: ProducerEntry,
    sources: Mapping[str, SourceSpec],
) -> OutputSemanticDeclaration | None:
    """Construct producer and read its semantics, tolerating draft-config errors.

    Dispatches on the producer id: a source root is built with
    ``create_source`` and a transform with ``create_transform``. Using the
    wrong factory would raise PluginNotFoundError, or silently construct a
    same-named plugin of the other kind. Each branch tolerates only its own
    factory's draft-config failure.

    Returns None when:
    - Producer has no plugin name (a structural node: coalesce, queue, row_union)
    - The producer id names a source that is not in ``sources``
    - Plugin construction fails with an expected draft/config probe error

    Unexpected exceptions PROPAGATE — they indicate a framework bug
    (per CLAUDE.md plugin-as-system-code policy: a plugin method that
    raises is a bug we MUST know about).
    """
    # Recognize both the legacy "source" id and named "source:<name>" ids;
    # a named source must never be mis-probed as a transform.
    is_source = is_source_producer_id(producer.producer_id)
    tolerated = _is_source_config_probe_exception if is_source else _is_config_probe_exception
    instance: BaseSource | BaseTransform | None
    try:
        instance = _instantiate_source_producer(producer, sources) if is_source else _instantiate_transform_producer(producer)
    except Exception as exc:
        if tolerated(exc):
            return None
        raise

    if instance is None:
        return None
    try:
        return instance.output_semantics()
    finally:
        instance.close()


def _instantiate_sink_consumer(output: OutputSpec) -> BaseSink | None:
    """Construct a sink instance to read its requirements.

    The caller owns every non-None instance and must close it in ``finally``.

    Uses ``create_sink``, never ``create_transform``: the two registries are
    separate, so the transform factory would raise ``PluginNotFoundError`` for
    every sink name — or, worse, silently build an unrelated transform that
    happens to share the name.

    Unlike the transform-consumer probe, sink draft-config failures ARE
    tolerated. A sink's options are authored on the same in-progress
    composition as everything else, and the schema-contract layer already
    abstains on an unconstructable sink; crashing here would make a half-typed
    output path fail validation outright instead of reporting what is missing.
    """
    from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager

    try:
        return cast(
            "BaseSink",
            get_shared_plugin_manager().create_sink(
                output.plugin,
                prepare_validation_probe_options(output.options),
            ),
        )
    except Exception as exc:
        if _is_sink_config_probe_exception(exc):
            return None
        raise


def _find_producer_facts(
    declaration: OutputSemanticDeclaration,
    field_name: str,
) -> FieldSemanticFacts | None:
    for facts in declaration.fields:
        if facts.field_name == field_name:
            return facts
    return None


def _targeted_conflict_repair_hint(
    *,
    consumer_plugin: str,
    producer_plugin: str | None,
    requirement_code: str,
    facts: FieldSemanticFacts | None,
) -> str | None:
    if (
        consumer_plugin == "json_explode"
        and producer_plugin == "llm"
        and requirement_code == "json_explode.array_field.list"
        and facts is not None
        and facts.value_type.value == "str"
    ):
        return (
            "json_explode requires list field; LLM response_field is str. "
            "Use structured-output parsing or a transform that parses JSON object/string first."
        )
    return None


@dataclass(frozen=True, slots=True)
class _ConsumerRef:
    """Identity of one semantic consumer, whichever surface it lives on.

    Transform nodes and sink outputs are different composer objects with
    different id conventions, so the shared evaluation below addresses them
    through this record rather than branching on type.

    ``component`` is the ``ValidationEntry`` attribution and ``contract_id``
    the ``SemanticEdgeContract.to_id``. They differ for a node (``node:x`` vs
    ``x``) and coincide for a sink (``output:x``) — deliberately, because
    ``assistance_suggestion_for`` matches an entry to its contract by stripping
    the ``node:`` prefix, and ``output:<name>`` is already the id the
    schema-contract layer gives a sink edge.
    """

    component: str
    contract_id: str
    plugin: str
    description: str


def _record_undeclared_producer(
    *,
    consumer: _ConsumerRef,
    requirement: FieldSemanticRequirement,
    errors: list[ValidationEntry],
    warnings: list[ValidationEntry],
    contracts: list[SemanticEdgeContract],
    seen_edges: set[tuple[str, str, str, str]],
) -> None:
    """Emit the UNKNOWN finding for a consumer with no resolvable producer.

    No declared producer means coalesce, ambiguity, or an unreachable input.
    The schema-contract layer owns missing-field cases; the semantic layer
    treats this as unknown and lets the requirement's own policy grade it.
    """
    edge_key = ("?source", consumer.contract_id, requirement.field_name, requirement.field_name)
    if edge_key in seen_edges:
        return
    seen_edges.add(edge_key)
    contracts.append(
        SemanticEdgeContract(
            from_id="?",
            to_id=consumer.contract_id,
            consumer_plugin=consumer.plugin,
            producer_plugin=None,
            producer_field=requirement.field_name,
            consumer_field=requirement.field_name,
            producer_facts=None,
            requirement=requirement,
            outcome=SemanticOutcome.UNKNOWN,
        )
    )
    if requirement.unknown_policy is UnknownSemanticPolicy.ALLOW:
        return
    entry = ValidationEntry(
        consumer.component,
        (
            f"Semantic contract: {consumer.description} "
            f"requires field '{requirement.field_name}' "
            f"({requirement.requirement_code}) but the upstream producer is "
            f"undeclared (coalesce, ambiguous, or unreachable)."
        ),
        cast(Severity, requirement.severity),
        "semantic_contract_violation",
    )
    if requirement.unknown_policy is UnknownSemanticPolicy.FAIL:
        errors.append(entry)
    else:
        warnings.append(entry)


def _record_producer_edge(
    *,
    consumer: _ConsumerRef,
    producer: ProducerEntry,
    producer_decl: OutputSemanticDeclaration | None,
    requirement: FieldSemanticRequirement,
    errors: list[ValidationEntry],
    warnings: list[ValidationEntry],
    contracts: list[SemanticEdgeContract],
    seen_edges: set[tuple[str, str, str, str]],
) -> None:
    """Compare one producer's facts to one requirement and record the outcome.

    Called once per (producer, requirement) pair. A sink with fan-in calls it
    once per producer, which is what makes the conservative rule hold: every
    conflicting producer contributes its own blocking error, so ANY conflict
    blocks the composition.
    """
    facts = _find_producer_facts(producer_decl, requirement.field_name) if producer_decl is not None else None
    outcome = compare_semantic(facts, requirement)

    edge_key = (producer.producer_id, consumer.contract_id, requirement.field_name, requirement.field_name)
    if edge_key in seen_edges:
        return
    seen_edges.add(edge_key)

    contracts.append(
        SemanticEdgeContract(
            from_id=producer.producer_id,
            to_id=consumer.contract_id,
            consumer_plugin=consumer.plugin,
            producer_plugin=producer.plugin_name,
            producer_field=requirement.field_name,
            consumer_field=requirement.field_name,
            producer_facts=facts,
            requirement=requirement,
            outcome=outcome,
        )
    )

    producer_label = producer.plugin_name if producer.plugin_name is not None else "(unknown plugin)"

    if outcome is SemanticOutcome.CONFLICT:
        facts_kind = facts.content_kind.value if facts else "unknown"
        facts_framing = facts.text_framing.value if facts else "unknown"
        facts_value_type = facts.value_type.value if facts else "unknown"
        # Generic diagnostic: name producer, consumer, field, observed
        # producer facts, and the requirement_code. Do NOT enumerate accepted
        # enum sets — that prints values like "markdown" / "newline_framed"
        # which read as fix prose ("use markdown"). Fix prose belongs in
        # PluginAssistance, addressed by requirement_code.
        message = (
            f"Semantic contract violation: '{producer.producer_id}' "
            f"-> '{consumer.contract_id}'. Consumer ({consumer.plugin}) requires field "
            f"'{requirement.field_name}' to satisfy {requirement.requirement_code}, "
            f"but producer ({producer_label}) declares "
            f"content_kind={facts_kind}, text_framing={facts_framing}, "
            f"value_type={facts_value_type}."
        )
        repair_hint = _targeted_conflict_repair_hint(
            consumer_plugin=consumer.plugin,
            producer_plugin=producer.plugin_name,
            requirement_code=requirement.requirement_code,
            facts=facts,
        )
        if repair_hint is not None:
            message = f"{message} {repair_hint}"
        errors.append(
            ValidationEntry(
                consumer.component,
                message,
                cast(Severity, requirement.severity),
                "semantic_contract_violation",
                # Producer/consumer ids are pipeline identifiers, never row
                # content — safe for the planner's redacted repair feedback
                # (see SchemaContractDetail).
                contract=SchemaContractDetail(
                    producer=producer.producer_id,
                    consumer=consumer.contract_id,
                ),
            )
        )
        return

    if outcome is not SemanticOutcome.UNKNOWN or requirement.unknown_policy is UnknownSemanticPolicy.ALLOW:
        return

    if facts is None:
        semantic_detail = (
            "declares no semantic facts for that field. Producers semantically feeding this consumer must declare output_semantics()."
        )
    else:
        semantic_detail = (
            "does not declare all semantic dimensions required "
            "for that field. Declared facts: "
            f"content_kind={facts.content_kind.value}, "
            f"text_framing={facts.text_framing.value}, "
            f"value_type={facts.value_type.value}."
        )
    entry = ValidationEntry(
        consumer.component,
        (
            f"Semantic contract: {consumer.description} "
            f"requires field '{requirement.field_name}' "
            f"({requirement.requirement_code}) but upstream producer "
            f"'{producer.producer_id}' ({producer_label}) "
            f"{semantic_detail}"
        ),
        cast(Severity, requirement.severity),
        "semantic_contract_violation",
        contract=SchemaContractDetail(
            producer=producer.producer_id,
            consumer=consumer.contract_id,
        ),
    )
    if requirement.unknown_policy is UnknownSemanticPolicy.FAIL:
        errors.append(entry)
    else:
        warnings.append(entry)


def _sink_upstream_producers(resolver: ProducerResolver, sink_name: str) -> tuple[ProducerEntry, ...]:
    """Return every real upstream producer of a sink, deduplicated, in order.

    Sink-targeted edges are absent from the connection map by construction, so
    the immediate producers come from ``sink_producers`` and each is then
    walked back through structural gates. The connection-map fallback covers
    the residual case where a sink name is nonetheless resolvable as a
    connection; it is the same rule the schema-contract layer applies.

    Several route labels from one gate onto one sink resolve to one upstream:
    dedupe on producer_id so a sink is not reported as conflicting twice for
    what is one edge.
    """
    direct = resolver.sink_producers(sink_name)
    if not direct:
        walked = resolver.walk_to_real_producer(sink_name)
        return () if walked is None else (walked,)

    resolved: list[ProducerEntry] = []
    seen: set[str] = set()
    for entry in direct:
        actual = resolver.walk_entry_to_real_producer(entry)
        if actual is None or actual.producer_id in seen:
            continue
        seen.add(actual.producer_id)
        resolved.append(actual)
    return tuple(resolved)


def validate_semantic_contracts(
    state: CompositionState,
) -> tuple[tuple[ValidationEntry, ...], tuple[ValidationEntry, ...], tuple[SemanticEdgeContract, ...]]:
    """Validate semantic contracts across the composition.

    Returns (errors, warnings, contracts):
    - errors: ValidationEntry records suitable for ValidationSummary.errors
    - warnings: advisory records for ValidationSummary.warnings, raised on
      UNKNOWN + WARN policy — disclosed but non-blocking
    - contracts: SemanticEdgeContract records for ValidationSummary.semantic_contracts
    """
    errors: list[ValidationEntry] = []
    warnings: list[ValidationEntry] = []
    contracts: list[SemanticEdgeContract] = []
    seen_edges: set[tuple[str, str, str, str]] = set()

    sink_names = frozenset(output.name for output in state.outputs)
    resolver = ProducerResolver.build(
        source=None,
        sources=state.sources,
        nodes=state.nodes,
        sink_names=sink_names,
    )

    for node in state.nodes:
        if node.node_type != "transform" or node.plugin is None:
            continue

        # CONSUMER probe failures must NOT be silently tolerated. The
        # consumer is the entry point — if it can't construct, the
        # test/composition has a bug we MUST surface, otherwise the
        # validator silently skips the case it's meant to check.
        # Producer probes are tolerant (separate _safe_output_semantics).
        consumer_instance = _instantiate_consumer(node)
        if consumer_instance is None:
            continue
        try:
            requirements = consumer_instance.input_semantic_requirements()
        finally:
            consumer_instance.close()
        if not requirements.fields:
            continue

        consumer = _ConsumerRef(
            component=f"node:{node.id}",
            contract_id=node.id,
            plugin=node.plugin,
            description=f"'{node.plugin}' node '{node.id}'",
        )
        upstream_producer = resolver.walk_to_real_producer(node.input)
        if upstream_producer is None:
            for req in requirements.fields:
                _record_undeclared_producer(
                    consumer=consumer,
                    requirement=req,
                    errors=errors,
                    warnings=warnings,
                    contracts=contracts,
                    seen_edges=seen_edges,
                )
            continue

        producer_decl = _safe_output_semantics(upstream_producer, state.sources)
        for req in requirements.fields:
            _record_producer_edge(
                consumer=consumer,
                producer=upstream_producer,
                producer_decl=producer_decl,
                requirement=req,
                errors=errors,
                warnings=warnings,
                contracts=contracts,
                seen_edges=seen_edges,
            )

    # Sinks are consumers too, and they are NOT in state.nodes — NodeType has
    # no sink member, so relaxing the node filter above could never reach one.
    # Without this loop every sink requirement is declared and never read: an
    # inert gate reporting success while checking nothing.
    for output in state.outputs:
        sink_instance = _instantiate_sink_consumer(output)
        if sink_instance is None:
            continue
        try:
            sink_requirements = sink_instance.input_semantic_requirements()
        finally:
            sink_instance.close()
        if not sink_requirements.fields:
            continue

        sink_consumer = _ConsumerRef(
            component=f"output:{output.name}",
            contract_id=f"output:{output.name}",
            plugin=output.plugin,
            description=f"'{output.plugin}' sink '{output.name}'",
        )
        sink_upstream = _sink_upstream_producers(resolver, output.name)
        if not sink_upstream:
            for req in sink_requirements.fields:
                _record_undeclared_producer(
                    consumer=sink_consumer,
                    requirement=req,
                    errors=errors,
                    warnings=warnings,
                    contracts=contracts,
                    seen_edges=seen_edges,
                )
            continue

        # Sink fan-in: evaluate EVERY producer. Each conflicting producer
        # contributes its own blocking error, so one bad arm blocks the sink
        # even when its siblings are satisfied.
        for sink_producer in sink_upstream:
            sink_producer_decl = _safe_output_semantics(sink_producer, state.sources)
            for req in sink_requirements.fields:
                _record_producer_edge(
                    consumer=sink_consumer,
                    producer=sink_producer,
                    producer_decl=sink_producer_decl,
                    requirement=req,
                    errors=errors,
                    warnings=warnings,
                    contracts=contracts,
                    seen_edges=seen_edges,
                )

    return tuple(errors), tuple(warnings), tuple(contracts)
