"""Per-run scorer for the composer battery (spec §3, §5), in two halves.

``score_path(capture) -> PathScore`` is **scenario-free**: buckets, surface,
deviation events, exclusions, tokens, wall clock — everything that is a fact
about the run alone. The live driver reads only ``PathScore.excluded`` (for
its abort rules) and the planner probe reads only ``PathScore``; neither
needs a scenario, a floor, or the topology comparator.

``judge(scenario, path) -> Score`` binds a run to its pre-registered floor
and oracle: excess, ``below_floor``, ``wrong_shape``, green/red,
``unattributed_excess``, clean/optimal. ``score_run`` composes the two.

Every excess above the floor lands in exactly one deviation class or in
``unattributed_excess`` — silence is not an option. Exclusions
(``instrument_error`` sub-kinds) remove the run from every rate and are
reported beside it; they are partitioned into INSTRUMENT_KINDS (the harness
failed — abort/flag material) and MEASUREMENT_KINDS (the product did
something the loop-only instrument cannot score — reported, never an abort).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.web.composer.tools.discovery import is_discovery_tool, is_mutation_tool
from evals.lib.battery_capture import (
    Capture,
    CaptureError,
    ToolCall,
    assistant_turns,
    llm_calls,
    load_capture,
    parse_instrument,
    planner_attempts,
    tool_outcomes,
    tool_rows,
)
from evals.lib.battery_scenario import Scenario, topology_from_dict
from evals.lib.battery_topology import observed_option_values, topologies_match, topology_from_pipeline
from evals.lib.scenario_from_example import BUILD_FAILURE_SENTINELS, PASSIVITY_PHRASES

INSTRUMENT_KINDS: tuple[str, ...] = ("capture", "truncated", "read_integrity", "auth", "http", "transport", "terminal_missing")
MEASUREMENT_KINDS: tuple[str, ...] = ("surface", "no_calls")  # the product routed elsewhere / never called a tool: a finding, not a fault
EXCLUSION_KINDS: tuple[str, ...] = (
    "no_calls",
    "auth",
    "http",
    "read_integrity",
    "truncated",
    "surface",
    "terminal_missing",
    "transport",
    "capture",
)
assert set(EXCLUSION_KINDS) == set(INSTRUMENT_KINDS) | set(MEASUREMENT_KINDS)

SEVERITY: dict[str, str] = {
    "excess_discovery": "soft",
    "schema_fumble": "soft",
    "data_setup_detour": "soft",
    "data_rework": "soft",
    "retried_provider_error": "soft",
    "repair": "hard",
    "backtrack": "hard",
    "malformed_output": "hard",
    "wrong_shape": "hard",
    "decline": "hard",
    "passivity": "hard",
    "turn_exhaustion": "hard",
    "wall_timeout": "hard",
    "approval_pending": "hard",
    "abandoned_mutation": "hard",
    "unattributed_excess": "unattributed",  # neither soft nor hard: a taxonomy gap, reported in its own headline line
}
PATH_CLASSES: frozenset[str] = frozenset(SEVERITY) - {"wrong_shape", "unattributed_excess"}  # classes score_path may emit

# Provider-transport failures. ``cancelled`` is deliberately NOT here: the coordinator cancels the in-flight
# call on wall-timeout / turn-exhaustion shutdown, so a CANCELLED row is the natural LAST row of exactly the
# runs the terminal classes must keep in the denominator.
_TRANSPORT_STATUSES = frozenset(
    {ComposerLLMCallStatus.API_ERROR.value, ComposerLLMCallStatus.TIMEOUT.value, ComposerLLMCallStatus.AUTH_ERROR.value}
)
_MALFORMED_STATUSES = frozenset({ComposerLLMCallStatus.MALFORMED_RESPONSE.value, ComposerLLMCallStatus.BAD_REQUEST_ERROR.value})
_SUCCESS = ComposerLLMCallStatus.SUCCESS.value
_NOT_APPLIED = frozenset({"rejected", "failed", "cancelled"})
_REMOVAL_TOOLS = frozenset({"remove_node", "remove_edge", "remove_output", "clear_source"})
_PATCH_TOOLS = frozenset({"patch_source_options", "patch_node_options", "patch_output_options"})
_TERMINAL_CLASSES = frozenset({"turn_exhaustion", "wall_timeout"})


@dataclass(frozen=True)
class Deviation:
    cls: str
    sequence_no: tuple[int, int]
    tool: str | None = None
    args_digest: str | None = None
    codes: tuple[str, ...] = ()
    audit_ordinal: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "class": self.cls,
            "severity": SEVERITY[self.cls],
            "sequence_no": list(self.sequence_no),
            "tool": self.tool,
            "args_digest": self.args_digest,
            "codes": list(self.codes),
            "audit_ordinal": self.audit_ordinal,
        }


@dataclass
class PathScore:
    """Scenario-free facts about one run."""

    repeat: int
    surface_observed: str
    tool_bearing_calls: int
    advisor_calls: int
    other_text_calls: int
    retried_calls: int
    audited_provider_calls: int
    deviations: list[Deviation]
    review_rounds: int
    recipe_used: bool
    is_valid: bool | None
    state: dict[str, Any] | None
    state_empty: bool
    excluded: str | None
    exclusion_evidence: str | None
    tokens: dict[str, int]
    cost: float | None
    wall_ms: int
    applied_any: bool
    attempted_any: bool
    schema_read_before_first_mutation: bool | None  # None when no mutation was applied
    passivity_hits: tuple[str, ...]
    sentinel_hits: tuple[str, ...]

    @property
    def excluded_by_instrument(self) -> bool:
        return self.excluded in INSTRUMENT_KINDS


@dataclass
class Score:
    case: str
    repeat: int
    surface_observed: str
    tool_bearing_calls: int
    advisor_calls: int
    other_text_calls: int
    retried_calls: int
    audited_provider_calls: int
    floor: int
    excess: int
    deviations: list[Deviation]
    review_rounds: int
    recipe_used: bool
    green: bool
    red: bool
    is_valid: bool | None
    wrong_shape: bool
    clean: bool
    optimal: bool
    excluded: str | None
    tokens: dict[str, int]
    cost: float | None
    wall_ms: int
    red_reasons: list[str] = field(default_factory=list)
    green_reasons: list[str] = field(default_factory=list)
    exclusion_evidence: str | None = None
    below_floor: bool = False  # tool-bearing calls < floor: the floor may be too high; never "optimal" hides it
    scenario_sha256: str | None = None  # the scenario file that produced this score (late-binding guard, report checks it)

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "deviations"}
        d["deviations"] = [x.to_dict() for x in self.deviations]
        return d


# ── helpers ────────────────────────────────────────────────────────────────


def _digest(args: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _discovery_key(call: ToolCall) -> tuple[Any, ...]:
    a = call.arguments
    if call.name == "get_plugin_schema":
        return ("schema", a.get("plugin_type"), a.get("plugin_name"))
    if call.name in {
        "get_pipeline_state",
        "get_audit_info",
        "get_expression_grammar",
        "list_recipes",
        "list_models",
        "list_sources",
        "list_transforms",
        "list_sinks",
    }:
        return ("tool", call.name)
    return ("tool", call.name, _digest(a))


def _patch_target(call: ToolCall) -> str:
    a = call.arguments
    return str(a.get("node_id") or a.get("sink_name") or a.get("output_name") or "source")


def _codes_from(content: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not content:
        return ()
    codes: list[str] = []
    for key in ("error_class", "code", "status"):
        v = content.get(key)
        if isinstance(v, str) and v:
            codes.append(v)
    for entry in content.get("errors") or []:
        if isinstance(entry, Mapping) and isinstance(entry.get("code"), str):
            codes.append(entry["code"])
    return tuple(dict.fromkeys(codes))


def _scenario_sha(scenario: Scenario) -> str | None:
    try:
        return hashlib.sha256(Path(scenario.path).read_bytes()).hexdigest()
    except OSError:
        return None


@dataclass(frozen=True)
class _Event:
    turn_seq: int
    row_seq: int
    call: ToolCall
    outcome: str
    content: Mapping[str, Any] | None
    audit_ordinal: int


def _events(capture: Capture, tool_bearing_seqs: list[int]) -> list[_Event]:
    outcomes = tool_outcomes(capture)
    rows_by_id = {r.tool_call_id: r for r in tool_rows(capture)}
    events: list[_Event] = []
    for turn in assistant_turns(capture):
        ordinal = sum(
            1 for s in tool_bearing_seqs if s <= turn.sequence_no
        )  # 1-based index of the last tool-bearing audit row at/before this turn
        for c in turn.tool_calls:
            row = rows_by_id.get(c.id)
            events.append(
                _Event(
                    turn.sequence_no,
                    row.sequence_no if row else turn.sequence_no,
                    c,
                    outcomes.get(c.id, "cancelled"),
                    row.content if row else None,
                    ordinal,
                )
            )
    return events


def surface_of(capture: Capture) -> str:
    calls = llm_calls(capture)
    if not calls:
        return "undetermined"
    if any(c.planner_call_ordinal is not None for c in calls) or planner_attempts(capture):
        return "planner"
    return "compose_loop"


# ── half 1: scenario-free path scoring ─────────────────────────────────────


def score_path(capture: Capture) -> PathScore:  # one linear pass, sectioned below
    meta = capture.meta
    binding = (meta.get("identity") or {}).get("binding") or {}
    advisor_model = binding.get("advisor_model")
    instrument = parse_instrument(meta)  # strict: raises CaptureError on a drifted contract
    http_steps = meta.get("http") or []
    terminal = meta.get("server_terminal") or {}
    repeat = int(meta.get("repeat") or 0)

    calls = llm_calls(capture)
    tool_bearing = [c for c in calls if c.status == _SUCCESS and c.tools_spec_hash]
    advisor = [c for c in calls if c.status == _SUCCESS and not c.tools_spec_hash and c.model_requested == advisor_model]
    other_text = [c for c in calls if c.status == _SUCCESS and not c.tools_spec_hash and c.model_requested != advisor_model]
    success_seqs = [c.sequence_no for c in calls if c.status == _SUCCESS]
    transport = [c for c in calls if c.status in _TRANSPORT_STATUSES]
    retried = [c for c in transport if any(s > c.sequence_no for s in success_seqs)]
    unrecovered = [c for c in transport if c not in retried]
    surface = surface_of(capture)

    tokens = {"prompt": 0, "completion": 0, "cached_prompt": 0, "unknown_calls": 0}
    cost_known, cost = False, 0.0
    for c in calls:
        if c.prompt_tokens is None or c.completion_tokens is None:
            tokens["unknown_calls"] += 1
        else:
            tokens["prompt"] += c.prompt_tokens
            tokens["completion"] += c.completion_tokens
            tokens["cached_prompt"] += c.cached_prompt_tokens or 0
        if c.provider_cost is not None:
            cost_known, cost = True, cost + c.provider_cost
    starts = [t for t in (_parse_ts(c.started_at) for c in calls) if t]
    ends = [t for t in (_parse_ts(c.finished_at) for c in calls) if t]
    wall_ms = int((max(ends) - min(starts)).total_seconds() * 1000) if starts and ends else 0

    is_valid: bool | None = None
    if isinstance(capture.validate, Mapping) and "is_valid" in capture.validate:
        is_valid = bool(capture.validate["is_valid"])
    state = capture.state if isinstance(capture.state, Mapping) else None
    state_empty = state is None or (
        not (state.get("sources") or state.get("source")) and not state.get("nodes") and not state.get("outputs")
    )

    # ── deviations from the tool-call timeline ──
    events = _events(capture, [c.sequence_no for c in tool_bearing])
    deviations: list[Deviation] = []
    seen_discovery: set[tuple[Any, ...]] = set()
    patched: set[tuple[str, str]] = set()
    pending_failed: _Event | None = None
    applied_any = applied_set_pipeline = blob_created = recipe_used = False
    for ev in events:
        name, args = ev.call.name, ev.call.arguments
        digest = _digest(args)
        if name == "apply_pipeline_recipe":
            recipe_used = True
        if is_discovery_tool(name):
            key = _discovery_key(ev.call)
            if key in seen_discovery:
                deviations.append(Deviation("excess_discovery", (ev.turn_seq, ev.row_seq), name, digest, (), ev.audit_ordinal))
            seen_discovery.add(key)
            continue
        # blob tools ARE mutation tools in the registry; they are classified here, ahead of the generic
        # mutation path, so a data detour never reads as a repair/backtrack — keep this ordering.
        if name == "create_blob":
            deviations.append(
                Deviation(
                    "data_rework" if blob_created else "data_setup_detour", (ev.turn_seq, ev.row_seq), name, digest, (), ev.audit_ordinal
                )
            )
            blob_created = True
            continue
        if name in {"update_blob", "delete_blob"}:
            deviations.append(Deviation("data_rework", (ev.turn_seq, ev.row_seq), name, digest, (), ev.audit_ordinal))
            continue
        if not is_mutation_tool(name):
            continue
        # a mutation
        if pending_failed is not None:
            deviations.append(
                Deviation(
                    "repair",
                    (pending_failed.row_seq, ev.row_seq),
                    pending_failed.call.name,
                    _digest(pending_failed.call.arguments),
                    _codes_from(pending_failed.content),
                    pending_failed.audit_ordinal,
                )
            )
            pending_failed = None
        if ev.outcome in _NOT_APPLIED:
            pending_failed = ev
            continue
        if isinstance(ev.content, Mapping) and ev.content.get("success") is True and ev.content.get("status") == "APPROVAL_REQUIRED":
            deviations.append(
                Deviation("approval_pending", (ev.turn_seq, ev.row_seq), name, digest, ("APPROVAL_REQUIRED",), ev.audit_ordinal)
            )
            continue
        # applied
        if (name in _REMOVAL_TOOLS and applied_any) or (name == "set_pipeline" and applied_set_pipeline):
            deviations.append(Deviation("backtrack", (_first_applied_seq(events), ev.row_seq), name, digest, (), ev.audit_ordinal))
        if name in _PATCH_TOOLS:
            tkey = (name, _patch_target(ev.call))
            if tkey in patched:
                deviations.append(Deviation("schema_fumble", (ev.turn_seq, ev.row_seq), name, digest, (), ev.audit_ordinal))
            patched.add(tkey)
        applied_any = True
        applied_set_pipeline = applied_set_pipeline or name in {"set_pipeline", "apply_pipeline_recipe"}
        seen_discovery.clear()  # a mutation licenses a fresh read
    attempted_any = any(is_mutation_tool(e.call.name) for e in events)
    if pending_failed is not None:
        # a rejected/failed mutation that was never retried: keep its codes — the most useful triage datum
        deviations.append(
            Deviation(
                "abandoned_mutation",
                (pending_failed.turn_seq, pending_failed.row_seq),
                pending_failed.call.name,
                _digest(pending_failed.call.arguments),
                _codes_from(pending_failed.content),
                pending_failed.audit_ordinal,
            )
        )
    first_mut = next((e for e in events if is_mutation_tool(e.call.name) and e.outcome not in _NOT_APPLIED), None)
    schema_before: bool | None = (
        None if first_mut is None else any(e.call.name == "get_plugin_schema" and e.row_seq < first_mut.row_seq for e in events)
    )

    # ── audit-row classes ──
    for c in retried:
        deviations.append(
            Deviation("retried_provider_error", (c.sequence_no, c.sequence_no), None, None, (c.status, c.error_class or ""), None)
        )
    for c in calls:
        if c.status in _MALFORMED_STATUSES:
            deviations.append(Deviation("malformed_output", (c.sequence_no, c.sequence_no), None, None, (c.status,), None))

    # ── terminal classes (from the captured body only) ──
    post = next((h for h in http_steps if h.get("step") == "post_message"), {})
    post_status = post.get("status")
    budget = terminal.get("budget_exhausted")
    if not is_valid:
        if budget in {"composition", "discovery"}:
            deviations.append(Deviation("turn_exhaustion", (0, 0), None, None, (str(budget), str(terminal.get("reason"))), None))
        elif budget == "timeout":
            deviations.append(Deviation("wall_timeout", (0, 0), None, None, (str(terminal.get("reason")),), None))

    # ── final-message signals (RGR lists) ──
    final_text = next((t.content for t in reversed(assistant_turns(capture)) if t.content), "").lower()
    phrase_hits = tuple(p for p in PASSIVITY_PHRASES if p in final_text)
    sentinel_hits = tuple(p for p in BUILD_FAILURE_SENTINELS if p in final_text)
    if not applied_any and not attempted_any and not any(d.cls in _TERMINAL_CLASSES for d in deviations):
        # never attempted a mutation: passivity (asked permission) or decline (answered in prose)
        deviations.append(Deviation("passivity" if phrase_hits else "decline", (0, 0), None, None, phrase_hits, None))
    deviations.sort(key=lambda d: (d.sequence_no, d.cls))

    # ── exclusions (precedence order; instrument kinds first, then measurement kinds) ──
    excluded: str | None = None
    evidence: str | None = None
    if instrument.truncated:
        excluded, evidence = "truncated", "last messages page was full"
    elif instrument.read_integrity:
        excluded, evidence = "read_integrity", instrument.read_integrity
    elif instrument.auth_failed or post_status in {401, 403}:
        excluded, evidence = "auth", f"post_message status {post_status}"
    elif instrument.http_unrecovered:
        excluded, evidence = "http", instrument.http_unrecovered
    elif instrument.review_rounds_exhausted:
        excluded, evidence = "http", "interpretation review rounds exhausted (5)"
    elif surface != "compose_loop":
        excluded, evidence = "surface", f"surface_observed={surface}"
    elif unrecovered and budget is None:
        # a server terminal reason (turn/wall budget) outranks transport: those runs stay in the denominator as hard
        excluded, evidence = (
            "transport",
            f"{unrecovered[0].status} at sequence_no {unrecovered[0].sequence_no} with no later successful call",
        )
    elif not tool_bearing:
        excluded, evidence = "no_calls", "zero tool-bearing calls"
    elif not is_valid and post_status != 200 and terminal.get("source", "none") == "none":
        excluded, evidence = "terminal_missing", f"post_message status {post_status} and no server terminal reason"

    return PathScore(
        repeat=repeat,
        surface_observed=surface,
        tool_bearing_calls=len(tool_bearing),
        advisor_calls=len(advisor),
        other_text_calls=len(other_text),
        retried_calls=len(retried),
        audited_provider_calls=len(calls),
        deviations=deviations,
        review_rounds=len(capture.reviews),
        recipe_used=recipe_used,
        is_valid=is_valid,
        state=dict(state) if state is not None else None,
        state_empty=state_empty,
        excluded=excluded,
        exclusion_evidence=evidence,
        tokens=tokens,
        cost=(cost if cost_known else None),
        wall_ms=wall_ms,
        applied_any=applied_any,
        attempted_any=attempted_any,
        schema_read_before_first_mutation=schema_before,
        passivity_hits=phrase_hits,
        sentinel_hits=sentinel_hits,
    )


def _first_applied_seq(events: list[_Event]) -> int:
    for e in events:
        if is_mutation_tool(e.call.name) and e.outcome not in _NOT_APPLIED:
            return e.row_seq
    return 0


def _excluded_path(kind: str, evidence: str, *, repeat: int) -> PathScore:
    return PathScore(
        repeat=repeat,
        surface_observed="undetermined",
        tool_bearing_calls=0,
        advisor_calls=0,
        other_text_calls=0,
        retried_calls=0,
        audited_provider_calls=0,
        deviations=[],
        review_rounds=0,
        recipe_used=False,
        is_valid=None,
        state=None,
        state_empty=True,
        excluded=kind,
        exclusion_evidence=evidence,
        tokens={"prompt": 0, "completion": 0, "cached_prompt": 0, "unknown_calls": 0},
        cost=None,
        wall_ms=0,
        applied_any=False,
        attempted_any=False,
        schema_read_before_first_mutation=None,
        passivity_hits=(),
        sentinel_hits=(),
    )


def _repeat_from_dir(run_dir: Path) -> int:
    try:
        return int(run_dir.name)
    except ValueError:
        return 0


def path_from_disk(run_dir: Path) -> PathScore:
    """Scenario-free scoring of a captured run; a CaptureError (missing/unparseable artifact, drifted meta contract) becomes ``excluded="capture"``. Never raises."""
    try:
        return score_path(load_capture(Path(run_dir)))
    except CaptureError as exc:
        return _excluded_path("capture", str(exc), repeat=_repeat_from_dir(Path(run_dir)))


# ── half 2: judgement against the pre-registered scenario ──────────────────


def judge(scenario: Scenario, path: PathScore) -> Score:
    floor = scenario.floor.tool_bearing_calls
    excess = max(0, path.tool_bearing_calls - floor)
    below_floor = 0 < path.tool_bearing_calls < floor
    deviations = list(path.deviations)

    wrong_shape = False
    shape_reason: str | None = None
    if path.is_valid and path.state is not None:
        match = topologies_match(
            topology_from_dict(scenario.expected_topology),
            topology_from_pipeline(path.state),
            option_values=observed_option_values(path.state),
            option_assertions=[tuple(a) for a in scenario.option_assertions],
        )
        wrong_shape, shape_reason = (not match.ok), match.reason
    if wrong_shape:
        deviations.append(Deviation("wrong_shape", (0, 0), None, None, (shape_reason or "",), None))

    red_reasons: list[str] = []
    if path.passivity_hits:
        red_reasons.append(f"forbidden passivity phrases in final message: {list(path.passivity_hits)}")
    if path.sentinel_hits:
        red_reasons.append(f"build failure sentinels in final message: {list(path.sentinel_hits)}")
    if path.state_empty:
        red_reasons.append("final composition state is null or structurally empty")
    if path.is_valid is False:
        red_reasons.append("final composition state has is_valid=false")
    gc = scenario.green_criteria  # closed vocabulary, validated by load_scenario; every key defaults True
    green_reasons: list[str] = []
    if gc.get("is_valid", True) and not path.is_valid:
        green_reasons.append("not is_valid")
    if gc.get("topology_matches_expected", True) and (not path.is_valid or wrong_shape):
        green_reasons.append(f"topology: {shape_reason or 'no valid state'}")
    if gc.get("must_discover_schema_before_first_mutation", True) and path.schema_read_before_first_mutation is False:
        green_reasons.append("no get_plugin_schema before the first applied mutation")
    green = not green_reasons and not red_reasons
    red = bool(red_reasons)

    if excess > 0 and not [d for d in deviations if d.cls != "wrong_shape"]:
        deviations.append(Deviation("unattributed_excess", (0, 0), None, None, (f"excess={excess}",), None))
    deviations.sort(key=lambda d: (d.sequence_no, d.cls))

    clean = path.excluded is None and not deviations and green and bool(path.is_valid)
    optimal = clean and path.tool_bearing_calls == floor
    return Score(
        case=scenario.case,
        repeat=path.repeat,
        surface_observed=path.surface_observed,
        tool_bearing_calls=path.tool_bearing_calls,
        advisor_calls=path.advisor_calls,
        other_text_calls=path.other_text_calls,
        retried_calls=path.retried_calls,
        audited_provider_calls=path.audited_provider_calls,
        floor=floor,
        excess=excess,
        deviations=deviations,
        review_rounds=path.review_rounds,
        recipe_used=path.recipe_used,
        green=green,
        red=red,
        is_valid=path.is_valid,
        wrong_shape=wrong_shape,
        clean=clean,
        optimal=optimal,
        excluded=path.excluded,
        tokens=path.tokens,
        cost=path.cost,
        wall_ms=path.wall_ms,
        red_reasons=red_reasons,
        green_reasons=green_reasons,
        exclusion_evidence=path.exclusion_evidence,
        below_floor=below_floor,
        scenario_sha256=_scenario_sha(scenario),
    )


def score_run(capture: Capture, scenario: Scenario) -> Score:
    return judge(scenario, score_path(capture))


def score_from_disk(run_dir: Path, scenario: Scenario) -> Score:
    """``load_capture`` + ``score_run``; a CaptureError becomes ``excluded="capture"``. Never raises."""
    return judge(scenario, path_from_disk(Path(run_dir)))


def write_score(run_dir: Path, score: Score) -> Path:
    p = Path(run_dir) / "score.json"
    p.write_text(json.dumps(score.to_dict(), indent=2, sort_keys=True) + "\n")
    return p


__all__ = [
    "EXCLUSION_KINDS",
    "INSTRUMENT_KINDS",
    "MEASUREMENT_KINDS",
    "PATH_CLASSES",
    "SEVERITY",
    "Deviation",
    "PathScore",
    "Score",
    "judge",
    "path_from_disk",
    "score_from_disk",
    "score_path",
    "score_run",
    "surface_of",
    "write_score",
]
