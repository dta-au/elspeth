"""Drive a planned series of small Composer edits against a large graph and judge every one.

The battery takes a session holding a (very large) graph and an edit plan of
small edits (default: ``scripts/staging/edit_plans/composer_gold_50.json``,
for graphs whose prompts are all held in files; the mixed plan
``composer_mixed_50.json`` adds inline-prompt edits). Each edit is sent as an
ordinary chat message, ``POST /api/sessions/{id}/messages``, and answered by
the configured provider; the battery never writes the graph itself. Per edit:

* Before sending, every expectation must be FALSE on the live graph. An edit
  whose change already holds is a plan or precondition error, recorded as a
  failure and never sent.
* After the turn, its evidence rows are read from the audit-grade messages
  view with bounded polling until they stop changing (they can land after the
  operation reports completed).
* The verdict: the turn completed and a provider call succeeded; the
  expected change landed; nothing else changed (every difference is covered
  by an expectation, the edit's ``may_change`` prefixes or its
  ``edges_touching`` components, or is a field the server writes itself,
  which is allowed by name and listed as server-written); no new validation
  error; one of the edit's acceptable tools was used; no forbidden tool was
  called without being rejected.
* A tool call the server rejected inside a passing edit is a warning, counted
  in the summary as "tool calls rejected then corrected"; it does not fail the
  edit.
* History shedding, wall time, server-recorded span, provider time, the server
  remainder and provider-call counts (see :mod:`scripts.staging.turn_evidence`).

Gold is a COMPLETE attempt: every applicable plan edit judged and passed, in
one attempt, with at least ``GOLD_MINIMUM_EDITS`` applicable edits. An edit
whose optional graph role is absent is skipped as not applicable: reported,
not judged, and it neither extends nor breaks the run in a row. The summary
also reports tool coverage: the editing tools used at least once against the
editing tools the Composer offers.

Safety properties (named by tests in
``tests/unit/scripts/staging/test_composer_edit_battery.py``):

* Every edit is an ordinary chat message; the battery never writes graph state.
* The session must be in ``auto_commit`` trust mode. In ``explicit_approve``
  each edit becomes a proposal awaiting the user's acceptance, which the
  battery does not give on their behalf, so it refuses to start.
* A new attempt refuses to start on a graph holding leftovers of an earlier
  attempt (``battery_*`` steps or outputs, battery marker text in a prompt, the
  battery's pipeline name); ``cleanup`` reports them and, with ``--execute``,
  asks the Composer to remove them in ordinary chat turns.
* Each edit's operation id is saved BEFORE it is submitted; a resumed run polls
  that operation instead of submitting again.
* A run resumes only from a checkpoint that is valid for the same session,
  attempt and plan file (by SHA-256); the targets and messages resolved when
  the attempt started are reused, never re-resolved.
* Before each new edit the live graph must equal the state the previous edit
  left; a change made outside the battery stops the attempt.
* By default the first failing edit stops the attempt and the attempt never
  continues past it. ``--keep-going`` judges every edit instead (each against
  the state the previous edit actually left) for a full K-of-N measure; such
  an attempt is never gold once an edit failed.
* A prompt review card is approved only when the plan asks for it, only for
  the named node and kind, only on a turn whose verdict is otherwise clean
  (expectations hold, nothing unexpected changed, no new validation error, no
  tool finding), and only when the card shows exactly the node's current
  prompt. The approval itself may change only server-written fields.
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
    MARKER_TEXTS,
    TEMPORARY_PREFIX,
    Plan,
    as_found_differences,
    check_expectations,
    classify_changes,
    graph_view,
    leftover_findings,
    load_plan,
    new_validation_errors,
    preconditions,
    resolve_edits,
    resolve_targets,
    tool_findings,
    tool_warnings,
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
from scripts.staging.turn_evidence import UNAVAILABLE_FROM_API, operation_rows, shed_state, shed_this_turn, turn_evidence

LIVE_STATUSES = ("queued", "running")
CHECKPOINT_VERSION = 2
GOLD_MINIMUM_EDITS = 50
AUTO_COMMIT = "auto_commit"
# The Composer's graph-editing tools (registry kinds MUTATION, BLOB_MUTATION and
# SECRET_MUTATION); a test pins this list to the registry.
EDITING_TOOLS = (
    "change_substring",
    "clear_source",
    "create_blob",
    "delete_blob",
    "patch_node_options",
    "patch_output_options",
    "patch_source_options",
    "remove_edge",
    "remove_node",
    "remove_output",
    "set_metadata",
    "set_output",
    "set_pipeline",
    "set_source",
    "set_source_from_blob",
    "set_source_from_blobs",
    "splice_transform",
    "update_blob",
    "upsert_edge",
    "upsert_node",
    "wire_blob_inline_ref",
    "wire_secret_ref",
)
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
_USED_OUTCOMES = ("applied", "completed")


def checkpoint_name(attempt: str, session_id: str) -> str:
    return f"edit-battery-{attempt}-{session_id}"


def _valid_result(result: object) -> bool:
    if type(result) is not dict or "number" not in result:
        return False
    return "skipped" in result or {"operation_id", "base_state_id"} <= set(result)


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
        and all(_valid_result(result) for result in saved["results"])
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


def _used_tools(results: Sequence[Mapping[str, Any]]) -> set[str]:
    return {
        name
        for result in results
        for name, outcomes in result["evidence"]["tool_outcomes"].items()
        if any(outcome in _USED_OUTCOMES for outcome in outcomes)
    }


def tool_coverage(used: set[str]) -> JsonObject:
    """Editing tools used at least once against the editing tools the Composer offers."""
    used_editing = sorted(used & set(EDITING_TOOLS))
    return {
        "line": f"editing tools used: {len(used_editing)} of {len(EDITING_TOOLS)}",
        "used": used_editing,
        "not_used": sorted(set(EDITING_TOOLS) - used),
        "other_tools_used": sorted(used - set(EDITING_TOOLS)),
    }


def summarise(checkpoint: Mapping[str, Any]) -> JsonObject:
    """The attempt's K-of-N summary with time, provider and tool totals."""
    applicable = [edit for edit in checkpoint["edits"] if edit["applicable"]]
    skipped = [edit for edit in checkpoint["edits"] if not edit["applicable"]]
    judged = [result for result in checkpoint["results"] if "passed" in result]
    sent = [result for result in judged if "evidence" in result]
    longest = run = 0
    for result in judged:
        run = run + 1 if result["passed"] else 0
        longest = max(longest, run)
    passed = sum(1 for result in judged if result["passed"])
    complete = len(judged) == len(applicable)
    first_failure = next((result["number"] for result in judged if not result["passed"]), None)
    tools: dict[str, int] = {}
    for result in sent:
        for name, count in result["evidence"]["tool_calls_by_name"].items():
            tools[name] = tools[name] + count if name in tools else count
    span = _total(sent, "span_ms")
    server = _total(sent, "server_ms")
    wall = _total(sent, "wall_ms")
    busy = _total(sent, "provider_busy_ms")
    shed = [result for result in sent if result["history_shed"]]
    corrected = sum(int(warning.rsplit(":", 1)[1]) for result in judged if result["passed"] for warning in result["warnings"])
    gold = complete and passed == len(applicable) and len(applicable) >= GOLD_MINIMUM_EDITS
    return {
        "plan": checkpoint["plan_name"],
        "edits": len(checkpoint["edits"]),
        "applicable": len(applicable),
        "skipped_not_applicable": [
            {"number": edit["number"], "id": edit["id"], "missing_roles": edit["missing_roles"]} for edit in skipped
        ],
        "judged": len(judged),
        "complete": complete,
        "passed": passed,
        "longest_run": longest,
        "gold": gold,
        "gold_requires": f"every applicable edit judged and passed in one attempt, at least {GOLD_MINIMUM_EDITS} applicable",
        "line": f"longest run in a row: {longest} of {len(applicable)}" + ("" if complete else f" (incomplete: {len(judged)} judged)"),
        "first_failure": first_failure,
        "stopped_reason": checkpoint["stopped_reason"] if "stopped_reason" in checkpoint else None,
        "as_found_differences": checkpoint["as_found_differences"] if "as_found_differences" in checkpoint else None,
        "tool_calls_rejected_then_corrected": corrected,
        "server_written_changes": sum(len(result["server_written"]) for result in sent),
        "tool_coverage": tool_coverage(_used_tools(sent)),
        "totals": {
            "wall_ms": wall,
            "span_ms": span,
            "provider_busy_ms": busy,
            "provider_latency_ms_sum": _total(sent, "provider_latency_ms_sum"),
            "server_ms": server,
            "provider_calls": sum(result["evidence"]["provider_calls"] for result in sent),
            "tool_calls": sum(tools.values()),
            "tool_calls_by_name": dict(sorted(tools.items())),
            "polls": sum(result["timing"]["polls"] for result in sent),
        },
        "server_time_share_of_span": round(server / span, 4) if server is not None and span else None,
        "non_provider_share_of_wall": round((wall - busy) / wall, 4) if wall is not None and busy is not None and wall else None,
        "history_shed": {"edits": len(shed), "passed": sum(1 for result in shed if result["passed"])},
        "not_available_from_api": list(UNAVAILABLE_FROM_API),
    }


def plan_coverage(plan: Plan) -> JsonObject:
    """The editing tools a plan's edits accept, against the editing tools the Composer offers."""
    expected = set(plan.expected_tools())
    return {
        "line": f"editing tools the plan exercises: {len(expected & set(EDITING_TOOLS))} of {len(EDITING_TOOLS)}",
        "exercised": sorted(expected & set(EDITING_TOOLS)),
        "not_exercised": sorted(set(EDITING_TOOLS) - expected),
        "forbidden": list(plan.forbidden_tools),
        "edits_per_tool": {
            tool: sum(1 for edit in plan.edits for key in ("tools_any", "tools_all") if key in edit and tool in edit[key])
            for tool in sorted(expected)
        },
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
    settle_reads: int = 6
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

    def require_auto_commit(self) -> None:
        preferences = require_dict(self.client.get_json(f"/api/sessions/{self.session_id}/composer/preferences"), "preferences")
        if preferences["trust_mode"] != AUTO_COMMIT:
            raise OperationFailed(
                f"the session's trust mode is {preferences['trust_mode']}: each edit would wait as a proposal for the user to accept, "
                "and the battery does not accept proposals on the user's behalf. Switch the session to auto-commit "
                "(trust mode auto_commit) in the Composer and start again."
            )

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

    def settled_rows(self, offset: int, operation_id: str) -> tuple[list[JsonObject], int, int, bool]:
        """``(rows, new offset, reads, settled)``: the turn's rows once two reads agree and its provider audit is present.

        Bounded at ``settle_reads`` reads of the audit-grade view, ``poll_seconds`` apart.
        """
        previous: int | None = None
        rows: list[JsonObject] = []
        base = offset
        for read in range(1, self.settle_reads + 1):
            base = offset
            rows = self.rows_from(offset)
            if not operation_rows(rows, operation_id):
                base = 0
                rows = self.rows_from(0)
            own = operation_rows(rows, operation_id)
            audited = any(row["role"] == "audit" for row in own)
            if audited and previous == len(own):
                return rows, base + len(rows), read, True
            previous = len(own) if audited else None
            if read < self.settle_reads:
                self.sleep(self.poll_seconds)
        return rows, base + len(rows), self.settle_reads, False

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

    def send(self, operation_id: str, state_id: str, message: str) -> JsonObject:
        accepted = require_dict(
            self.client.post_json(
                f"/api/sessions/{self.session_id}/messages",
                {"operation_id": operation_id, "state_id": state_id, "content": message},
            ),
            "admission",
        )
        if accepted["operation_id"] != operation_id:
            raise OperationFailed("the server admitted a different operation id")
        return accepted

    def start(self) -> JsonObject:
        checkpoint = load_checkpoint(self.store, self.name, session_id=self.session_id, attempt=self.attempt, plan=self.plan)
        self.require_auto_commit()
        if checkpoint is not None:
            if self.overrides and self.overrides != checkpoint["targets"]["overrides"]:
                raise OperationFailed("--target values differ from the ones this attempt started with")
            return checkpoint
        baseline = self.state()
        leftovers = leftover_findings(baseline, self.read_text)
        if leftovers:
            raise OperationFailed(
                "this graph holds leftovers of an earlier battery attempt: "
                + ", ".join(leftovers)
                + ". Run the cleanup action to report or remove them, then start again."
            )
        targets = resolve_targets(self.plan, baseline, self.overrides)
        rows = self.rows_from(0)
        edits = resolve_edits(self.plan, targets, baseline)
        checkpoint = {
            "version": CHECKPOINT_VERSION,
            "session_id": self.session_id,
            "attempt": self.attempt,
            "plan_name": self.plan.name,
            "plan_sha256": self.plan.sha256,
            "targets": {"resolved": targets, "overrides": dict(self.overrides)},
            "baseline": baseline,
            "last_state": baseline,
            "edits": edits,
            "results": [],
            "rows_seen": len(rows),
            "shed_history": shed_state(rows),
        }
        self.store.write(self.name, checkpoint)
        emit(
            "edit_battery_started",
            plan=self.plan.name,
            edits=len(edits),
            applicable=sum(1 for edit in edits if edit["applicable"]),
            targets=targets,
        )
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
    ) -> tuple[list[str], list[str], list[str]]:
        """``(expectation failures, unrelated changes, server-written changes)``."""
        before, after = graph_view(before_state), graph_view(after_state)
        failures, covered = check_expectations(edit, before, after, graph_view(checkpoint["baseline"]), self.read_text)
        changes = classify_changes(before, after, [*covered, *edit["may_change"]])
        return failures, changes.unrelated, changes.server_written

    def not_sent(self, checkpoint: JsonObject, edit: Mapping[str, Any], live: Mapping[str, Any], findings: list[str]) -> JsonObject:
        result: JsonObject = {
            "number": edit["number"],
            "id": edit["id"],
            "operation_id": None,
            "base_state_id": live["id"],
            "status": "not_sent",
            "passed": False,
            "findings": findings,
            "warnings": [],
        }
        checkpoint["results"].append(result)
        if not self.keep_going:
            checkpoint["stopped_reason"] = f"edit {edit['number']} ({edit['id']}) was not sent: its change already holds"
        self.store.write(self.name, checkpoint)
        emit("edit_result", number=edit["number"], id=edit["id"], status="not_sent", passed=False, findings=findings)
        return result

    def run_edit(self, checkpoint: JsonObject, index: int) -> JsonObject:
        edit = checkpoint["edits"][index]
        results: list[JsonObject] = checkpoint["results"]
        self.keep_token_fresh()
        if index == len(results):
            live = self.state()
            if differences(graph_view(live), graph_view(checkpoint["last_state"])):
                raise OperationFailed("the live graph changed outside the battery; start a new --attempt")
            already = preconditions(edit, graph_view(live), graph_view(checkpoint["baseline"]), self.read_text)
            if already:
                return self.not_sent(checkpoint, edit, live, already)
            results.append({"number": edit["number"], "id": edit["id"], "operation_id": str(uuid4()), "base_state_id": live["id"]})
            self.store.write(self.name, checkpoint)
        result = results[index]
        if "submitted_at" not in result:
            submitted_at = self.now()
            accepted = self.send(result["operation_id"], result["base_state_id"], edit["message"])
            result["submitted_at"] = submitted_at
            result["poll_after_ms"] = accepted["poll_after_ms"] if "poll_after_ms" in accepted else None
            self.store.write(self.name, checkpoint)
            emit("edit_submitted", number=edit["number"], id=edit["id"], operation_id=result["operation_id"])
        operation, polls = self.wait_for(result["operation_id"], result["poll_after_ms"])
        observed_at = self.now()
        before_state = checkpoint["last_state"]
        rows, rows_seen, evidence_reads, settled = self.settled_rows(checkpoint["rows_seen"], result["operation_id"])
        after_state = self.state()
        evidence = turn_evidence(rows, result["operation_id"], shed_before=checkpoint["shed_history"])
        completed = operation["status"] == "completed" and operation["result"] is not None
        failures, unrelated, server_written = self.judge(edit, checkpoint, before_state, after_state)
        validation = new_validation_errors(before_state, after_state)
        tools = tool_findings(edit, evidence, self.plan.forbidden_tools)
        findings: list[str] = []
        if not completed:
            error = operation["error"] if "error" in operation else None
            findings.append(
                "turn_not_completed:"
                + str(operation["status"])
                + (":" + str(error["code"]) if type(error) is dict and "code" in error else "")
            )
        if not settled:
            findings.append(f"evidence_not_settled_after_{evidence_reads}_reads")
        if evidence["successful_provider_calls"] == 0:
            findings.append("no_successful_provider_call")
        findings.extend(failures)
        findings.extend("unrelated_change:" + path for path in unrelated)
        findings.extend("new_validation_error:" + key for key in validation)
        findings.extend(tools)
        if "approvals" not in result:
            result["approvals"] = []
        warnings = tool_warnings(evidence)
        if edit["approve"] is not None and not findings:
            # Only a turn with a clean verdict earns an approval; the approval may change server-written fields only.
            self.approve_reviews(edit, after_state, checkpoint, result["approvals"])
            if not result["approvals"]:
                warnings.append("no_pending_review_card_to_approve")
            approved_state = self.state()
            approval = classify_changes(graph_view(after_state), graph_view(approved_state), [])
            findings.extend("approval_changed:" + path for path in approval.unrelated)
            server_written = [*server_written, *approval.server_written]
            after_state = approved_state
            failures, _, _ = self.judge(edit, checkpoint, before_state, after_state)
            findings.extend("after_approval:" + failure for failure in failures)
        wall = round((observed_at - result["submitted_at"]) * 1000)
        result.update(
            status=operation["status"],
            passed=not findings,
            findings=findings,
            warnings=warnings,
            server_written=server_written,
            evidence=evidence,
            history_shed=shed_this_turn(evidence),
            timing={
                "wall_ms": wall,
                "polls": polls,
                "evidence_reads": evidence_reads,
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
            **{
                key: result[key]
                for key in ("number", "id", "status", "passed", "findings", "warnings", "server_written", "history_shed", "timing")
            },
            tools=evidence["tool_calls_by_name"],
            provider_calls=evidence["provider_calls"],
        )
        return result

    def run(self) -> JsonObject:
        sign_in(self.client, self.auth, prompt_secret=self.prompt_secret)
        try:
            checkpoint = self.start()
            for index, edit in enumerate(checkpoint["edits"]):
                if "stopped_reason" in checkpoint:
                    break
                if index < len(checkpoint["results"]) and (
                    "passed" in checkpoint["results"][index] or "skipped" in checkpoint["results"][index]
                ):
                    continue
                if not edit["applicable"]:
                    checkpoint["results"].append(
                        {"number": edit["number"], "id": edit["id"], "skipped": "not_applicable", "missing_roles": edit["missing_roles"]}
                    )
                    self.store.write(self.name, checkpoint)
                    emit("edit_skipped", number=edit["number"], id=edit["id"], reason="not_applicable", missing_roles=edit["missing_roles"])
                    continue
                self.run_edit(checkpoint, index)
            if "stopped_reason" not in checkpoint and "as_found_differences" not in checkpoint:
                # A plan leaves the graph as it found it; any difference left is reported, not judged.
                checkpoint["as_found_differences"] = as_found_differences(checkpoint["baseline"], self.state(), self.read_text)
                self.store.write(self.name, checkpoint)
        finally:
            self.client.close()
        summary = summarise(checkpoint)
        self.store.write(f"{self.name}-summary", summary)
        emit("edit_battery_summary", **summary)
        return summary


# ── cleanup ────────────────────────────────────────────────────────────────


def cleanup_messages(findings: Sequence[str]) -> list[str]:
    """Ordinary chat requests that would remove the leftovers ``findings`` names; a pipeline name is only reported."""
    steps = [path.split("/", 2)[2] for path in findings if path.startswith("/nodes/") and path.count("/") == 2]
    outputs = [path.split("/", 2)[2] for path in findings if path.startswith("/outputs/")]
    prompts = sorted({path.split("/")[2] for path in findings if path.startswith("/nodes/") and path.count("/") == 4})
    messages: list[str] = []
    if outputs:
        messages.append(
            f"Remove the output{'s' if len(outputs) > 1 else ''} {', '.join(outputs)}; anything sent there "
            "should be discarded instead. Change nothing else."
        )
    if steps:
        messages.append(
            f"Remove the step{'s' if len(steps) > 1 else ''} {', '.join(steps)} from the pipeline, connecting what fed "
            "each one directly to what it fed, so the flow is as it was before they were added. Change nothing else."
        )
    for node_id in prompts:
        kinds = " or ".join(f'"{marker}"' for marker in MARKER_TEXTS)
        messages.append(
            f"In the prompts of the step {node_id}, remove every sentence that starts with {kinds}, "
            "keeping the rest of each prompt exactly as it is. Change nothing else."
        )
    return messages


def cleanup(battery: EditBattery, *, execute: bool) -> int:
    """Report the graph's battery leftovers and, with ``execute``, ask the Composer to remove them."""
    sign_in(battery.client, battery.auth, prompt_secret=battery.prompt_secret)
    try:
        state = battery.state()
        found = leftover_findings(state, battery.read_text)
        emit("battery_leftovers", found=found, prefix=TEMPORARY_PREFIX)
        if not found:
            return 0
        messages = cleanup_messages(found)
        if not execute:
            emit("dry_run", would_send=messages, note="nothing was sent; pass --execute to ask the Composer to remove them")
            return 1
        battery.require_auto_commit()
        for message in messages:
            operation_id = str(uuid4())
            accepted = battery.send(operation_id, battery.state()["id"], message)
            operation, _ = battery.wait_for(operation_id, accepted["poll_after_ms"] if "poll_after_ms" in accepted else None)
            emit("cleanup_turn", operation_id=operation_id, status=operation["status"], message=message)
        remaining = leftover_findings(battery.state(), battery.read_text)
        emit("battery_leftovers_remaining", found=remaining)
        return 1 if remaining else 0
    finally:
        battery.client.close()


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
            "Ends with 'longest run in a row: K of N'; gold is a complete attempt with every applicable edit passed "
            f"(at least {GOLD_MINIMUM_EDITS}). The session must be in auto_commit trust mode: in explicit_approve each "
            "edit waits as a proposal, which the battery does not accept for the user, so it refuses to start. "
            "Real provider calls: dry run unless --execute."
        ),
    )
    parser.add_argument(
        "action",
        choices=["run", "plan", "report", "cleanup"],
        help=(
            "run the battery; plan: validate and list the plan with its tool coverage; report: summarise a saved attempt "
            "(no network); cleanup: report battery_* leftovers on the graph and, with --execute, ask the Composer to "
            "remove them in ordinary chat turns"
        ),
    )
    parser.add_argument(
        "--base-url", help="server origin: https://..., or http://localhost:PORT for a local server (env ELSPETH_OPS_BASE_URL)"
    )
    parser.add_argument("--session", help="the session holding the graph to edit (env ELSPETH_OPS_SESSION)")
    add_account_arguments(parser, prefix="", role="session owner")
    parser.add_argument("--state-dir", help="private checkpoint directory outside the repository (env ELSPETH_OPS_STATE_DIR)")
    parser.add_argument(
        "--plan",
        type=Path,
        default=DEFAULT_PLAN,
        help="edit plan JSON (default: the shipped 50-edit plan for graphs whose prompts are all in files)",
    )
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        metavar="NAME=ID",
        help="name the graph component (or, for an operator role, the value) for a plan target",
    )
    parser.add_argument("--attempt", default="1", help="attempt label; a stopped attempt never continues, start a new one after diagnosis")
    parser.add_argument("--keep-going", action="store_true", help="judge every edit instead of stopping at the first failure")
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=2.0,
        help="minimum seconds between operation polls and evidence reads (the server's poll_after_ms is honoured when longer)",
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
        emit(
            "edit_plan",
            name=plan.name,
            sha256=plan.sha256,
            edits=len(plan.edits),
            targets=dict(plan.targets),
            expected_tools=plan.expected_tools(),
            forbidden_tools=list(plan.forbidden_tools),
            tool_coverage=plan_coverage(plan),
        )
        for number, edit in enumerate(plan.edits, start=1):
            emit("planned_edit", number=number, id=edit["id"], message=edit["message"], roles=sorted(plan.roles_of(edit)))
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
            if "passed" in result and "evidence" in result:
                emit(
                    "edit_result",
                    **{key: result[key] for key in ("number", "id", "status", "passed", "findings", "warnings", "history_shed", "timing")},
                )
            elif "passed" in result:
                emit("edit_result", **{key: result[key] for key in ("number", "id", "status", "passed", "findings")})
        emit("edit_battery_summary", **summarise(saved))
        return 0
    base_url = validate_base_url(setting(args.base_url, flag="--base-url", env="ELSPETH_OPS_BASE_URL"))
    auth = account_from(args, prefix="", env="ELSPETH_OPS_ACCOUNT")
    if args.action == "run" and not args.execute:
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
    if args.action == "cleanup":
        return cleanup(battery, execute=args.execute)
    return 0 if battery.run()["gold"] else 1


if __name__ == "__main__":
    raise SystemExit(run_tool(main))
