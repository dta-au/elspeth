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
from typing import Any, Final

from elspeth.contracts.plugin_capabilities import ControlMode, ControlRole, PluginCapability
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
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    REQUIRED_CONTROL_AUTO_WIRED_USER_TERM,
)

# Coverage's private per-stream predicates are imported deliberately: they are
# the credit authority, and re-deriving "is this on_success stream uncovered"
# with a second traversal could disagree with the walk that produced the
# finding (the same discipline coverage.py itself documents for its
# diagnosis probe).
from elspeth.web.plugin_policy.coverage import (
    _llm_output_fields,
    _stream_proves_output_control,
    build_output_stream_graph,
    control_coverage_findings,
    node_has_blocking_control,
    node_has_capability,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot

_REQUIRED_CONTROL_CAPABILITIES: Final[tuple[PluginCapability, ...]] = (
    PluginCapability.PROMPT_SHIELD,
    PluginCapability.CONTENT_SAFETY,
)

_AUTO_WIRE_ACTIONABLE_REASONS: Final[frozenset[str]] = frozenset({"input_not_dominated", "output_not_post_dominated"})


def _disclosure_draft(
    *,
    capability: PluginCapability,
    control_plugin: str,
    llm_node_id: str,
    role: ControlRole,
    protected_fields: tuple[str, ...] = (),
    scanned_fields: tuple[str, ...] = (),
) -> str:
    """Operator-facing draft for the auto-wired disclosure review card."""
    edge = "input" if role is ControlRole.INPUT else "output"
    draft = (
        f"ELSPETH automatically inserted the deployment-required {capability.value} control "
        f"'{control_plugin}' on the {edge} path of llm node '{llm_node_id}'. Deployment policy "
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


def _parse_node(raw: Mapping[str, Any]) -> NodeSpec:
    """Tolerant NodeSpec projection of one set_pipeline node dict."""
    options = raw.get("options")
    fork_to = raw.get("fork_to")
    return NodeSpec(
        id=raw["id"],
        node_type=raw["node_type"],
        plugin=raw.get("plugin"),
        input=raw.get("input", ""),
        on_success=raw.get("on_success"),
        on_error=raw.get("on_error"),
        options=options if isinstance(options, Mapping) else {},
        condition=raw.get("condition"),
        routes=raw.get("routes"),
        fork_to=tuple(fork_to) if isinstance(fork_to, Sequence) and not isinstance(fork_to, (str, bytes)) else None,
        branches=raw.get("branches"),
        policy=raw.get("policy"),
        merge=raw.get("merge"),
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
        sources: dict[str, SourceSpec] = {}
        source_block = candidate.get("source")
        if isinstance(source_block, Mapping):
            sources["source"] = _parse_source(source_block)
        named_sources = candidate.get("sources")
        if isinstance(named_sources, Mapping):
            for name, block in named_sources.items():
                if not isinstance(name, str) or not isinstance(block, Mapping):
                    return None
                sources[name] = _parse_source(block)
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
            options = raw.get("options")
            outputs.append(
                OutputSpec(
                    name=raw["sink_name"],
                    plugin=raw["plugin"],
                    options=options if isinstance(options, Mapping) else {},
                    on_write_failure=raw.get("on_write_failure") or "discard",
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
    return SourceSpec(
        plugin=block["plugin"],
        on_success=block.get("on_success", ""),
        options=options if isinstance(options, Mapping) else {},
        on_validation_failure=block.get("on_validation_failure") or "discard",
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
            "user_term": REQUIRED_CONTROL_AUTO_WIRED_USER_TERM,
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
                llm_node_id=target_id,
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
                llm_node_id=target.id,
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
    state = _parse_candidate_state(candidate)
    if state is None:
        return candidate
    llm_node_count = sum(1 for node in state.nodes if node_has_capability(node, PluginCapability.LLM))
    if llm_node_count == 0:
        # Coverage findings only exist for LLM nodes; skip the catalog sweep.
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
    changed = False
    # Each successful splice permanently covers at least one finding, so the
    # fixpoint needs at most two insertions (input + output) per LLM node.
    budget = 2 * llm_node_count
    for _ in range(budget):
        working_candidate = {**candidate, "nodes": working_nodes}
        state = _parse_candidate_state(working_candidate)
        if state is None:
            # A splice produced an unparseable candidate — impossible by
            # construction, but fail safe: keep what was authored so far.
            break
        reserved = _reserved_names(state)
        nodes_by_id = {node.id: node for node in state.nodes}
        progressed = False
        for capability, (plugin_name, alias) in selections.items():
            for finding in control_coverage_findings(state, capability):
                if finding.reason not in _AUTO_WIRE_ACTIONABLE_REASONS:
                    continue
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
    result: dict[str, object] = dict(candidate)
    result["nodes"] = working_nodes
    return result


__all__ = ["wire_required_controls"]
