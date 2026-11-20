"""Drive a planned series of small Composer edits against a large graph and judge every one.

The battery takes a session holding a (very large) graph and an edit plan of
N small edits (default: ``scripts/staging/edit_plans/composer_gold_50.json``).
Each edit is sent as an ordinary chat message, ``POST
/api/sessions/{id}/messages``, and answered by the configured provider; the
battery never writes the graph itself. After each turn it records a verdict:

* the turn completed and at least one provider call succeeded;
* the expected change landed (the edit's expectations hold);
* nothing else changed: every difference from the previous state is covered
  by the edit's expectations or its declared ``may_change`` prefixes;
* no validation error appeared that was not there before;
* the model used one of the edit's expected tools, and no forbidden tool
  (``set_pipeline`` in the default plan) was applied;
* which tools it called, with their server-stamped outcomes;
* whether history was shed in that turn (from the stored context-budget
  records) and whether the turn still passed;
* wall time, server-recorded span, provider time, the server remainder and
  provider-call counts (see :mod:`scripts.staging.turn_evidence`).

The result is one JSON line per edit and a summary: "longest run in a row: K
of N", where gold is N of N, with totals and the server-time share.

Safety properties (named by tests in
``tests/unit/scripts/staging/test_composer_edit_battery.py``):

* Every edit is an ordinary chat message; the battery never writes graph state.
* Each edit's operation id is saved BEFORE it is submitted; a resumed run polls
  that operation instead of submitting again.
* A run resumes only from a checkpoint that is valid for the same session,
  attempt and plan file (by SHA-256); the targets and messages resolved when
  the attempt started are reused, never re-resolved.
* Before each new edit the live graph must equal the state the previous edit
  left; a change made outside the battery stops the attempt.
* By default the first failing edit stops the attempt and the attempt never
  continues past it. ``--keep-going`` judges every edit instead (each against
  the state the previous edit actually left) for a full K-of-N measure.
* A prompt review card is approved only when the plan asks for it, only for
  the named node and kind, only after that edit's expected change verified,
  and only when the card shows exactly the node's current prompt.

Reading evidence uses the audit-grade messages view, which records an access
entry per read; the battery reads only the rows added since the previous edit.
"""

from __future__ import annotations

import argparse
import getpass
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from scripts.staging.edit_plan import (
    DEFAULT_PLAN,
    Plan,
    check_expectations,
    graph_view,
    load_plan,
    new_validation_errors,
    resolve_edits,
    resolve_targets,
    tool_findings,
    unrelated_changes,
)
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
    blob_markers,
    differences,
    emit,
    require_dict,
    require_list,
    require_uuid,
    run_tool,
    setting,
    sign_in,
    token_expiry,
    validate_base_url,
)
from scripts.staging.repair_copied_prompt_reviews import prompt_surface
from scripts.staging.turn_evidence import UNAVAILABLE_FROM_API, shed_state, shed_this_turn, turn_evidence

LIVE_STATUSES = ("queued", "running")
CHECKPOINT_VERSION = 1
CHECKPOINT_KEYS = frozenset(
    {
        "version",
        "session_id",
        "attempt",
        "plan_name",
        "plan_sha256",
        "targets",
        "baseline",
        "last_state",
        "edits",
        "results",
        "rows_seen",
        "shed_history",
    }
)
_MESSAGE_PAGE = 500


def checkpoint_name(attempt: str, session_id: str) -> str:
    return f"edit-battery-{attempt}-{session_id}"


def load_checkpoint(store: CheckpointStore, name: str, *, session_id: str, attempt: str, plan: Plan) -> JsonObject | None:
    saved = store.read(name)
    if saved is None:
        return None
    valid = (
        set(saved) >= CHECKPOINT_KEYS
        and saved["version"] == CHECKPOINT_VERSION
        and saved["session_id"] == session_id
        and saved["attempt"] == attempt
        and type(saved["baseline"]) is dict
        and type(saved["last_state"]) is dict
        and type(saved["edits"]) is list
        and type(saved["results"]) is list
        and len(saved["results"]) <= len(saved["edits"])
        and type(saved["rows_seen"]) is int
        and all(type(result) is dict and {"number", "operation_id", "base_state_id"} <= set(result) for result in saved["results"])
    )
    if not valid:
        raise OperationFailed(f"checkpoint {name} is not a complete, valid checkpoint for this session and attempt")
    if saved["plan_sha256"] != plan.sha256 or saved["plan_name"] != plan.name:
        raise OperationFailed(f"checkpoint {name} was made with a different plan; start a new --attempt to change the plan")
    return saved


# ── summary ────────────────────────────────────────────────────────────────


def _total(results: Sequence[Mapping[str, Any]], key: str) -> int | None:
    values = [result["timing"][key] for result in results]
    return sum(values) if values and all(type(value) is int for value in values) else None


def summarise(checkpoint: Mapping[str, Any]) -> JsonObject:
    """The attempt's K-of-N summary with time, provider and tool totals."""
    total = len(checkpoint["edits"])
    judged = [result for result in checkpoint["results"] if "passed" in result]
    longest = run = 0
    for result in judged:
        run = run + 1 if result["passed"] else 0
        longest = max(longest, run)
    first_failure = next((result["number"] for result in judged if not result["passed"]), None)
    tools: dict[str, int] = {}
    for result in judged:
        for name, count in result["evidence"]["tool_calls_by_name"].items():
            tools[name] = tools[name] + count if name in tools else count
    span = _total(judged, "span_ms")
    server = _total(judged, "server_ms")
    wall = _total(judged, "wall_ms")
    busy = _total(judged, "provider_busy_ms")
    shed = [result for result in judged if result["history_shed"]]
    return {
        "plan": checkpoint["plan_name"],
        "edits": total,
        "judged": len(judged),
        "passed": sum(1 for result in judged if result["passed"]),
        "longest_run": longest,
        "gold": longest == total,
        "line": f"longest run in a row: {longest} of {total}",
        "first_failure": first_failure,
        "stopped_reason": checkpoint["stopped_reason"] if "stopped_reason" in checkpoint else None,
        "totals": {
            "wall_ms": wall,
            "span_ms": span,
            "provider_busy_ms": busy,
            "provider_latency_ms_sum": _total(judged, "provider_latency_ms_sum"),
            "server_ms": server,
            "provider_calls": sum(result["evidence"]["provider_calls"] for result in judged),
            "tool_calls": sum(tools.values()),
            "tool_calls_by_name": dict(sorted(tools.items())),
            "polls": sum(result["timing"]["polls"] for result in judged),
        },
        "server_time_share_of_span": round(server / span, 4) if server is not None and span else None,
        "non_provider_share_of_wall": round((wall - busy) / wall, 4) if wall is not None and busy is not None and wall else None,
        "history_shed": {"edits": len(shed), "passed": sum(1 for result in shed if result["passed"])},
        "not_available_from_api": list(UNAVAILABLE_FROM_API),
    }


# ── the run ────────────────────────────────────────────────────────────────


@dataclass
class EditBattery:
    client: WebClient
    auth: AccountAuth
    store: CheckpointStore
    session_id: str
    attempt: str
    plan: Plan
    overrides: Mapping[str, str] = field(default_factory=dict)
    keep_going: bool = False
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.time
    poll_seconds: float = 2.0
    refresh_margin_seconds: float = 420.0
    prompt_secret: Callable[[str], str] = getpass.getpass

    @property
    def name(self) -> str:
        return checkpoint_name(self.attempt, self.session_id)

    def keep_token_fresh(self) -> None:
        if token_expiry(self.client.token) < self.now() + self.refresh_margin_seconds:
            self.client.refresh()

    def state(self) -> JsonObject:
        return require_dict(self.client.get_json(f"/api/sessions/{self.session_id}/state"), "state")

    def rows_from(self, offset: int) -> list[JsonObject]:
        rows: list[JsonObject] = []
        while True:
            page = require_list(
                self.client.get_json(
                    f"/api/sessions/{self.session_id}/messages?include_llm_audit=true&include_tool_rows=true"
                    f"&limit={_MESSAGE_PAGE}&offset={offset + len(rows)}"
                ),
                "messages",
            )
            rows.extend(require_dict(row, "message") for row in page)
            if len(page) < _MESSAGE_PAGE:
                return rows

    def read_text(self, marker: Mapping[str, Any]) -> str:
        data = self.client.download(f"/api/sessions/{self.session_id}/blobs/{marker['blob_ref']}/content")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise OperationFailed(f"prompt file {marker['blob_ref']} is not UTF-8 text") from None

    def wait_for(self, operation_id: str, first_wait_ms: int | None) -> tuple[JsonObject, int]:
        polls = 0
        wait = max(self.poll_seconds, (first_wait_ms or 0) / 1000)
        while True:
            result = require_dict(self.client.get_json(f"/api/sessions/{self.session_id}/operations/{operation_id}"), "operation")
            polls += 1
            if result["status"] not in LIVE_STATUSES:
                return result, polls
            self.sleep(wait)
            hint = result["poll_after_ms"] if "poll_after_ms" in result and type(result["poll_after_ms"]) is int else 0
            wait = max(self.poll_seconds, hint / 1000)

    def start(self) -> JsonObject:
        checkpoint = load_checkpoint(self.store, self.name, session_id=self.session_id, attempt=self.attempt, plan=self.plan)
        if checkpoint is not None:
            if self.overrides and self.overrides != checkpoint["targets"]["overrides"]:
                raise OperationFailed("--target values differ from the ones this attempt started with")
            return checkpoint
        baseline = self.state()
        targets = resolve_targets(self.plan, baseline, self.overrides)
        rows = self.rows_from(0)
        checkpoint = {
            "version": CHECKPOINT_VERSION,
            "session_id": self.session_id,
            "attempt": self.attempt,
            "plan_name": self.plan.name,
            "plan_sha256": self.plan.sha256,
            "targets": {"resolved": targets, "overrides": dict(self.overrides)},
            "baseline": baseline,
            "last_state": baseline,
            "edits": resolve_edits(self.plan, targets, baseline),
            "results": [],
            "rows_seen": len(rows),
            "shed_history": shed_state(rows),
        }
        self.store.write(self.name, checkpoint)
        emit("edit_battery_started", plan=self.plan.name, edits=len(checkpoint["edits"]), targets=targets)
        return checkpoint

    def approve_reviews(
        self, edit: Mapping[str, Any], state: Mapping[str, Any], checkpoint: JsonObject, approved: list[JsonObject]
    ) -> None:
        """Resolve the edit's own prompt review cards, each checked against the node's current prompt.

        Each approval is saved as soon as it is made, so a resumed run counts it.
        """
        approve = edit["approve"]
        nodes = {node["id"]: node for node in state["nodes"]}
        pending = require_dict(self.client.get_json(f"/api/sessions/{self.session_id}/interpretations?status=pending"), "reviews")["events"]
        for event in pending:
            if event["kind"] != approve["kind"]:
                continue
            snapshot = require_dict(
                self.client.get_json(f"/api/sessions/{self.session_id}/interpretations/{event['id']}/prompt"), "prompt review snapshot"
            )
            if snapshot["node_id"] != approve["node"]:
                continue
            if snapshot["is_live_review"] is not True or approve["node"] not in nodes:
                raise OperationFailed(f"review {event['id']} for {approve['node']} is not a live review of a node in the graph")
            surface = prompt_surface(nodes[approve["node"]]["options"])
            if differences(snapshot["authored_surface"], surface):
                raise OperationFailed(f"review {event['id']} does not show the node's current prompt; not approved")
            bindings = require_list(snapshot["bindings"], "bindings")
            if sorted(binding["blob_id"] for binding in bindings) != sorted(marker["blob_ref"] for marker in blob_markers(surface)):
                raise OperationFailed(f"review {event['id']} bindings are not exactly the prompt's files; not approved")
            for binding in bindings:
                if binding["text"] != self.read_text({"blob_ref": binding["blob_id"]}):
                    raise OperationFailed(f"review {event['id']} shows text that differs from file {binding['blob_id']}; not approved")
            self.client.post_json(
                f"/api/sessions/{self.session_id}/interpretations/{event['id']}/resolve",
                {"choice": "accepted_as_drafted", "reviewed_anchor": snapshot["anchor"]},
            )
            approved.append({"event_id": event["id"], "kind": event["kind"], "node_id": approve["node"]})
            self.store.write(self.name, checkpoint)

    def judge(
        self, edit: Mapping[str, Any], checkpoint: Mapping[str, Any], before_state: Mapping[str, Any], after_state: Mapping[str, Any]
    ) -> tuple[list[str], list[str]]:
        before, after = graph_view(before_state), graph_view(after_state)
        failures, covered = check_expectations(edit, before, after, graph_view(checkpoint["baseline"]), self.read_text)
        unrelated = unrelated_changes(before, after, [*covered, *edit["may_change"]])
        return failures, unrelated

    def run_edit(self, checkpoint: JsonObject, index: int) -> JsonObject:
        edit = checkpoint["edits"][index]
        results: list[JsonObject] = checkpoint["results"]
        self.keep_token_fresh()
        if index == len(results):
            live = self.state()
            if differences(graph_view(live), graph_view(checkpoint["last_state"])):
                raise OperationFailed("the live graph changed outside the battery; start a new --attempt")
            results.append({"number": edit["number"], "id": edit["id"], "operation_id": str(uuid4()), "base_state_id": live["id"]})
            self.store.write(self.name, checkpoint)
        result = results[index]
        if "submitted_at" not in result:
            submitted_at = self.now()
            accepted = require_dict(
                self.client.post_json(
                    f"/api/sessions/{self.session_id}/messages",
                    {"operation_id": result["operation_id"], "state_id": result["base_state_id"], "content": edit["message"]},
                ),
                "admission",
            )
            if accepted["operation_id"] != result["operation_id"]:
                raise OperationFailed("the server admitted a different operation id")
            result["submitted_at"] = submitted_at
            result["poll_after_ms"] = accepted["poll_after_ms"] if "poll_after_ms" in accepted else None
            self.store.write(self.name, checkpoint)
            emit("edit_submitted", number=edit["number"], id=edit["id"], operation_id=result["operation_id"])
        operation, polls = self.wait_for(result["operation_id"], result["poll_after_ms"])
        observed_at = self.now()
        before_state = checkpoint["last_state"]
        after_state = self.state()
        rows = self.rows_from(checkpoint["rows_seen"])
        rows_seen = checkpoint["rows_seen"] + len(rows)
        if not any(row["operation_id"] == result["operation_id"] for row in rows):
            rows = self.rows_from(0)
            rows_seen = len(rows)
        evidence = turn_evidence(rows, result["operation_id"], shed_before=checkpoint["shed_history"])
        completed = operation["status"] == "completed" and operation["result"] is not None
        failures, unrelated = self.judge(edit, checkpoint, before_state, after_state)
        if "approvals" not in result:
            result["approvals"] = []
        if edit["approve"] is not None and completed and not failures:
            self.approve_reviews(edit, after_state, checkpoint, result["approvals"])
            after_state = self.state()
            failures, unrelated = self.judge(edit, checkpoint, before_state, after_state)
        findings: list[str] = []
        if not completed:
            error = operation["error"] if "error" in operation else None
            findings.append(
                "turn_not_completed:"
                + str(operation["status"])
                + (":" + str(error["code"]) if type(error) is dict and "code" in error else "")
            )
        if evidence["successful_provider_calls"] == 0:
            findings.append("no_successful_provider_call")
        findings.extend(failures)
        findings.extend("unrelated_change:" + path for path in unrelated)
        findings.extend("new_validation_error:" + key for key in new_validation_errors(before_state, after_state))
        findings.extend(tool_findings(edit, evidence, self.plan.forbidden_tools))
        wall = round((observed_at - result["submitted_at"]) * 1000)
        result.update(
            status=operation["status"],
            passed=not findings,
            findings=findings,
            evidence=evidence,
            history_shed=shed_this_turn(evidence),
            timing={
                "wall_ms": wall,
                "polls": polls,
                "poll_interval_s": self.poll_seconds,
                "span_ms": evidence["span_ms"],
                "provider_busy_ms": evidence["provider_busy_ms"],
                "provider_latency_ms_sum": evidence["provider_latency_ms_sum"],
                "server_ms": evidence["server_ms"],
                "non_provider_wall_ms": wall - evidence["provider_busy_ms"] if evidence["provider_busy_ms"] is not None else None,
            },
            state_id_after=after_state["id"],
        )
        checkpoint["last_state"] = after_state
        checkpoint["rows_seen"] = rows_seen
        if evidence["history"]["shed_history_after"] is not None:
            checkpoint["shed_history"] = evidence["history"]["shed_history_after"]
        if findings and not self.keep_going:
            checkpoint["stopped_reason"] = f"edit {edit['number']} ({edit['id']}) failed verification"
        self.store.write(self.name, checkpoint)
        self.store.write(f"{self.name}-edit-{edit['number']}", {"result": result, "state": after_state, "operation": operation})
        emit(
            "edit_result",
            **{key: result[key] for key in ("number", "id", "status", "passed", "findings", "history_shed", "timing")},
            tools=evidence["tool_calls_by_name"],
            provider_calls=evidence["provider_calls"],
        )
        return result

    def run(self) -> JsonObject:
        sign_in(self.client, self.auth, prompt_secret=self.prompt_secret)
        try:
            checkpoint = self.start()
            for index in range(len(checkpoint["edits"])):
                if "stopped_reason" in checkpoint:
                    break
                if index < len(checkpoint["results"]) and "passed" in checkpoint["results"][index]:
                    continue
                self.run_edit(checkpoint, index)
        finally:
            self.client.close()
        summary = summarise(checkpoint)
        self.store.write(f"{self.name}-summary", summary)
        emit("edit_battery_summary", **summary)
        return summary


# ── command line ───────────────────────────────────────────────────────────


def parse_targets(values: Sequence[str]) -> dict[str, str]:
    targets: dict[str, str] = {}
    for value in values:
        name, separator, target = value.partition("=")
        if not separator or not name or not target or name in targets:
            raise UsageError(f"--target takes NAME=ID once per name, got {value!r}")
        targets[name] = target
    return targets


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.staging.composer_edit_battery",
        description=(
            "Send a plan of small edits to the Composer as ordinary chat messages and judge each one: completed, "
            "expected change landed, nothing else changed, tools used, history shedding, time and provider calls. "
            "Ends with 'longest run in a row: K of N' (gold is N of N). Real provider calls: dry run unless --execute."
        ),
    )
    parser.add_argument(
        "action",
        choices=["run", "plan", "report"],
        help="run the battery; plan: validate and list the plan; report: summarise a saved attempt (no network)",
    )
    parser.add_argument(
        "--base-url", help="server origin: https://..., or http://localhost:PORT for a local server (env ELSPETH_OPS_BASE_URL)"
    )
    parser.add_argument("--session", help="the session holding the graph to edit (env ELSPETH_OPS_SESSION)")
    add_account_arguments(parser, prefix="", role="session owner")
    parser.add_argument("--state-dir", help="private checkpoint directory outside the repository (env ELSPETH_OPS_STATE_DIR)")
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN, help="edit plan JSON (default: the shipped 50-edit gold plan)")
    parser.add_argument("--target", action="append", default=[], metavar="NAME=ID", help="name the graph component for a plan target")
    parser.add_argument("--attempt", default="1", help="attempt label; a stopped attempt never continues, start a new one after diagnosis")
    parser.add_argument("--keep-going", action="store_true", help="judge every edit instead of stopping at the first failure")
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=2.0,
        help="minimum seconds between operation polls (the server's poll_after_ms is honoured when longer)",
    )
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
    plan = load_plan(args.plan)
    overrides = parse_targets(args.target)
    if args.action == "plan":
        tools = sorted({tool for edit in plan.edits if "tools_any" in edit for tool in edit["tools_any"]})
        emit(
            "edit_plan",
            name=plan.name,
            sha256=plan.sha256,
            edits=len(plan.edits),
            targets=dict(plan.targets),
            expected_tools=tools,
            forbidden_tools=list(plan.forbidden_tools),
        )
        for number, edit in enumerate(plan.edits, start=1):
            emit("planned_edit", number=number, id=edit["id"], message=edit["message"])
        return 0
    session_id = require_uuid(setting(args.session, flag="--session", env="ELSPETH_OPS_SESSION"), flag="--session")
    store = CheckpointStore(Path(setting(args.state_dir, flag="--state-dir", env="ELSPETH_OPS_STATE_DIR")))
    if not args.attempt.isalnum() or args.attempt != args.attempt.lower():
        raise UsageError("--attempt must be lowercase letters and digits")
    if args.poll_seconds <= 0:
        raise UsageError("--poll-seconds must be positive")
    if args.action == "report":
        saved = load_checkpoint(store, checkpoint_name(args.attempt, session_id), session_id=session_id, attempt=args.attempt, plan=plan)
        if saved is None:
            raise UsageError("no saved attempt for this session, attempt and state directory")
        for result in saved["results"]:
            if "passed" in result:
                emit(
                    "edit_result",
                    **{key: result[key] for key in ("number", "id", "status", "passed", "findings", "history_shed", "timing")},
                )
        emit("edit_battery_summary", **summarise(saved))
        return 0
    base_url = validate_base_url(setting(args.base_url, flag="--base-url", env="ELSPETH_OPS_BASE_URL"))
    auth = account_from(args, prefix="", env="ELSPETH_OPS_ACCOUNT")
    if not args.execute:
        emit(
            "dry_run",
            base_url=base_url,
            session_id=session_id,
            account=auth.username,
            plan=plan.name,
            edits=len(plan.edits),
            checkpoint=checkpoint_name(args.attempt, session_id),
            note="nothing was contacted; pass --execute to send the edits",
        )
        return 0
    battery = EditBattery(
        client=client_factory(base_url),
        auth=auth,
        store=store,
        session_id=session_id,
        attempt=args.attempt,
        plan=plan,
        overrides=overrides,
        keep_going=args.keep_going,
        sleep=sleep,
        now=now,
        poll_seconds=args.poll_seconds,
        prompt_secret=prompt_secret,
    )
    return 0 if battery.run()["gold"] else 1


if __name__ == "__main__":
    raise SystemExit(run_tool(main))
