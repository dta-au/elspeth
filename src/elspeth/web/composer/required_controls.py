"""Auto-wire deployment-REQUIRED controls into pipeline candidates (R2-F10).

Layer: L3 (application).

Required-control enforcement used to be prose-only at authoring time
(``planner_authoring_aids``) and blocking only at execution
(``web.execution.validation``), while composer-side coverage findings were
demoted to warnings — so the compose loop felt no repair pressure and shipped
uncovered graphs that wedged at the run gate. The operator product decision
(elspeth-f99655f540) is **auto-wire + disclose**, not recommend-only:

* Insertion is driven by :func:`elspeth.web.plugin_policy.coverage.
  control_coverage_findings` — the single coverage authority. For each
  ``ControlMode.REQUIRED`` capability's ``input_not_dominated`` /
  ``output_not_post_dominated`` finding, the deployment-SELECTED
  implementation is spliced onto the offending edge, options drawn from the
  same exemplar machinery the authoring aids use.
* Every inserted node stages a pending ``pipeline_decision`` disclosure with
  the registered user_term ``required_control_auto_wired`` — the operator
  acknowledges the policy-mandated insertion on the ordinary review-card
  surface — and surfaces a ``policy_control`` implicit-decision entry
  (``implicit_decisions``).
* A REQUIRED-but-unselected capability inserts nothing: the existing
  ``required_control_unavailable`` finding is an operator problem, not
  authorable.
* Idempotence is mandatory: a covered graph passes through untouched, so the
  pass can run at every proposal-creation seam without double-splicing.

The pass runs inside the planner ``candidate_finalizer`` seam
(``pipeline_planner`` reclassifies finalizer ``AuditIntegrityError`` prefixes
as repair feedback), so it must NEVER raise on a malformed candidate — it
returns the candidate unchanged and lets downstream validation own the
rejection.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

from elspeth.contracts.plugin_capabilities import ControlMode, ControlRole, PluginCapability
from elspeth.plugins.infrastructure.manager import PluginNotFoundError, get_shared_plugin_manager
from elspeth.web.catalog.policy_view import PolicyCatalogView

# The aids' exemplar machinery is the single source of truth for how a
# selected control is authored (profile alias vs direct options); importing
# the private helpers — like the aids' own private-constant imports from
# interpretation_state — is deliberate, so the two surfaces can never drift.
from elspeth.web.composer.planner_authoring_aids import (
    _direct_control_options,
    _direct_control_options_are_deployable,
    _plugin_declares_field,
    _plugin_summaries,
    _selected_control_profile,
)
from elspeth.web.composer.state import (
    COMPOSER_NODE_TYPES,
    CoalesceBranches,
    CompositionState,
    NodeSpec,
    NodeType,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
    validate_composer_source_name,
)
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    REQUIRED_CONTROL_AUTO_WIRED_USER_TERM,
    ServerStagedRequiredControlUserTerm,
)

# Coverage's private per-stream predicates are imported deliberately: they are
# the credit authority, and re-deriving "is this on_success stream uncovered"
# with a second traversal could disagree with the walk that produced the
# finding (the same discipline coverage.py itself documents for its
# diagnosis probe).
from elspeth.web.plugin_policy.coverage import (
    _llm_output_fields,
    _llm_source_output_fields,
    _stream_proves_output_control,
    build_output_stream_graph,
    control_coverage_findings,
    node_has_blocking_control,
    node_has_capability,
    source_has_capability,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot

_REQUIRED_CONTROL_CAPABILITIES: Final[tuple[PluginCapability, ...]] = (
    PluginCapability.PROMPT_SHIELD,
    PluginCapability.CONTENT_SAFETY,
)

_AUTO_WIRE_ACTIONABLE_REASONS: Final[frozenset[str]] = frozenset({"input_not_dominated", "output_not_post_dominated"})
_MISSING: Final[object] = object()


@dataclass(frozen=True, slots=True)
class _CandidateSource:
    """One source block plus the authored container needed to rewrite it."""

    container: Literal["source", "sources"]
    name: str
    block: Mapping[str, Any]

    @property
    def component_id(self) -> str:
        return "source" if self.name == "source" else f"source:{self.name}"


def _disclosure_draft(
    *,
    capability: PluginCapability,
    control_plugin: str,
    llm_component_id: str,
    llm_component_kind: Literal["node", "source"],
    role: ControlRole,
    protected_fields: tuple[str, ...] = (),
    scanned_fields: tuple[str, ...] = (),
) -> str:
    """Operator-facing draft for the auto-wired disclosure review card."""
    edge = "input" if role is ControlRole.INPUT else "output"
    draft = (
        f"ELSPETH automatically inserted the deployment-required {capability.value} control "
        f"'{control_plugin}' on the {edge} path of llm {llm_component_kind} '{llm_component_id}'. Deployment policy "
        "makes this control mandatory on every such path; acknowledging records that you "
        "reviewed the inserted node. Removing it will block the pipeline at the required-control "
        f"gate unless an operator relaxes the {capability.value} control mode."
    )
    if scanned_fields and protected_fields:
        # Coverage populates scanned_fields only when a control already
        # provably dominates this edge with the WRONG field scope. The graph
        # then carries two controls, which is only explicable if the card
        # names the mismatch that made the second one necessary.
        draft += (
            f" An existing upstream control scans only [{', '.join(scanned_fields)}] while this "
            f"node reads [{', '.join(protected_fields)}], so a control scoped to the full "
            "protected set was inserted rather than modifying the existing control."
        )
    return draft


def _candidate_sources(candidate: Mapping[str, Any]) -> tuple[_CandidateSource, ...] | None:
    """Return authored sources without collapsing singular/plural provenance.

    Both containers at once are ambiguous even if one is empty: choosing one
    would silently reinterpret persisted proposal shape. Malformed source
    containers similarly return ``None`` so the finalizer can preserve the
    original candidate for downstream validation.
    """
    singular: object = candidate.get("source", _MISSING)
    plural: object = candidate.get("sources", _MISSING)
    if singular is not _MISSING and plural is not _MISSING:
        return None
    if singular is not _MISSING:
        if not isinstance(singular, Mapping):
            return None
        return (_CandidateSource(container="source", name="source", block=singular),)
    if plural is _MISSING:
        return ()
    if not isinstance(plural, Mapping):
        return None
    sources: list[_CandidateSource] = []
    for name, block in plural.items():
        if type(name) is not str or not isinstance(block, Mapping):
            return None
        sources.append(_CandidateSource(container="sources", name=name, block=block))
    return tuple(sources)


def _parse_node(raw: Mapping[str, Any]) -> NodeSpec:
    """Tolerant NodeSpec projection of one set_pipeline node dict."""
    node_id = raw["id"]
    node_type = raw["node_type"]
    plugin = raw.get("plugin")
    input_stream = raw.get("input", "")
    on_success = raw.get("on_success")
    on_error = raw.get("on_error")
    options = raw.get("options")
    condition = raw.get("condition")
    routes = raw.get("routes")
    fork_to = raw.get("fork_to")
    branches = raw.get("branches")
    policy = raw.get("policy")
    merge = raw.get("merge")
    if (
        type(node_id) is not str
        or not node_id
        or type(node_type) is not str
        or node_type not in COMPOSER_NODE_TYPES
        or (plugin is not None and type(plugin) is not str)
        or type(input_stream) is not str
        or (on_success is not None and type(on_success) is not str)
        or (on_error is not None and type(on_error) is not str)
        or (options is not None and not isinstance(options, Mapping))
        or (condition is not None and type(condition) is not str)
        or (policy is not None and type(policy) is not str)
        or (merge is not None and type(merge) is not str)
    ):
        raise TypeError("node fields have malformed types")
    if routes is not None and (
        not isinstance(routes, Mapping) or any(type(key) is not str or type(value) is not str for key, value in routes.items())
    ):
        raise TypeError("node routes must map exact strings")
    if fork_to is not None and (
        not isinstance(fork_to, Sequence) or isinstance(fork_to, (str, bytes)) or any(type(value) is not str for value in fork_to)
    ):
        raise TypeError("node fork_to must contain exact strings")
    parsed_branches: CoalesceBranches | None = None
    if branches is not None:
        if isinstance(branches, Mapping):
            if any(type(key) is not str or type(value) is not str for key, value in branches.items()):
                raise TypeError("node branches must map exact strings")
            parsed_branches = cast("Mapping[str, str]", branches)
        elif not isinstance(branches, Sequence) or isinstance(branches, (str, bytes)) or any(type(value) is not str for value in branches):
            raise TypeError("node branches must contain exact strings")
        else:
            parsed_branches = tuple(cast("Sequence[str]", branches))
    return NodeSpec(
        id=node_id,
        node_type=cast("NodeType", node_type),
        plugin=plugin,
        input=input_stream,
        on_success=on_success,
        on_error=on_error,
        options=options if isinstance(options, Mapping) else {},
        condition=condition,
        routes=routes,
        fork_to=tuple(fork_to) if isinstance(fork_to, Sequence) and not isinstance(fork_to, (str, bytes)) else None,
        branches=parsed_branches,
        policy=policy,
        merge=merge,
    )


def _parse_candidate_state(candidate: Mapping[str, Any]) -> CompositionState | None:
    """Project a set_pipeline candidate into a CompositionState for coverage.

    ``None`` means the candidate does not parse into the coverage authority's
    input shape; the caller returns the candidate untouched and downstream
    validation owns the rejection. This projection carries exactly what
    ``control_coverage_findings`` reads — nodes, source streams, sink names —
    and never becomes session state.
    """
    try:
        source_locations = _candidate_sources(candidate)
        if source_locations is None:
            return None
        sources: dict[str, SourceSpec] = {}
        for location in source_locations:
            validate_composer_source_name(location.name)
            sources[location.name] = _parse_source(location.block)
        raw_nodes = candidate.get("nodes")
        if not isinstance(raw_nodes, Sequence) or isinstance(raw_nodes, (str, bytes)):
            return None
        nodes: list[NodeSpec] = []
        for raw in raw_nodes:
            if not isinstance(raw, Mapping):
                return None
            nodes.append(_parse_node(raw))
        raw_outputs = candidate.get("outputs")
        if not isinstance(raw_outputs, Sequence) or isinstance(raw_outputs, (str, bytes)):
            return None
        outputs: list[OutputSpec] = []
        for raw in raw_outputs:
            if not isinstance(raw, Mapping):
                return None
            sink_name = raw["sink_name"]
            plugin = raw["plugin"]
            options = raw.get("options")
            on_write_failure = raw.get("on_write_failure") or "discard"
            if (
                type(sink_name) is not str
                or not sink_name
                or type(plugin) is not str
                or not plugin
                or (options is not None and not isinstance(options, Mapping))
                or type(on_write_failure) is not str
            ):
                return None
            outputs.append(
                OutputSpec(
                    name=sink_name,
                    plugin=plugin,
                    options=options if isinstance(options, Mapping) else {},
                    on_write_failure=on_write_failure,
                )
            )
        if len({node.id for node in nodes}) != len(nodes):
            # Duplicate ids make edge rewrites ambiguous; the candidate is
            # rejected downstream anyway.
            return None
        return CompositionState(
            sources=sources,
            nodes=tuple(nodes),
            edges=(),
            outputs=tuple(outputs),
            metadata=PipelineMetadata(),
            version=1,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _parse_source(block: Mapping[str, Any]) -> SourceSpec:
    options = block.get("options")
    plugin = block["plugin"]
    on_success = block.get("on_success", "")
    authored_validation_failure = block.get("on_validation_failure")
    on_validation_failure = "discard" if authored_validation_failure is None else authored_validation_failure
    if type(plugin) is not str or type(on_success) is not str or type(on_validation_failure) is not str or not isinstance(options, Mapping):
        raise TypeError("source string fields must be exact strings")
    return SourceSpec(
        plugin=plugin,
        on_success=on_success,
        options=options,
        on_validation_failure=on_validation_failure,
    )


def _reserved_names(state: CompositionState) -> set[str]:
    """Every identifier a new node id or stream name must not collide with."""
    names: set[str] = set()
    for node in state.nodes:
        names.add(node.id)
        if node.input:
            names.add(node.input)
        if node.on_success:
            names.add(node.on_success)
        if node.on_error:
            names.add(node.on_error)
        if node.routes:
            names.update(node.routes.values())
        if node.fork_to:
            names.update(node.fork_to)
        if node.branches:
            branches = node.branches
            names.update(branches.values() if isinstance(branches, Mapping) else branches)
    for source in state.sources.values():
        if source.on_success:
            names.add(source.on_success)
    names.update(output.name for output in state.outputs)
    return names


def _allocate_node_id(capability: PluginCapability, reserved: set[str]) -> str:
    """Deterministic ``{capability}_auto_{n}`` id whose derived streams are free."""
    n = 1
    while True:
        node_id = f"{capability.value}_auto_{n}"
        if node_id not in reserved and f"{node_id}_in" not in reserved and f"{node_id}_out" not in reserved:
            return node_id
        n += 1


def _control_options(
    *,
    plugin_name: str,
    alias: str | None,
    fields: Sequence[str],
    summaries: Mapping[str, Any],
    role: ControlRole,
    disclosure_draft: str,
) -> dict[str, object]:
    """Author the inserted control node's options from the exemplar machinery."""
    options: dict[str, object] = {
        "fields": list(fields),
        "schema": {"mode": "observed"},
    }
    if alias is not None:
        # The operator-owned control binding stays behind the alias.
        options["profile"] = alias
    else:
        options.update(_direct_control_options(summaries, plugin_name))
    if role is ControlRole.OUTPUT and _plugin_declares_field(summaries, plugin_name, "source"):
        options["source"] = "OUTPUT"
    options[INTERPRETATION_REQUIREMENTS_KEY] = [
        {
            "kind": "pipeline_decision",
            "user_term": ServerStagedRequiredControlUserTerm(REQUIRED_CONTROL_AUTO_WIRED_USER_TERM),
            "draft": disclosure_draft,
        }
    ]
    return options


def _control_node_is_creditable(
    control: Mapping[str, object],
    *,
    capability: PluginCapability,
    role: ControlRole,
    protected_fields: Sequence[str],
) -> bool:
    """Ask the coverage credit authority whether the authored node would count.

    Guard against splice churn: if the options this pass authors were ever not
    creditable (a selected implementation whose ``is_effective_blocking_control``
    rejects them), the same finding would re-fire and the fixpoint would chain
    redundant nodes until budget exhaustion. Checking creditability BEFORE
    inserting means an un-authorable control inserts nothing and the
    diagnosable finding stays.
    """
    try:
        spec = _parse_node(control)
    except (KeyError, TypeError, ValueError):
        return False
    return node_has_blocking_control(spec, capability, role, protected_fields=frozenset(protected_fields))


def _splice_input_control(
    nodes: list[dict[str, object]],
    *,
    target_id: str,
    protected_fields: tuple[str, ...],
    scanned_fields: tuple[str, ...],
    capability: PluginCapability,
    plugin_name: str,
    alias: str | None,
    summaries: Mapping[str, Any],
    reserved: set[str],
) -> bool:
    """Interpose the shield on the target's input edge; True when spliced."""
    index = next((i for i, node in enumerate(nodes) if node.get("id") == target_id), None)
    if index is None:
        return False
    target = nodes[index]
    upstream = target.get("input")
    if not isinstance(upstream, str) or not upstream:
        return False
    if not protected_fields:
        # A field-scoped control can never be credited against an unprovable
        # protected set (only ``fields: all`` could, and not every selected
        # implementation supports it). The scope repair belongs to the author;
        # the diagnosable finding stays.
        return False
    new_id = _allocate_node_id(capability, reserved)
    out_stream = f"{new_id}_out"
    control: dict[str, object] = {
        "id": new_id,
        "node_type": "transform",
        "plugin": plugin_name,
        "input": upstream,
        "on_success": out_stream,
        "on_error": "discard",
        "options": _control_options(
            plugin_name=plugin_name,
            alias=alias,
            fields=protected_fields,
            summaries=summaries,
            role=ControlRole.INPUT,
            disclosure_draft=_disclosure_draft(
                capability=capability,
                control_plugin=plugin_name,
                llm_component_id=target_id,
                llm_component_kind="node",
                role=ControlRole.INPUT,
                protected_fields=protected_fields,
                scanned_fields=scanned_fields,
            ),
        ),
    }
    if not _control_node_is_creditable(control, capability=capability, role=ControlRole.INPUT, protected_fields=protected_fields):
        return False
    rewired = dict(target)
    rewired["input"] = out_stream
    nodes[index] = rewired
    nodes.insert(index, control)
    return True


def _splice_output_control(
    nodes: list[dict[str, object]],
    *,
    target: NodeSpec,
    state: CompositionState,
    capability: PluginCapability,
    plugin_name: str,
    alias: str | None,
    summaries: Mapping[str, Any],
    reserved: set[str],
) -> bool:
    """Interpose content safety on the target's on_success edge; True when spliced."""
    index = next((i for i, node in enumerate(nodes) if node.get("id") == target.id), None)
    if index is None:
        return False
    downstream = target.on_success
    if not isinstance(downstream, str) or not downstream:
        return False
    protected_fields = _llm_output_fields(target)
    if not protected_fields:
        return False
    graph = build_output_stream_graph(state.nodes)
    sink_streams = frozenset(output.name for output in state.outputs)
    if _stream_proves_output_control(
        downstream,
        graph,
        sink_streams=sink_streams,
        visited=frozenset({target.id}),
        protected_fields=protected_fields,
    ):
        # on_success is already covered: the finding's residue is an error
        # route or another edge the pass must not author around (a transform's
        # on_error may only name a sink or 'discard' — engine invariant — so
        # no control can sit there; that is the operator-decision case).
        return False
    new_id = _allocate_node_id(capability, reserved)
    in_stream = f"{new_id}_in"
    control: dict[str, object] = {
        "id": new_id,
        "node_type": "transform",
        "plugin": plugin_name,
        "input": in_stream,
        "on_success": downstream,
        "on_error": "discard",
        "options": _control_options(
            plugin_name=plugin_name,
            alias=alias,
            fields=tuple(sorted(protected_fields)),
            summaries=summaries,
            role=ControlRole.OUTPUT,
            disclosure_draft=_disclosure_draft(
                capability=capability,
                control_plugin=plugin_name,
                llm_component_id=target.id,
                llm_component_kind="node",
                role=ControlRole.OUTPUT,
            ),
        ),
    }
    if not _control_node_is_creditable(
        control,
        capability=capability,
        role=ControlRole.OUTPUT,
        protected_fields=tuple(sorted(protected_fields)),
    ):
        return False
    rewired = dict(nodes[index])
    rewired["on_success"] = in_stream
    nodes[index] = rewired
    nodes.insert(index + 1, control)
    return True


def _splice_source_output_control(
    candidate: dict[str, object],
    nodes: list[dict[str, object]],
    *,
    location: _CandidateSource,
    source: SourceSpec,
    state: CompositionState,
    capability: PluginCapability,
    plugin_name: str,
    alias: str | None,
    summaries: Mapping[str, Any],
    reserved: set[str],
) -> bool:
    """Interpose content safety on one source's successful output route."""
    if source.on_validation_failure != "discard":
        return False
    downstream = source.on_success
    if type(downstream) is not str or not downstream:
        return False
    protected_fields = _llm_source_output_fields(source)
    if not protected_fields:
        return False
    graph = build_output_stream_graph(state.nodes)
    sink_streams = frozenset(output.name for output in state.outputs)
    if _stream_proves_output_control(
        downstream,
        graph,
        sink_streams=sink_streams,
        visited=frozenset(),
        protected_fields=protected_fields,
    ):
        return False

    new_id = _allocate_node_id(capability, reserved)
    in_stream = f"{new_id}_in"
    sorted_fields = tuple(sorted(protected_fields))
    control: dict[str, object] = {
        "id": new_id,
        "node_type": "transform",
        "plugin": plugin_name,
        "input": in_stream,
        "on_success": downstream,
        "on_error": "discard",
        "options": _control_options(
            plugin_name=plugin_name,
            alias=alias,
            fields=sorted_fields,
            summaries=summaries,
            role=ControlRole.OUTPUT,
            disclosure_draft=_disclosure_draft(
                capability=capability,
                control_plugin=plugin_name,
                llm_component_id=location.component_id,
                llm_component_kind="source",
                role=ControlRole.OUTPUT,
            ),
        ),
    }
    if not _control_node_is_creditable(
        control,
        capability=capability,
        role=ControlRole.OUTPUT,
        protected_fields=sorted_fields,
    ):
        return False

    rewired_source = dict(location.block)
    rewired_source["on_success"] = in_stream
    if location.container == "source":
        candidate["source"] = rewired_source
    else:
        raw_sources = candidate.get("sources")
        if not isinstance(raw_sources, Mapping):
            return False
        rewired_sources = dict(raw_sources)
        rewired_sources[location.name] = rewired_source
        candidate["sources"] = rewired_sources
    nodes.append(control)
    return True


def wire_required_controls(
    candidate: Mapping[str, Any],
    snapshot: PluginAvailabilitySnapshot,
    catalog: PolicyCatalogView,
) -> Mapping[str, Any]:
    """Splice the deployment-selected REQUIRED controls onto uncovered edges.

    Returns the candidate object ITSELF unchanged (identity, not an equal
    copy — the finalizer contract; idempotence, malformed candidates,
    recommend-mode or unselected capabilities) or a new dict with control
    nodes inserted, each staging its ``required_control_auto_wired``
    disclosure. Never raises on candidate content — this runs inside the
    planner finalizer seam where an unprefixed exception is a terminal
    failure.
    """
    if catalog.snapshot is not snapshot:
        raise ValueError("plugin_snapshot_catalog_mismatch")
    modes = dict(snapshot.control_modes)
    selections: dict[PluginCapability, tuple[str, str | None]] = {}
    for capability in _REQUIRED_CONTROL_CAPABILITIES:
        if modes.get(capability, ControlMode.RECOMMEND) is not ControlMode.REQUIRED:
            continue
        selected = _selected_control_profile(catalog, capability)
        if selected is None:
            # REQUIRED but unselected/unavailable: insert nothing and leave
            # the required_control_unavailable finding — operator problem.
            continue
        selections[capability] = selected
    # Every no-op path returns the INPUT OBJECT, not an equal copy: the
    # finalizer seam's pre-existing contract (pinned by
    # test_shared_planner_surfaces) is identity on no-op, which keeps
    # byte-exactness/authority hashing downstream trivially intact.
    if not selections:
        return candidate
    source_locations = _candidate_sources(candidate)
    if source_locations is None:
        return candidate
    state = _parse_candidate_state(candidate)
    if state is None:
        return candidate
    try:
        source_manager = get_shared_plugin_manager()
        for source in state.sources.values():
            source_manager.get_source_by_name(source.plugin)
    except PluginNotFoundError:
        # Never repair one source while a sibling source is already known to
        # be invalid. Downstream validation must see the original proposal.
        return candidate
    llm_node_count = sum(1 for node in state.nodes if node_has_capability(node, PluginCapability.LLM))
    llm_source_locations = tuple(
        location for location in source_locations if source_has_capability(state.sources[location.name], PluginCapability.LLM)
    )
    if not llm_source_locations and llm_node_count == 0:
        # Coverage findings only exist for LLM components; skip the catalog sweep.
        return candidate
    if any(state.sources[location.name].on_validation_failure != "discard" for location in llm_source_locations):
        # A source validation-failure route cannot be interposed in the current
        # graph model. Refuse the WHOLE candidate before any transform/source
        # splice so this pass never partially certifies an unrepairable graph.
        return candidate

    summaries = _plugin_summaries(catalog)
    # SECURITY: an alias-less selection whose required options would come from
    # the aids' placeholder exemplar table is NOT deployable — a placeholder
    # endpoint (third-party registrable, suffix-only validated) must never be
    # baked into real node config carrying a live secret_ref. Treat it as
    # REQUIRED-but-unselected: insert nothing, leave the finding, and the aids
    # keep teaching manual wiring for that posture.
    selections = {
        capability: (plugin_name, alias)
        for capability, (plugin_name, alias) in selections.items()
        if alias is not None or _direct_control_options_are_deployable(summaries, plugin_name)
    }
    if not selections:
        return candidate
    working_nodes = [dict(node) for node in candidate["nodes"]]
    working_candidate: dict[str, object] = dict(candidate)
    working_candidate["nodes"] = working_nodes
    changed = False
    # Each successful splice permanently covers one source output or one of a
    # transform's two protected edges. This is also the hard upper bound if a
    # future coverage authority regresses and keeps reporting stale findings.
    budget = len(llm_source_locations) + (2 * llm_node_count)
    for _ in range(budget):
        state = _parse_candidate_state(working_candidate)
        if state is None:
            # A splice produced an unparseable candidate — impossible by
            # construction, but fail safe: keep what was authored so far.
            break
        reserved = _reserved_names(state)
        nodes_by_id = {node.id: node for node in state.nodes}
        current_locations = _candidate_sources(working_candidate)
        if current_locations is None:
            break
        sources_by_component = {location.component_id: (location, state.sources[location.name]) for location in current_locations}
        progressed = False
        for capability, (plugin_name, alias) in selections.items():
            for finding in control_coverage_findings(state, capability):
                if finding.reason not in _AUTO_WIRE_ACTIONABLE_REASONS:
                    continue
                if finding.component_type == "source":
                    source_target = sources_by_component.get(finding.component_id)
                    if source_target is None or finding.role is not ControlRole.OUTPUT:
                        continue
                    location, source = source_target
                    progressed = _splice_source_output_control(
                        working_candidate,
                        working_nodes,
                        location=location,
                        source=source,
                        state=state,
                        capability=capability,
                        plugin_name=plugin_name,
                        alias=alias,
                        summaries=summaries,
                        reserved=reserved,
                    )
                else:
                    target = nodes_by_id.get(finding.component_id)
                    if target is None:
                        continue
                    if finding.role is ControlRole.INPUT:
                        progressed = _splice_input_control(
                            working_nodes,
                            target_id=finding.component_id,
                            protected_fields=finding.protected_fields,
                            scanned_fields=finding.scanned_fields,
                            capability=capability,
                            plugin_name=plugin_name,
                            alias=alias,
                            summaries=summaries,
                            reserved=reserved,
                        )
                    else:
                        progressed = _splice_output_control(
                            working_nodes,
                            target=target,
                            state=state,
                            capability=capability,
                            plugin_name=plugin_name,
                            alias=alias,
                            summaries=summaries,
                            reserved=reserved,
                        )
                if progressed:
                    changed = True
                    break
            if progressed:
                break
        if not progressed:
            break
    if not changed:
        return candidate
    return working_candidate


__all__ = ["wire_required_controls"]
