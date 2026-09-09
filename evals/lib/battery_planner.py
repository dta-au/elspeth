"""§7 planner probe (paired, calibration only) and tripwire (standing, every round) — the OFFLINE half.

Both reuse the parity fixtures verbatim; scoring is over captured run directories only. Nothing here
enters a pooled rate. The live wrapper (``evals/composer-battery/planner_probe.py``) fires the arms
through the Battery and calls into this module; all logic lives here.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from elspeth.contracts.composer_planner_audit import ComposerPlannerCode as PC
from elspeth.contracts.composer_planner_audit import ComposerPlannerInformationClass as IC
from elspeth.web.composer import no_tool_policy
from elspeth.web.composer.no_tool_policy import PipelineMutationIntentDecision, classify_pipeline_mutation_intent
from elspeth.web.composer.protocol import (
    PIPELINE_STAGED_AUTO_COMMIT_MESSAGE,
    PIPELINE_STAGED_REVIEW_FINDINGS_MESSAGE,
    PIPELINE_STAGED_REVIEW_MESSAGE,
    PIPELINE_STAGED_REVIEW_PENDING_INTERPRETATION_MESSAGE,
    PIPELINE_STAGED_REVIEW_PREFLIGHT_NOT_RUN_MESSAGE,
)
from elspeth.web.composer.tools.discovery import is_mutation_tool
from evals.lib.battery_capture import (
    CaptureError,
    assistant_turns,
    llm_calls,
    load_capture,
    parse_instrument,
    planner_attempts,
    tool_outcomes,
)
from evals.lib.battery_score import INSTRUMENT_KINDS, captured_is_valid, instrument_exclusion, score_path, surface_of
from evals.lib.battery_topology import topologies_match, topology_from_pipeline

REPO = Path(__file__).resolve().parents[2]
PARITY_DIR = REPO / "evals" / "composer-parity" / "fixtures"
TRIPWIRE_FIXTURES: tuple[str, ...] = ("fork_coalesce", "error_routing", "linear_transform")
PROBE_FIXTURES: tuple[str, ...] = tuple(sorted(p.stem for p in PARITY_DIR.glob("*.json")))
STAGED_VARIANTS: dict[str, str] = {
    PIPELINE_STAGED_AUTO_COMMIT_MESSAGE: "PIPELINE_STAGED_AUTO_COMMIT_MESSAGE",
    PIPELINE_STAGED_REVIEW_MESSAGE: "PIPELINE_STAGED_REVIEW_MESSAGE",
    PIPELINE_STAGED_REVIEW_FINDINGS_MESSAGE: "PIPELINE_STAGED_REVIEW_FINDINGS_MESSAGE",
    PIPELINE_STAGED_REVIEW_PREFLIGHT_NOT_RUN_MESSAGE: "PIPELINE_STAGED_REVIEW_PREFLIGHT_NOT_RUN_MESSAGE",
    PIPELINE_STAGED_REVIEW_PENDING_INTERPRETATION_MESSAGE: "PIPELINE_STAGED_REVIEW_PENDING_INTERPRETATION_MESSAGE",
}
# Loop-side discovery tool → the information class it yields (values are LIVE enum members, never literals).
LOOP_TOOL_TO_INFO: dict[str, str] = {
    "get_plugin_schema": IC.PLUGIN_SCHEMA.value,
    "list_models": IC.MODEL_CATALOG.value,
    "get_plugin_assistance": IC.PLUGIN_ASSISTANCE.value,
    "get_expression_grammar": IC.EXPRESSION_GRAMMAR.value,
    "list_sources": IC.CATALOG_SELECTION.value,
    "list_transforms": IC.CATALOG_SELECTION.value,
    "list_sinks": IC.CATALOG_SELECTION.value,
    "get_pipeline_state": IC.PIPELINE_CURRENT.value,
    "get_audit_info": IC.AUDIT_INFO.value,
}
# Planner-code triage (spec §7), keyed by live ComposerPlannerCode values; ``repeated_fingerprint`` is an attempt flag, not a code.
TRIAGE_BY_CODE: dict[str, str] = {
    PC.DISCOVERY_NO_GAIN.value: "kit_misled_discovery",
    PC.DISCOVERY_CYCLE.value: "kit_misled_discovery",
    PC.REPAIR_BLIND_REPEAT.value: "error_message_not_actionable",
    PC.COMPOSITION_EXHAUSTED.value: "budget",
    PC.DISCOVERY_EXHAUSTED.value: "budget",
    PC.PROVIDER_CALLS_EXHAUSTED.value: "budget",
    PC.REPAIR_EXHAUSTED.value: "budget",
    PC.REQUEST_BYTES_EXHAUSTED.value: "budget",
    PC.TOOL_CALLS_EXHAUSTED.value: "budget",
    PC.MALFORMED_RESPONSE.value: "model",
    PC.PROSE_REPLY.value: "model",
    PC.RESPONSE_TRUNCATED.value: "model",
}
LOOP_PREFIX = "Hi. "


class ProbeUnpaired(RuntimeError):
    pass


def load_fixture(name: str) -> dict[str, Any]:
    doc = json.loads((PARITY_DIR / f"{name}.json").read_text())
    return {"intent": doc["intent"], "canonical_arguments": doc["canonical_arguments"]}


def classifier_fingerprint() -> str:
    return hashlib.sha256(Path(inspect.getsourcefile(no_tool_policy) or "").read_bytes()).hexdigest()


def assert_pair_routes(intent: str) -> None:
    p = classify_pipeline_mutation_intent(intent)
    l_ = classify_pipeline_mutation_intent(LOOP_PREFIX + intent)
    if p is not PipelineMutationIntentDecision.EXPLICIT_MUTATION or l_ is PipelineMutationIntentDecision.EXPLICIT_MUTATION:
        raise ProbeUnpaired(f"pair does not route P→planner / L→loop: P={p.name} L={l_.name} for {intent[:60]!r}")


def required_information(args: dict[str, Any]) -> frozenset[str]:
    plugins = [str((args.get("source") or {}).get("plugin") or "")] + [str(n.get("plugin") or "") for n in args.get("nodes") or []]
    for src in (args.get("sources") or {}).values() if isinstance(args.get("sources"), dict) else []:
        plugins.append(str(src.get("plugin") or ""))
    need = {IC.PLUGIN_SCHEMA.value}
    if any(p.startswith("llm") or "_llm" in p for p in plugins):
        need.add(IC.MODEL_CATALOG.value)
    return frozenset(need)


def triage_code(code: str) -> str:
    if code == "repeated_fingerprint":
        return "error_message_not_actionable"
    return TRIAGE_BY_CODE.get(code, "other")


@dataclass
class ArmResult:
    fixture: str
    arm: str
    surface: str
    surface_ok: bool
    information_seen: list[str]
    floor_missing: list[str]
    accepted_terminal: bool
    planner_calls: int
    planner_codes: dict[str, int]
    triage: dict[str, int]
    deviations: list[str]
    staged_variant: str | None
    staged_topology_ok: bool | None
    clean: bool
    tool_bearing_calls: int
    reason: str | None
    excluded: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def staged_topology(run_dir: Path, args: dict[str, Any]) -> tuple[bool | None, str | None]:
    expected = topology_from_pipeline(args)
    state_p = run_dir / "state.json"
    if state_p.exists():
        state = json.loads(state_p.read_text())
        if isinstance(state, dict) and (state.get("sources") or state.get("source") or state.get("nodes")):
            m = topologies_match(expected, topology_from_pipeline(state))
            return m.ok, m.reason
    prop_p = run_dir / "proposals.json"
    if prop_p.exists():
        doc = json.loads(prop_p.read_text())
        for prop in (doc.get("proposals") or []) if isinstance(doc, dict) else []:
            if isinstance(prop, dict) and prop.get("status") == "pending" and isinstance(prop.get("arguments_redacted_json"), dict):
                m = topologies_match(expected, topology_from_pipeline(prop["arguments_redacted_json"]))
                return m.ok, m.reason
    return None, "no committed state and no pending proposal"


def _staged_variant(capture: Any) -> str | None:
    for turn in reversed(assistant_turns(capture)):
        for text, name in STAGED_VARIANTS.items():
            if text in (turn.content or ""):
                return name
    return None


def _exclusion_reason(kind: str | None, evidence: str | None) -> str | None:
    """Render an exclusion so the operator reads the actionable half (``post_message 524``), not just the kind."""
    if kind is None:
        return None
    return f"excluded: {kind}" + (f" ({evidence})" if evidence else "")


def score_arm(run_dir: Path, fixture: str, arm: str) -> ArmResult:
    args = load_fixture(fixture)["canonical_arguments"]
    cap = load_capture(Path(run_dir))
    surface = surface_of(cap)
    expected_surface = "planner" if arm == "P" else "compose_loop"
    surface_ok = surface == expected_surface
    reason: str | None = (
        None
        if surface_ok
        else (f"surface {surface} != expected {expected_surface}" if surface != "undetermined" else "surface undetermined")
    )
    post = next((h for h in (cap.meta.get("http") or []) if h.get("step") == "post_message"), {})
    terminal = cap.meta.get("server_terminal") or {}
    excluded, evidence = instrument_exclusion(parse_instrument(cap.meta), post.get("status"))
    need = required_information(args)
    calls = llm_calls(cap)
    tool_bearing = sum(1 for c in calls if c.status == "success" and c.tools_spec_hash)
    variant = _staged_variant(cap)
    topo_ok, topo_reason = staged_topology(Path(run_dir), args)

    if surface == "planner":
        attempts = planner_attempts(cap)
        accepted_idx = next((i for i, a in enumerate(attempts) if a.outcome == "accepted" and a.led_to == "done"), None)
        before = attempts if accepted_idx is None else attempts[: accepted_idx + 1]
        seen = sorted({info for a in before for info in a.new_information})
        codes = Counter(a.planner_code for a in attempts if a.planner_code)
        if any(a.repeated_fingerprint for a in attempts):
            codes["repeated_fingerprint"] += 1
        triage = Counter(triage_code(c) for c in codes.elements())
        # A staged pipeline the validator REJECTED is not an accepted terminal. Absent validation is not
        # evidence of invalidity, though: a review-mode staging commits no state for `validate` to run against,
        # which is why this is `is not False` and not the loop branch's `bool(...)` (there, an APPLIED mutation
        # always leaves a committed state, so a missing verdict really is a missing capture).
        accepted = accepted_idx is not None and captured_is_valid(cap) is not False
        deviations = sorted(codes)
        # Did the planner reach a verdict of its own? That is this surface's equivalent of the loop ladder's
        # "a server terminal reason outranks transport" — every attempt still `continue` means the run was cut
        # mid-loop and its outcome is unknowable; anything else means we observed it, whatever happened after.
        decided = any(a.led_to != "continue" for a in attempts)
        if excluded == "http" and decided:
            # The planner reached its verdict before the 5xx landed, so the run is observed and excluding would
            # bury a real defect (fork_coalesce: prose x3 -> MALFORMED_RESPONSE terminal, THEN a 502).
            excluded, evidence = None, None
        elif excluded is None and post.get("status") != 200 and not decided and str(terminal.get("source", "none")) == "none":
            # No response, no planner verdict, no server terminal. A client timeout deliberately does NOT set
            # `http_unrecovered` (the driver flags only transport errors there), so without this rung the run
            # falls through to the topology reason and reads as a planner finding — the same defect as `http`
            # in the shape the flag does not cover. Mirrors score_path's terminal_missing rungs.
            excluded, evidence = "terminal_missing", f"post_message status {post.get('status')} and no server terminal reason"
        return ArmResult(
            fixture,
            arm,
            surface,
            surface_ok,
            seen,
            sorted(need - set(seen)),
            accepted,
            sum(1 for c in calls if c.planner_call_ordinal is not None),
            dict(codes),
            dict(triage),
            deviations,
            variant,
            topo_ok,
            excluded is None and surface_ok and not (need - set(seen)) and accepted and not deviations and topo_ok is True,
            tool_bearing,
            _exclusion_reason(excluded, evidence) or reason or (None if topo_ok else f"topology: {topo_reason}"),
            excluded,
        )

    # compose loop (or undetermined): the scenario-free path score — no fabricated floor, no synthetic Scenario
    path = score_path(cap)
    outcomes = tool_outcomes(cap)
    seen_set: set[str] = set()
    accepted = False
    for turn in assistant_turns(cap):
        for c in turn.tool_calls:
            if is_mutation_tool(c.name) and outcomes.get(c.id) == "applied":
                accepted = True
                break
            if c.name in LOOP_TOOL_TO_INFO:
                seen_set.add(LOOP_TOOL_TO_INFO[c.name])
        if accepted:
            break
    accepted = accepted and bool(path.is_valid)
    seen = sorted(seen_set)
    deviations = [d.cls for d in path.deviations]
    # An INSTRUMENT exclusion leads: "surface undetermined" for a substrate that died mid-run names the symptom
    # and hides the fault. A MEASUREMENT kind stays behind `reason`, which already describes it more precisely.
    excl_reason = _exclusion_reason(path.excluded, path.exclusion_evidence)
    return ArmResult(
        fixture,
        arm,
        surface,
        surface_ok,
        seen,
        sorted(need - seen_set),
        accepted,
        0,
        {},
        {},
        deviations,
        variant,
        topo_ok,
        surface_ok and not (need - seen_set) and accepted and not deviations and topo_ok is True and path.excluded is None,
        tool_bearing,
        (excl_reason if path.excluded_by_instrument else None)
        or reason
        or excl_reason
        or (None if topo_ok else f"topology: {topo_reason}"),
        path.excluded,
    )


def score_tripwire_dir(round_dir: Path) -> list[dict[str, Any]]:
    tw = Path(round_dir) / "_tripwire"
    table: list[dict[str, Any]] = []
    for fixture in TRIPWIRE_FIXTURES:
        run_dir = tw / fixture / "1"
        if not run_dir.exists():
            table.append(
                {
                    "fixture": fixture,
                    "pass": False,
                    "staged_variant": None,
                    "planner_calls": 0,
                    "planner_codes": {},
                    "surface": "undetermined",
                    "reason": "not fired",
                    "excluded": "capture",
                }
            )
            continue
        try:
            r = score_arm(run_dir, fixture, "P")
        except CaptureError as exc:
            # a half-written tripwire capture is a failed tripwire, not a traceback out of the round
            table.append(
                {
                    "fixture": fixture,
                    "pass": False,
                    "staged_variant": None,
                    "planner_calls": 0,
                    "planner_codes": {},
                    "surface": "undetermined",
                    "reason": f"capture: {exc}",
                    "excluded": "capture",
                }
            )
            continue
        # An excluded arm never passes: the harness did not read the run, so it demonstrates nothing either way.
        passed = r.excluded is None and r.surface == "planner" and r.staged_variant is not None and r.staged_topology_ok is True
        reason = None if passed else (r.reason or ("no PIPELINE_STAGED_* message" if r.staged_variant is None else "topology mismatch"))
        table.append(
            {
                "fixture": fixture,
                "pass": passed,
                "staged_variant": r.staged_variant,
                "planner_calls": r.planner_calls,
                "planner_codes": r.planner_codes,
                "surface": r.surface,
                "reason": reason,
                "excluded": r.excluded,
            }
        )
    tw.mkdir(parents=True, exist_ok=True)
    (tw / "tripwire.json").write_text(json.dumps(table, indent=2))
    return table


def score_probe_dir(round_dir: Path) -> dict[str, Any]:
    pd = Path(round_dir) / "_probe"
    arms: list[dict[str, Any]] = []
    for fixture in PROBE_FIXTURES:
        for arm in ("P", "L"):
            run_dir = pd / fixture / arm
            if not run_dir.exists():
                continue
            try:
                arms.append(score_arm(run_dir, fixture, arm).to_dict())
            except CaptureError as exc:
                # one unreadable arm never costs the scoring of the arms that did fire
                arms.append(
                    ArmResult(
                        fixture,
                        arm,
                        "undetermined",
                        False,
                        [],
                        [],
                        False,
                        0,
                        {},
                        {},
                        [],
                        None,
                        None,
                        False,
                        0,
                        f"capture: {exc}",
                        "capture",
                    ).to_dict()
                )
    # The probe's whole claim is the PAIRED comparison. An arm excluded by the instrument was not read, so its
    # fixture has no pair to compare — say so, rather than tabulating half a comparison as if it were one.
    unpaired = sorted(
        {a["fixture"] for a in arms if a["excluded"] in INSTRUMENT_KINDS or sum(1 for b in arms if b["fixture"] == a["fixture"]) < 2}
    )
    doc = {
        "classifier_fingerprint": classifier_fingerprint(),
        "rule": "floor = required information classes seen before the accepting mutation + one accepted terminal; surface asserted per arm from artifacts",
        "unpaired": unpaired,
        "arms": arms,
    }
    pd.mkdir(parents=True, exist_ok=True)
    (pd / "probe.json").write_text(json.dumps(doc, indent=2))
    lines = [
        "# Planner probe (calibration; enters no rate)",
        "",
        f"classifier fingerprint `{doc['classifier_fingerprint']}`",
        "",
        (
            f"**NOT COMPARABLE — {len(unpaired)} fixture(s) have no usable pair: {', '.join(unpaired)}.** "
            "An instrument-excluded or missing arm was never read; do not read a paired conclusion off those rows."
            if unpaired
            else "All fixtures paired."
        ),
        "",
        "| fixture | arm | excluded | surface_ok | clean | floor_missing | accepted | tool_bearing | planner_calls | triage | staged | topology_ok | reason |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    lines += [
        f"| {a['fixture']} | {a['arm']} | {a['excluded'] or '-'} | {a['surface_ok']} | {a['clean']} | {','.join(a['floor_missing']) or '-'} "
        f"| {a['accepted_terminal']} | {a['tool_bearing_calls']} | {a['planner_calls']} | {json.dumps(a['triage'])} "
        f"| {a['staged_variant']} | {a['staged_topology_ok']} | {a['reason'] or ''} |"
        for a in arms
    ]
    (pd / "probe.md").write_text("\n".join(lines) + "\n")
    return doc


__all__ = [
    "LOOP_PREFIX",
    "LOOP_TOOL_TO_INFO",
    "PARITY_DIR",
    "PROBE_FIXTURES",
    "STAGED_VARIANTS",
    "TRIAGE_BY_CODE",
    "TRIPWIRE_FIXTURES",
    "ArmResult",
    "ProbeUnpaired",
    "assert_pair_routes",
    "classifier_fingerprint",
    "load_fixture",
    "required_information",
    "score_arm",
    "score_probe_dir",
    "score_tripwire_dir",
    "staged_topology",
    "triage_code",
]
