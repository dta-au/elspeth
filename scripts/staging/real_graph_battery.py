"""Drive provider-backed repair and edit turns against a copied real graph, through ordinary chat.

The battery works on a session the operator has already copied (graph and
files) into a test account. It runs two phases, each a sequence of normal
``POST /api/sessions/{id}/messages`` turns answered by the configured provider:

* ``repair`` -- one turn asking the Composer to revalidate the graph and,
  if needed, correct only schema declarations and field guarantees;
* ``edits`` -- ``--cycles`` cycles of ten structural turns that add, reorder
  and remove temporary passthrough transforms at the head of the graph and
  finally restore the original wiring.

After each turn it checks the graph: only the expected passthroughs may have
changed, the existing nodes must be unchanged (after the repair phase: except
schema declarations and required input fields), the graph must be valid, and
at least one provider call must have succeeded for that operation.

Safety properties (named by tests in
``tests/unit/scripts/staging/test_real_graph_battery.py``):

* Every turn is an ordinary chat message; the battery never writes state.
* Each turn's operation id is saved BEFORE it is submitted; a resumed run polls
  that operation instead of submitting again, so an interrupted client cannot
  duplicate a turn.
* A run resumes only from a checkpoint that is complete and valid for the
  same session and attempt. The edits phase starts only when the repair phase
  is complete and the live graph equals its final state.
* A turn that fails verification stops the phase and records the reason; the
  same attempt never continues past it. Start a new ``--attempt`` after
  diagnosis.
* The graph checker is proved on its own negative controls before the edits
  phase starts.

Reading provider evidence uses the audit-grade messages view, which records an
audit-grade access entry for each read.

Not carried from the delivered script: copying the source session's historical
graph and files (``prepare``), and the instruction suffix and evidence helpers
of an operator module that is not in the repository.
"""

from __future__ import annotations

import argparse
import copy
import getpass
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from scripts.staging.opslib import (
    AccountAuth,
    CheckpointStore,
    JsonObject,
    OperationFailed,
    UsageError,
    WebClient,
    account_from,
    add_account_arguments,
    add_execute_flag,
    clean_node,
    differences,
    emit,
    graph_content,
    list_pages,
    require_dict,
    require_uuid,
    run_tool,
    setting,
    sign_in,
    token_expiry,
)
from scripts.staging.repair_copied_prompt_reviews import checkpoint_name as review_checkpoint_name

REPAIR_INSTRUCTION = (
    "Revalidate this copied graph and resolve its saved validation errors. If the current validator passes, refresh the "
    "saved validation result without changing graph content. If repairs are required, correct only the affected schema "
    "declarations and field guarantees. Preserve every prompt, file binding, expression, output field, node and "
    "connection exactly. Do not execute the pipeline."
)
REVIEWED_REPAIR_INSTRUCTION = (
    "Revalidate this copied graph and refresh its saved validation result. Keep the graph, schemas, prompts and file "
    "bindings exactly unchanged. Its copied file reviews have been re-established and runtime validation passes. Report "
    "any disagreement from unmaterialized authoring checks as a diagnostic, without rewriting the graph to address it. "
    "Do not execute the pipeline."
)
REPAIR_EXEMPT_OPTIONS = ("schema", "required_input_fields")
LIVE_STATUSES = ("queued", "running")
LONG_PROVIDER_CALL_MS = 300_000


# ── graph checks ───────────────────────────────────────────────────────────


def check_graph(baseline: Mapping[str, Any], state: Mapping[str, Any], order: Sequence[str], *, repair: bool = False) -> list[str]:
    """Return difference paths between ``state`` and the expected graph; empty when it is as expected.

    ``order`` is the expected chain of temporary passthrough transforms between
    the first source and the original head node. Edges are a display
    projection owned by the Composer, so routing is compared through node
    ``input``/``on_success`` instead.
    """
    before = graph_content(baseline)
    after = graph_content(state)
    before.pop("edges")
    after.pop("edges")
    original = {node["id"]: node for node in before["nodes"]}
    current = {node["id"]: node for node in after["nodes"]}
    errors: list[str] = []
    if set(current) != set(original) | set(order):
        errors.append("/node_inventory")
    if repair:
        for node in [*original.values(), *current.values()]:
            for key in REPAIR_EXEMPT_OPTIONS:
                if key in node["options"]:
                    del node["options"][key]
    else:
        source_name = next(iter(before["sources"]))
        initial_route = before["sources"][source_name]["on_success"]
        heads = [node for node in original.values() if node["input"] == initial_route]
        if len(heads) != 1:
            raise OperationFailed("the baseline graph does not have exactly one node fed by its first source")
        head = heads[0]["id"]
        route = after["sources"][source_name]["on_success"]
        for name in order:
            if name not in current:
                errors.append("/missing/" + name)
                continue
            node = current[name]
            if node["plugin"] != "passthrough" or node["node_type"] != "transform" or node["input"] != route:
                errors.append("/passthrough_wiring/" + name)
            route = node["on_success"]
        if head not in current or current[head]["input"] != route:
            errors.append("/route_into_original_graph")
        if head in current:
            current[head]["input"] = original[head]["input"]
        after["sources"][source_name]["on_success"] = initial_route
    before["nodes"] = [clean_node(original[key]) for key in sorted(original)]
    after["nodes"] = [clean_node(current[key]) for key in sorted(original) if key in current]
    errors.extend(differences(before, after))
    if state["is_valid"] is not True:
        errors.append("/runtime_validation")
    return errors


def edit_instructions(baseline: Mapping[str, Any], cycles: int) -> list[tuple[str, list[str]]]:
    """Ten turns per cycle: insert four passthroughs, swap two pairs, remove all four."""
    source_name = next(iter(baseline["sources"]))
    route = baseline["sources"][source_name]["on_success"]
    heads = [node["id"] for node in baseline["nodes"] if node["input"] == route]
    if len(heads) != 1:
        raise OperationFailed("the baseline graph does not have exactly one node fed by its first source")
    head = heads[0]
    result: list[tuple[str, list[str]]] = []
    for cycle in range(cycles):
        a, b, c, d = (f"battery{cycle}_{letter}" for letter in "abcd")
        result.extend(
            [
                (f"Add a passthrough transform named {a} between {source_name} and {head}.", [a]),
                (f"Add a passthrough transform named {b} between {a} and {head}.", [a, b]),
                (f"Add a passthrough transform named {c} between {source_name} and {a}.", [c, a, b]),
                (f"Add a passthrough transform named {d} between {b} and {head}.", [c, a, b, d]),
                (f"Swap {a} and {c} so the order is {source_name}, {a}, {c}, {b}, {d}, {head}.", [a, c, b, d]),
                (f"Swap {b} and {d} so the order is {source_name}, {a}, {c}, {d}, {b}, {head}.", [a, c, d, b]),
                (f"Remove {a} and reconnect {source_name} directly to {c}.", [c, d, b]),
                (f"Remove {b} and reconnect {d} directly to {head}.", [c, d]),
                (f"Remove {c} and reconnect {source_name} directly to {d}.", [d]),
                (
                    f"Remove {d} and reconnect {source_name} directly to {head}. "
                    f"Restore {source_name}.on_success={route} and {head}.input={route}.",
                    [],
                ),
            ]
        )
    return result


def prove_checker(baseline: Mapping[str, Any]) -> None:
    """The graph checker must accept the baseline and reject two mutations of it."""
    if check_graph(baseline, baseline, []):
        raise OperationFailed("graph checker rejected an unchanged graph")
    for mutate in (_change_description, _break_input):
        control = copy.deepcopy(dict(baseline))
        mutate(control)
        if not check_graph(baseline, control, []):
            raise OperationFailed("graph checker accepted a changed graph")


def _change_description(state: JsonObject) -> None:
    node = state["nodes"][0]
    node["description"] = (node["description"] if "description" in node and node["description"] else "") + " negative control"


def _break_input(state: JsonObject) -> None:
    state["nodes"][0]["input"] = "broken_negative_control"


# ── provider evidence ─────────────────────────────────────────────────────


def provider_evidence(client: WebClient, session_id: str, operation_id: str) -> JsonObject:
    """LLM-call audit rows and tool names recorded for one operation."""
    rows = list_pages(client, f"/api/sessions/{session_id}/messages?include_llm_audit=true&include_tool_rows=true")
    calls: list[JsonObject] = []
    tools: list[str] = []
    for row in rows:
        if row["operation_id"] != operation_id or row["tool_calls"] is None:
            continue
        for entry in row["tool_calls"]:
            if row["role"] == "audit" and "_kind" in entry and entry["_kind"] == "llm_call_audit":
                calls.append(entry["call"])
            elif row["role"] == "assistant" and "function" in entry and "name" in entry["function"]:
                tools.append(entry["function"]["name"])
    latencies = [call["latency_ms"] for call in calls if type(call["latency_ms"]) in (int, float)]
    return {
        "provider_calls": len(calls),
        "successful_provider_calls": sum(1 for call in calls if call["status"] == "success"),
        "failed_provider_statuses": sorted(call["status"] for call in calls if call["status"] != "success"),
        "tool_names": tools,
        "provider_latency_ms_total": sum(latencies),
        "provider_latency_ms_max": max(latencies) if latencies else None,
        "provider_call_over_300_seconds": any(value > LONG_PROVIDER_CALL_MS for value in latencies),
    }


def publication_issues(result: Mapping[str, Any], evidence: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    if result["status"] != "completed":
        issues.append("operation_status:" + str(result["status"]))
    elif result["result"] is None:
        issues.append("no_result")
    if evidence["successful_provider_calls"] == 0:
        issues.append("no_successful_provider_call")
    return issues


# ── checkpoints ────────────────────────────────────────────────────────────

STEP_KEYS = frozenset({"number", "operation_id", "base_state_id", "message", "expected_order"})


def phase_name(attempt: str, phase: str, session_id: str) -> str:
    return f"battery-{attempt}-{phase}-{session_id}"


def load_phase(store: CheckpointStore, name: str, session_id: str) -> JsonObject | None:
    saved = store.read(name)
    if saved is None:
        return None
    steps = saved["steps"] if "steps" in saved else None
    valid = (
        {"session_id", "baseline", "last_state", "steps", "complete"} <= set(saved)
        and saved["session_id"] == session_id
        and type(saved["baseline"]) is dict
        and type(saved["last_state"]) is dict
        and type(saved["complete"]) is bool
        and type(steps) is list
        and all(type(step) is dict and set(step) >= STEP_KEYS for step in steps)
    )
    if not valid:
        raise OperationFailed(f"checkpoint {name} is not a complete, valid checkpoint for this session")
    return saved


# ── turns ──────────────────────────────────────────────────────────────────


@dataclass
class Battery:
    client: WebClient
    auth: AccountAuth
    store: CheckpointStore
    session_id: str
    attempt: str
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.time
    poll_seconds: float = 10.0
    refresh_margin_seconds: float = 420.0
    prompt_secret: Callable[[str], str] = getpass.getpass

    def keep_token_fresh(self) -> None:
        if token_expiry(self.client.token) < self.now() + self.refresh_margin_seconds:
            self.client.refresh()

    def state(self) -> JsonObject:
        return require_dict(self.client.get_json(f"/api/sessions/{self.session_id}/state"), "state")

    def wait_for(self, operation_id: str) -> JsonObject:
        while True:
            result = require_dict(self.client.get_json(f"/api/sessions/{self.session_id}/operations/{operation_id}"), "operation")
            if result["status"] not in LIVE_STATUSES:
                return result
            self.sleep(self.poll_seconds)

    def run_phase(self, *, repair: bool, cycles: int, reviewed: bool) -> bool:
        phase = ("repair-reviewed" if reviewed else "repair") if repair else ("edits-reviewed" if reviewed else "edits")
        name = phase_name(self.attempt, phase, self.session_id)
        checkpoint = load_phase(self.store, name, self.session_id)
        if checkpoint is None:
            baseline = self.state()
            if not repair:
                repaired = load_phase(
                    self.store, phase_name(self.attempt, "repair-reviewed" if reviewed else "repair", self.session_id), self.session_id
                )
                if repaired is None or not repaired["complete"] or repaired["last_state"] != baseline:
                    raise OperationFailed("the edits phase needs a complete repair phase whose final graph is the live graph")
                prove_checker(baseline)
            checkpoint = {"session_id": self.session_id, "baseline": baseline, "last_state": baseline, "steps": [], "complete": False}
            self.store.write(name, checkpoint)
        if checkpoint["complete"]:
            emit("real_graph_phase_already_complete", phase=phase)
            return True
        if "stopped_reason" in checkpoint:
            emit("real_graph_battery_stopped", phase=phase, reason=checkpoint["stopped_reason"])
            return False
        steps: list[tuple[str, list[str]]]
        if repair:
            steps = [(REVIEWED_REPAIR_INSTRUCTION if reviewed else REPAIR_INSTRUCTION, [])]
        else:
            steps = edit_instructions(checkpoint["baseline"], cycles)
        for index, (instruction, order) in enumerate(steps):
            saved_steps: list[JsonObject] = checkpoint["steps"]
            if index < len(saved_steps) and "verified" in saved_steps[index] and saved_steps[index]["verified"]:
                continue
            self.keep_token_fresh()
            if index == len(saved_steps):
                state = self.state()
                if state != checkpoint["last_state"]:
                    raise OperationFailed("the live graph changed outside the battery; start a new --attempt")
                step: JsonObject = {
                    "number": index + 1,
                    "operation_id": str(uuid4()),
                    "base_state_id": state["id"],
                    "message": instruction,
                    "expected_order": order,
                    "started_at": self.now(),
                }
                saved_steps.append(step)
                self.store.write(name, checkpoint)
            else:
                step = saved_steps[index]
            if "submitted" not in step:
                accepted = require_dict(
                    self.client.post_json(
                        f"/api/sessions/{self.session_id}/messages",
                        {"operation_id": step["operation_id"], "state_id": step["base_state_id"], "content": step["message"]},
                    ),
                    "admission",
                )
                if accepted["operation_id"] != step["operation_id"]:
                    raise OperationFailed("the server admitted a different operation id")
                step["submitted"] = True
                self.store.write(name, checkpoint)
                emit("real_graph_turn_submitted", phase=phase, number=index + 1, operation_id=step["operation_id"])
            result = self.wait_for(step["operation_id"])
            state = self.state()
            found = check_graph(checkpoint["baseline"], state, order, repair=repair)
            if repair and reviewed:
                found.extend(differences(graph_content(checkpoint["last_state"]), graph_content(state)))
            evidence = provider_evidence(self.client, self.session_id, step["operation_id"])
            issues = publication_issues(result, evidence)
            step.update(
                status=result["status"],
                difference_paths=found,
                publication_failures=issues,
                provider_evidence=evidence,
                finished_at=self.now(),
                verified=not found and not issues,
            )
            checkpoint["last_state"] = state
            if not step["verified"]:
                checkpoint["stopped_reason"] = "turn_failed_verification"
            self.store.write(name, checkpoint)
            self.store.write(f"{name}-step-{index + 1}", {"step": step, "state": state, "response": result})
            emit(
                "real_graph_turn_result",
                phase=phase,
                number=index + 1,
                status=result["status"],
                verified=step["verified"],
                difference_paths=found,
                publication_failures=issues,
                tools=evidence["tool_names"],
                provider_latency_ms_max=evidence["provider_latency_ms_max"],
            )
            if not step["verified"]:
                return False
        checkpoint["complete"] = True
        self.store.write(name, checkpoint)
        emit("real_graph_phase_complete", phase=phase, verified_turns=len(steps))
        return True

    def run(self, *, reviewed: bool, cycles: int) -> bool:
        if reviewed:
            reviews = self.store.read(review_checkpoint_name(self.session_id))
            if (
                reviews is None
                or "complete" not in reviews
                or reviews["complete"] is not True
                or reviews["validation"]["is_valid"] is not True
            ):
                raise OperationFailed("run-reviewed needs a complete review repair whose validation passed")
        sign_in(self.client, self.auth, prompt_secret=self.prompt_secret)
        try:
            return self.run_phase(repair=True, cycles=cycles, reviewed=reviewed) and self.run_phase(
                repair=False, cycles=cycles, reviewed=reviewed
            )
        finally:
            self.client.close()


# ── command line ───────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.staging.real_graph_battery",
        description=(
            "Drive provider-backed repair and edit turns against an already copied real graph through ordinary chat "
            "messages, checking the graph after every turn. Real provider calls: dry run unless --execute."
        ),
    )
    parser.add_argument("action", choices=["run", "run-reviewed"], help="run-reviewed requires a completed review repair for the session")
    parser.add_argument("--base-url", help="deployment origin, https://... (env ELSPETH_OPS_BASE_URL)")
    parser.add_argument("--session", help="the copied session to exercise (env ELSPETH_OPS_SESSION)")
    add_account_arguments(parser, prefix="", role="session owner")
    parser.add_argument("--state-dir", help="private checkpoint directory outside the repository (env ELSPETH_OPS_STATE_DIR)")
    parser.add_argument("--attempt", default="1", help="attempt label; a stopped attempt never continues, start a new one after diagnosis")
    parser.add_argument("--cycles", type=int, default=6, help="edit cycles of ten turns each (default 6)")
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    add_execute_flag(parser)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[str], WebClient] = WebClient,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    prompt_secret: Callable[[str], str] = getpass.getpass,
) -> int:
    args = build_parser().parse_args(argv)
    base_url = setting(args.base_url, flag="--base-url", env="ELSPETH_OPS_BASE_URL")
    session_id = require_uuid(setting(args.session, flag="--session", env="ELSPETH_OPS_SESSION"), flag="--session")
    auth = account_from(args, prefix="", env="ELSPETH_OPS_ACCOUNT")
    store = CheckpointStore(Path(setting(args.state_dir, flag="--state-dir", env="ELSPETH_OPS_STATE_DIR")))
    if not args.attempt.isalnum() or args.attempt != args.attempt.lower():
        raise UsageError("--attempt must be lowercase letters and digits")
    if args.cycles < 1:
        raise UsageError("--cycles must be at least 1")
    reviewed = args.action == "run-reviewed"
    if not args.execute:
        emit(
            "dry_run",
            base_url=base_url,
            session_id=session_id,
            account=auth.username,
            phases=["repair" + ("-reviewed" if reviewed else ""), "edits" + ("-reviewed" if reviewed else "")],
            turns=1 + 10 * args.cycles,
            checkpoints=[phase_name(args.attempt, phase, session_id) for phase in ("repair", "edits")],
            note="nothing was contacted; pass --execute to submit turns",
        )
        return 0
    battery = Battery(
        client=client_factory(base_url),
        auth=auth,
        store=store,
        session_id=session_id,
        attempt=args.attempt,
        sleep=sleep,
        now=now,
        poll_seconds=args.poll_seconds,
        prompt_secret=prompt_secret,
    )
    return 0 if battery.run(reviewed=reviewed, cycles=args.cycles) else 1


if __name__ == "__main__":
    raise SystemExit(run_tool(main))
