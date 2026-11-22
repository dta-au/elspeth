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
  allowed by name, only on a component the edit names or the node whose card
  it approves, and listed as server-written); no new validation
  error; one of the edit's acceptable tools was used; no forbidden tool was
  called without being rejected.
* A tool call the server rejected inside a passing edit is a warning, counted
  in the summary as "tool calls rejected then corrected"; it does not fail the
  edit.
* History shedding, wall time, server-recorded span, provider time, the server
  remainder and provider-call counts (see :mod:`scripts.staging.turn_evidence`).

Gold is a COMPLETE attempt: every applicable plan edit judged and passed, in
one attempt, with at least ``GOLD_MINIMUM_EDITS`` applicable edits, leaving
no difference from the graph it started on. Files the attempt added to the
session are listed by name ("files left in the session"). An edit
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
  battery's pipeline name). ``cleanup --execute`` asks the Composer to remove
  them in ordinary chat turns, each judged like a battery edit; without
  ``--execute`` it makes no network call.
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
import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from scripts.staging import structural_cycle
from scripts.staging.edit_plan import (
    DEFAULT_PLAN,
    MARKER_TEXTS,
    TEMPORARY_PREFIX,
    FileContext,
    Plan,
    as_found_differences,
    attachment_mime_type,
    card_kind,
    check_expectations,
    classify_changes,
    empty_state,
    escape,
    expected_new_error,
    graph_view,
    is_empty_graph,
    leftover_findings,
    load_plan,
    named_components,
    new_component_notes,
    new_validation_errors,
    own_description_corrections,
    preconditions,
    render_was,
    resolve_edits,
    resolve_targets,
    text_at,
    tool_findings,
    tool_warnings,
    uses_files,
)
from scripts.staging.fixture_setup import setup_fixture
from scripts.staging.large_graph_fixture import load_fixture
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
    list_pages,
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
from scripts.staging.structural_cycle import Position
from scripts.staging.turn_evidence import UNAVAILABLE_FROM_API, operation_rows, shed_state, shed_this_turn, turn_evidence

LIVE_STATUSES = ("queued", "running")
CHECKPOINT_VERSION = 3
GOLD_MINIMUM_EDITS = 50
AUTO_COMMIT = "auto_commit"
# Registry mutation-kind tools that are not graph edits, each with its reason.
# A test fails on any mutation tool that is neither here nor in EDITING_TOOLS.
NOT_EDITING_MUTATION_TOOLS = {
    "report_composer_bug": "records a report file; does not edit the pipeline",
}
# The Composer's graph-editing tools (registry kinds MUTATION, BLOB_MUTATION and
# SECRET_MUTATION, less NOT_EDITING_MUTATION_TOOLS); a test pins this list to the registry.
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
        "baseline_files",
        "last_state",
        "edits",
        "results",
        "rows_seen",
        "shed_history",
    }
)
_MESSAGE_PAGE = 500
# Set by a server whose audit-grade messages view returns END review rows: the
# count of stored END review rows it left out because they did not parse.
END_REVIEW_VIEW_HEADER = "X-Elspeth-End-Review-Rows-Omitted"
type EndReviewMode = Literal["auto", "required", "if-recorded"]
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
        and type(saved["baseline_files"]) is list
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


def _evidence_total(results: Sequence[Mapping[str, Any]], key: str) -> int | None:
    values = [result["evidence"][key] for result in results]
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


REVIEW_PENDING_CODE = "interpretation_review_pending"


def _profile_owned(edit: Mapping[str, Any]) -> JsonObject:
    """``{"profile_owned": [...]}`` for an edit skipped because a profile owns its option, else ``{}``."""
    return {"profile_owned": edit["profile_owned"]} if "profile_owned" in edit else {}


def _is_the_approved_review(edit: Mapping[str, Any], key: str) -> bool:
    """Whether a new validation error (``new_validation_errors`` key) is the pending card this edit approves.

    Exactly that node and kind: a pending review on any other node or of another kind is not covered.
    """
    if edit["approve"] is None:
        return False
    code, component, message = json.loads(key)
    return (
        code == REVIEW_PENDING_CODE
        and component == edit["approve"]["node"]
        and type(message) is str
        and message.startswith(edit["approve"]["kind"] + " review pending")
    )


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
    corrected = sum(
        int(warning.rsplit(":", 1)[1])
        for result in judged
        if result["passed"]
        for warning in result["warnings"]
        if warning.startswith("tool_call_")
    )
    as_found = checkpoint["as_found_differences"] if "as_found_differences" in checkpoint else None
    all_passed = complete and passed == len(applicable)
    gold = complete and passed == len(applicable) and len(applicable) >= GOLD_MINIMUM_EDITS and as_found == []
    return {
        "plan": checkpoint["plan_name"],
        "edits": len(checkpoint["edits"]),
        "applicable": len(applicable),
        "skipped_not_applicable": [
            {"number": edit["number"], "id": edit["id"], "missing_roles": edit["missing_roles"], **_profile_owned(edit)} for edit in skipped
        ],
        "judged": len(judged),
        "complete": complete,
        "passed": passed,
        "longest_run": longest,
        "gold": gold,
        "all_passed": all_passed,
        # The pass rule of a plan with pass_rule all_passed_as_found (the file-binding plan): every
        # applicable edit judged and passed in one attempt, and the graph left as found.
        "all_passed_as_found": all_passed and as_found == [],
        "gold_requires": (
            f"every applicable edit judged and passed in one attempt, at least {GOLD_MINIMUM_EDITS} applicable, "
            "and the graph left as it was found"
        ),
        "line": f"longest run in a row: {longest} of {len(applicable)}" + ("" if complete else f" (incomplete: {len(judged)} judged)"),
        "first_failure": first_failure,
        "stopped_reason": checkpoint["stopped_reason"] if "stopped_reason" in checkpoint else None,
        "as_found_differences": as_found,
        "files_left_in_session": checkpoint["files_left_in_session"] if "files_left_in_session" in checkpoint else None,
        "tool_calls_rejected_then_corrected": corrected,
        "server_written_changes": sum(len(result["server_written"]) for result in sent),
        "tool_coverage": tool_coverage(_used_tools(sent)),
        "totals": {
            "wall_ms": wall,
            "span_ms": span,
            "provider_busy_ms": busy,
            "provider_latency_ms_sum": _total(sent, "provider_latency_ms_sum"),
            "server_ms": server,
            "tool_busy_ms": _evidence_total(sent, "tool_busy_ms"),
            "server_ms_outside_provider_and_tools": _evidence_total(sent, "server_ms_outside_provider_and_tools"),
            "provider_calls": sum(result["evidence"]["provider_calls"] for result in sent),
            "tool_calls": sum(tools.values()),
            "tool_calls_by_name": dict(sorted(tools.items())),
            "polls": sum(result["timing"]["polls"] for result in sent),
        },
        "server_time_share_of_span": round(server / span, 4) if server is not None and span else None,
        "non_provider_share_of_wall": round((wall - busy) / wall, 4) if wall is not None and busy is not None and wall else None,
        "history_shed": {"edits": len(shed), "passed": sum(1 for result in shed if result["passed"])},
        "edits_with_complete_tool_timings": sum(1 for result in sent if result["evidence"]["tool_timings_complete"]),
        "end_reviews_recorded": sum(len(result["evidence"]["end_reviews"]) for result in sent),
        "end_reviews": end_review_summary(sent),
        "baseline_graph_counts": checkpoint["baseline_graph_counts"] if "baseline_graph_counts" in checkpoint else None,
        "not_available_from_api": list(UNAVAILABLE_FROM_API),
    }


def end_review_summary(sent: Sequence[Mapping[str, Any]]) -> JsonObject:
    """How many sent steps had an end review the server returned, and whether those were clean and admitted.

    A run with no end review in its evidence says so in words: it never reads
    as clean end reviews when none were seen. A server that has the END review
    view (it sets :data:`END_REVIEW_VIEW_HEADER` on the audit-grade messages
    view) is told apart from one that has none.
    """
    reviewed = [result["evidence"]["end_reviews"][-1] for result in sent if result["evidence"]["end_reviews"]]
    clean = sum(1 for review in reviewed if review["verdict"] == "clean" and review["publication_verdict"] == "ADMIT")
    unseen = len(sent) - len(reviewed)
    views = [result["end_review_view"] for result in sent if "end_review_view" in result]
    exposed = any(view["exposed"] for view in views)
    omitted = max((view["rows_omitted"] for view in views), default=0)
    if not reviewed and not exposed:
        line = "end reviews: none exposed by this server; not verified"
    else:
        line = f"end reviews: {len(reviewed)} of {len(sent)} steps recorded one, {clean} clean and admitted"
        if unseen:
            line += f"; {unseen} steps not verified"
        if omitted:
            line += f"; the server left out {omitted} end review rows it could not read"
    return {
        "line": line,
        "server_exposes_end_reviews": exposed,
        "rows_omitted_by_server": omitted,
        "steps_sent": len(sent),
        "steps_with_end_review": len(reviewed),
        "clean_and_admitted": clean,
        "steps_not_verified": unseen,
        "verified": bool(sent) and unseen == 0,
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
    end_review_evidence: EndReviewMode = "auto"
    # From the last read of the audit-grade messages view: whether the server
    # set END_REVIEW_VIEW_HEADER, and the count it carried.
    server_end_review_view: bool = field(default=False, init=False)
    end_review_rows_omitted: int = field(default=0, init=False)
    # The files this attempt attached, by name: the checkpoint's ``uploads`` (file-binding plans).
    uploads: dict[str, str] = field(default_factory=dict, init=False)

    def resolved_end_review_evidence(self) -> structural_cycle.EndReviewEvidence:
        """``auto`` is ``required`` on a server that has the END review view and ``if-recorded`` on one without."""
        if self.end_review_evidence == "auto":
            return "required" if self.server_end_review_view else "if-recorded"
        return self.end_review_evidence

    @property
    def name(self) -> str:
        return checkpoint_name(self.attempt, self.session_id)

    def keep_token_fresh(self) -> None:
        if token_expiry(self.client.token) < self.now() + self.refresh_margin_seconds:
            self.client.refresh()

    def state(self) -> JsonObject:
        """The session's graph; a session with no graph yet answers null, which stands for the empty pipeline."""
        raw = self.client.get_json(f"/api/sessions/{self.session_id}/state")
        if raw is None:
            return empty_state()
        return require_dict(raw, "state")

    def file_context(self) -> FileContext:
        return FileContext(uploads=dict(self.uploads), listing={item["id"]: item for item in self.session_files()})

    def attach(self, checkpoint: JsonObject, edit: Mapping[str, Any]) -> None:
        """Upload the edit's attachments, as the user would, before its message; each file id is saved before the message is sent.

        A resumed run reuses a recorded upload, and also an upload it made but had not yet recorded
        (a file of that name and content uploaded by the user since the attempt began), so no file is
        uploaded twice.
        """
        if "attach" not in edit:
            return
        uploads: dict[str, str] = checkpoint["uploads"]
        pending = [name for name in edit["attach"] if name not in uploads]
        if not pending:
            return
        listing = self.session_files()
        for name in pending:
            data = self.plan.attachments[name]
            digest = hashlib.sha256(data).hexdigest()
            blob_id = next(
                (
                    item["id"]
                    for item in listing
                    if item["filename"] == name
                    and item["content_hash"] == digest
                    and item["created_by"] == "user"
                    and item["id"] not in checkpoint["baseline_files"]
                ),
                None,
            )
            if blob_id is None:
                uploaded = require_dict(
                    self.client.upload_file(
                        f"/api/sessions/{self.session_id}/blobs", filename=name, mime_type=attachment_mime_type(name), data=data
                    ),
                    "uploaded file",
                )
                blob_id = str(uploaded["id"])
            uploads[name] = blob_id
            self.uploads[name] = blob_id
            self.store.write(self.name, checkpoint)
            emit("file_attached", number=edit["number"], id=edit["id"], filename=name, file_id=blob_id, bytes=len(data))

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
            path = (
                f"/api/sessions/{self.session_id}/messages?include_llm_audit=true&include_tool_rows=true"
                f"&limit={_MESSAGE_PAGE}&offset={offset + len(rows)}"
            )
            response = self.client.request("GET", path)
            self.note_end_review_view(response.headers)
            try:
                body = json.loads(response.body)
            except ValueError:
                raise OperationFailed("the messages view did not return JSON") from None
            page = require_list(body, "messages")
            rows.extend(require_dict(row, "message") for row in page)
            if len(page) < _MESSAGE_PAGE:
                return rows

    def note_end_review_view(self, headers: Mapping[str, str]) -> None:
        """Record whether the server has the END review view, from the header it sets on the view."""
        values = [value for name, value in headers.items() if name.lower() == END_REVIEW_VIEW_HEADER.lower()]
        if not values:
            self.server_end_review_view = False
            self.end_review_rows_omitted = 0
            return
        if len(values) != 1 or not values[0].isdigit():
            raise OperationFailed(f"the messages view sent {END_REVIEW_VIEW_HEADER} as {values!r}, not one count")
        self.server_end_review_view = True
        self.end_review_rows_omitted = int(values[0])

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
            self.uploads = dict(checkpoint["uploads"]) if "uploads" in checkpoint else {}
            return checkpoint
        baseline = self.state()
        leftovers = leftover_findings(baseline, self.read_text)
        if leftovers:
            raise OperationFailed(
                "this graph holds leftovers of an earlier battery attempt: "
                + ", ".join(leftovers)
                + ". Run the cleanup action to report or remove them, then start again."
            )
        if self.plan.start == "empty" and not is_empty_graph(baseline):
            raise OperationFailed(
                f"plan {self.plan.name} starts from an empty pipeline and this session's graph is not empty; create a new session for it"
            )
        files_before = self.session_files() if self.plan.attachments else None
        clashing = sorted({item["filename"] for item in files_before or []} & set(self.plan.attachments))
        if clashing:
            raise OperationFailed(
                "this session already holds files named as the plan's attachments ("
                + ", ".join(clashing)
                + "), so a message naming one would be ambiguous; create a new session for the plan"
            )
        counts: JsonObject | None = None
        if self.plan.verifier == structural_cycle.VERIFIER:
            args = self.plan.verifier_args
            counts = structural_cycle.check_fixture(baseline, positions=[Position.from_json(entry) for entry in args["positions"]])
        targets = resolve_targets(self.plan, baseline, self.overrides)
        rows = self.rows_from(0)
        baseline_files = sorted(item["id"] for item in (files_before if files_before is not None else self.session_files()))
        edits = resolve_edits(self.plan, targets, baseline)
        checkpoint = {
            "version": CHECKPOINT_VERSION,
            "session_id": self.session_id,
            "attempt": self.attempt,
            "plan_name": self.plan.name,
            "plan_sha256": self.plan.sha256,
            "targets": {"resolved": targets, "overrides": dict(self.overrides)},
            "baseline": baseline,
            "baseline_files": baseline_files,
            "last_state": baseline,
            "edits": edits,
            "results": [],
            "rows_seen": len(rows),
            "shed_history": shed_state(rows),
        }
        if counts is not None:
            checkpoint["baseline_graph_counts"] = counts
        if self.plan.attachments:
            checkpoint["uploads"] = {}
            self.uploads = {}
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
        self, edit: Mapping[str, Any], state: Mapping[str, Any], approved: list[JsonObject], save: Callable[[], None]
    ) -> list[str]:
        """Resolve the edit's own prompt review cards, each checked against the node's current prompt; return card findings.

        Each approval is saved (``save``) as soon as it is made, so a resumed run counts it.
        When the edit names a ``card`` kind, a card of another kind is a finding and is not
        approved (:func:`~scripts.staging.edit_plan.card_kind`).
        """
        findings: list[str] = []
        approve = edit["approve"]
        nodes = {node["id"]: node for node in state["nodes"]}
        for event in self.pending_reviews():
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
            if "card" in approve and card_kind(snapshot) != approve["card"]:
                findings.append(f"review_card_kind:{approve['node']}:expected_{approve['card']}:shown_{card_kind(snapshot)}")
                continue
            self.client.post_json(
                f"/api/sessions/{self.session_id}/interpretations/{event['id']}/resolve",
                {"choice": "accepted_as_drafted", "reviewed_anchor": snapshot["anchor"]},
            )
            approved.append({"event_id": event["id"], "kind": event["kind"], "node_id": approve["node"]})
            save()
        return findings

    def pending_reviews(self) -> list[JsonObject]:
        events = require_dict(self.client.get_json(f"/api/sessions/{self.session_id}/interpretations?status=pending"), "reviews")["events"]
        return [require_dict(event, "review") for event in require_list(events, "reviews")]

    def session_files(self) -> list[JsonObject]:
        return [require_dict(item, "file") for item in list_pages(self.client, f"/api/sessions/{self.session_id}/blobs")]

    def judge(
        self, edit: Mapping[str, Any], baseline_state: Mapping[str, Any], before_state: Mapping[str, Any], after_state: Mapping[str, Any]
    ) -> tuple[list[str], list[str], list[str]]:
        """``(expectation failures, unrelated changes, server-written changes)``.

        Server-written review fields count only on the components the edit names
        and the node whose card it approves.
        """
        before, after = graph_view(before_state), graph_view(after_state)
        files = self.file_context() if uses_files(edit) else None
        failures, covered = check_expectations(edit, before, after, graph_view(baseline_state), self.read_text, files=files)
        named = named_components(edit) | ({edit["approve"]["node"]} if edit["approve"] is not None else set())
        changes = classify_changes(before, after, [*covered, *edit["may_change"]], named=named)
        # A target's own description rewritten to stay true is reported as a warning (verdict), not failed.
        corrected = {note.split(":", 1)[1] for note in own_description_corrections(edit, before, after)}
        return failures, [path for path in changes.unrelated if path not in corrected], changes.server_written

    def verdict(
        self,
        edit: Mapping[str, Any],
        before_state: Mapping[str, Any],
        operation: Mapping[str, Any],
        operation_id: str,
        *,
        baseline_state: Mapping[str, Any],
        rows_offset: int,
        shed_before: Sequence[int] | None,
        approved: list[JsonObject],
        save: Callable[[], None],
    ) -> tuple[JsonObject, JsonObject, int]:
        """Judge one finished turn: ``(verdict fields, the state it left, rows read so far)``.

        Battery edits and cleanup turns are judged by this same rule.
        """
        rows, rows_seen, evidence_reads, settled = self.settled_rows(rows_offset, operation_id)
        after_state = self.state()
        evidence = turn_evidence(rows, operation_id, shed_before=shed_before)
        findings: list[str] = []
        if not settled:
            findings.append(f"evidence_not_settled_after_{evidence_reads}_reads")
        if evidence["successful_provider_calls"] == 0:
            findings.append("no_successful_provider_call")
        warnings = tool_warnings(evidence)
        extra: JsonObject = {}
        if self.plan.verifier == structural_cycle.VERIFIER:
            # The workload's own verifier judges the turn, with the reference codes, in place of the plan expectations.
            state_codes = structural_cycle.verify_state(baseline_state, after_state, edit["order"], Position.from_json(edit["position"]))
            turn_codes, review_warnings = structural_cycle.operation_codes(
                operation, evidence["end_reviews"], end_review_evidence=self.resolved_end_review_evidence()
            )
            findings.extend(sorted({*state_codes, *turn_codes}))
            warnings.extend(review_warnings)
            server_written: list[str] = []
            extra["graph_counts"] = structural_cycle.graph_counts(after_state)
        else:
            # The pending-review error for the card this edit approves is judged after the approval, not before it.
            deferred: list[str] = []
            server_written = self.judge_plan_edit(
                edit, operation, evidence, findings, before_state, after_state, baseline_state, deferred=deferred
            )
            warnings.extend(new_component_notes(edit, graph_view(before_state), graph_view(after_state)))
            warnings.extend(own_description_corrections(edit, graph_view(before_state), graph_view(after_state)))
            if edit["approve"] is not None and not findings:
                # Only a turn with a clean verdict earns an approval; the approval may change server-written fields only.
                findings.extend(self.approve_reviews(edit, after_state, approved, save))
                if not approved and "card" in edit["approve"]:
                    # An edit that names the card kind requires the card: binding a prompt file always opens one.
                    findings.append(f"expected_review_card_missing:{edit['approve']['node']}")
                elif not approved:
                    warnings.append("no_pending_review_card_to_approve")
                approved_state = self.state()
                approval = classify_changes(graph_view(after_state), graph_view(approved_state), [], named={edit["approve"]["node"]})
                findings.extend("approval_changed:" + path for path in approval.unrelated)
                server_written = [*server_written, *approval.server_written]
                after_state = approved_state
                failures, _, _ = self.judge(edit, baseline_state, before_state, after_state)
                findings.extend("after_approval:" + failure for failure in failures)
                # Every validation error new since the turn began, the deferred one included, fails the edit now.
                findings.extend(
                    "after_approval:new_validation_error:" + key
                    for key in new_validation_errors(before_state, after_state)
                    if not expected_new_error(edit, key)
                )
            else:
                findings.extend(deferred)
        fields: JsonObject = {
            "status": operation["status"],
            "passed": not findings,
            "findings": findings,
            "warnings": warnings,
            "server_written": server_written,
            "evidence": evidence,
            "history_shed": shed_this_turn(evidence),
            "evidence_reads": evidence_reads,
            "end_review_view": {"exposed": self.server_end_review_view, "rows_omitted": self.end_review_rows_omitted},
            **extra,
        }
        return fields, after_state, rows_seen

    def judge_plan_edit(
        self,
        edit: Mapping[str, Any],
        operation: Mapping[str, Any],
        evidence: Mapping[str, Any],
        findings: list[str],
        before_state: Mapping[str, Any],
        after_state: Mapping[str, Any],
        baseline_state: Mapping[str, Any],
        *,
        deferred: list[str] | None = None,
    ) -> list[str]:
        """Add a plan edit's findings (turn, expectations, unrelated changes, validation, tools, files); return server-written paths.

        With ``deferred`` and an edit that approves a card, a new ``interpretation_review_pending``
        error on exactly the node and kind it approves goes to ``deferred`` instead of
        ``findings``: the caller judges it after the approval (or adds it back if none happens).
        """
        if operation["status"] != "completed" or operation["result"] is None:
            error = operation["error"] if "error" in operation else None
            findings.insert(
                0,
                "turn_not_completed:"
                + str(operation["status"])
                + (":" + str(error["code"]) if type(error) is dict and "code" in error else ""),
            )
        failures, unrelated, server_written = self.judge(edit, baseline_state, before_state, after_state)
        findings.extend(failures)
        findings.extend("unrelated_change:" + path for path in unrelated)
        for key in new_validation_errors(before_state, after_state):
            if expected_new_error(edit, key):
                # A code the edit lists in new_errors: the change it asks for brings this error (an emptied pipeline).
                continue
            if deferred is not None and _is_the_approved_review(edit, key):
                deferred.append("new_validation_error:" + key)
            else:
                findings.append("new_validation_error:" + key)
        findings.extend(tool_findings(edit, evidence, self.plan.forbidden_tools))
        if "delete_files" in edit:
            present = {item["id"] for item in self.session_files()}
            findings.extend(f"file_not_deleted:{blob_id}" for blob_id in edit["delete_files"] if blob_id in present)
        return server_written

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
            self.attach(checkpoint, edit)
            already = preconditions(
                edit,
                graph_view(live),
                graph_view(checkpoint["baseline"]),
                self.read_text,
                files=self.file_context() if uses_files(edit) else None,
            )
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
        if "approvals" not in result:
            result["approvals"] = []
        fields, after_state, rows_seen = self.verdict(
            edit,
            checkpoint["last_state"],
            operation,
            result["operation_id"],
            baseline_state=checkpoint["baseline"],
            rows_offset=checkpoint["rows_seen"],
            shed_before=checkpoint["shed_history"],
            approved=result["approvals"],
            save=lambda: self.store.write(self.name, checkpoint),
        )
        evidence = fields["evidence"]
        wall = round((observed_at - result["submitted_at"]) * 1000)
        result.update(
            status=fields["status"],
            passed=fields["passed"],
            findings=fields["findings"],
            warnings=fields["warnings"],
            server_written=fields["server_written"],
            evidence=evidence,
            history_shed=fields["history_shed"],
            timing={
                "wall_ms": wall,
                "polls": polls,
                "evidence_reads": fields["evidence_reads"],
                "poll_interval_s": self.poll_seconds,
                "span_ms": evidence["span_ms"],
                "provider_busy_ms": evidence["provider_busy_ms"],
                "provider_latency_ms_sum": evidence["provider_latency_ms_sum"],
                "server_ms": evidence["server_ms"],
                "tool_busy_ms": evidence["tool_busy_ms"],
                "server_ms_outside_provider_and_tools": evidence["server_ms_outside_provider_and_tools"],
                "non_provider_wall_ms": wall - evidence["provider_busy_ms"] if evidence["provider_busy_ms"] is not None else None,
            },
            state_id_after=after_state["id"],
            end_review_view=fields["end_review_view"],
        )
        if "graph_counts" in fields:
            result["graph_counts"] = fields["graph_counts"]
        checkpoint["last_state"] = after_state
        checkpoint["rows_seen"] = rows_seen
        if evidence["history"]["shed_history_after"] is not None:
            checkpoint["shed_history"] = evidence["history"]["shed_history_after"]
        if result["findings"] and not self.keep_going:
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
            **({"graph_counts": result["graph_counts"]} if "graph_counts" in result else {}),
        )
        return result

    def files_left(self, baseline_files: Sequence[str], state: Mapping[str, Any]) -> list[JsonObject]:
        """Files in the session that were not there when the attempt started, and whether the graph still uses each."""
        referenced = referenced_files(state)
        return [
            {"id": item["id"], "filename": item["filename"], "referenced": item["id"] in referenced}
            for item in self.session_files()
            if item["id"] not in baseline_files
        ]

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
                    skip = {"missing_roles": edit["missing_roles"], **_profile_owned(edit)}
                    checkpoint["results"].append({"number": edit["number"], "id": edit["id"], "skipped": "not_applicable", **skip})
                    self.store.write(self.name, checkpoint)
                    emit("edit_skipped", number=edit["number"], id=edit["id"], reason="not_applicable", **skip)
                    continue
                self.run_edit(checkpoint, index)
            if "stopped_reason" not in checkpoint and "as_found_differences" not in checkpoint:
                # A plan leaves the graph as it found it; gold needs no difference left.
                final = self.state()
                checkpoint["as_found_differences"] = as_found_differences(checkpoint["baseline"], final, self.read_text)
                checkpoint["files_left_in_session"] = self.files_left(checkpoint["baseline_files"], final)
                self.store.write(self.name, checkpoint)
        finally:
            self.client.close()
        summary = summarise(checkpoint)
        self.store.write(f"{self.name}-summary", summary)
        emit("edit_battery_summary", **summary)
        return summary


def referenced_files(state: Mapping[str, Any]) -> set[str]:
    """Ids of every file a graph's sources, nodes or outputs point at: prompt file markers and source file bindings."""
    found: set[str] = set()

    def walk(value: object) -> None:
        if type(value) is dict:
            for key, child in value.items():
                if key in ("blob_ref", "blob_id") and type(child) is str:
                    found.add(child)
                else:
                    walk(child)
        elif type(value) is list:
            for child in value:
                walk(child)

    walk([state["sources"], state["nodes"], state["outputs"]])
    return found


# ── cleanup ────────────────────────────────────────────────────────────────

CLEANUP_TURN_LIMIT = 60
_MARKER_SENTENCE = re.compile("(?:" + "|".join(re.escape(marker) for marker in MARKER_TEXTS) + r")[^.]*\.")


def _cleanup_edit(number: int, edit_id: str, message: str, expect: list[JsonObject], tools: list[str], **extra: Any) -> JsonObject:
    edit: JsonObject = {
        "number": number,
        "id": edit_id,
        "applicable": True,
        "message": message + " Change nothing else.",
        "expect": expect,
        "may_change": [],
        "edges_touching": [],
        "tools_any": tools,
        "tools_all": [],
        "approve": None,
    }
    edit.update(extra)
    return edit


def _routes_into(view: Mapping[str, Any], name: str) -> list[tuple[str, str]]:
    """``(collection, id)`` of every source or node whose routing names ``name``."""
    found: list[tuple[str, str]] = [("sources", source) for source, spec in view["sources"].items() if spec["on_success"] == name]
    for node_id, node in view["nodes"].items():
        targets = [node["on_success"], node["on_error"]]
        if "routes" in node and type(node["routes"]) is dict:
            targets.extend(node["routes"].values())
        if "fork_to" in node and type(node["fork_to"]) is list:
            targets.extend(node["fork_to"])
        if name in targets:
            found.append(("nodes", node_id))
    return found


def next_cleanup_edit(
    state: Mapping[str, Any],
    read_text: Callable[[Mapping[str, Any]], str],
    files: Sequence[Mapping[str, Any]],
    baseline_state: Mapping[str, Any] | None,
    baseline_files: Sequence[str] | None,
    number: int,
) -> JsonObject | None:
    """The next cleanup turn for the graph as it is now, as a judgeable edit; ``None`` when nothing is left to ask for.

    Order: leftover outputs, leftover steps, battery marker sentences, the
    pipeline name (only when the attempt's starting graph is known), then
    leftover files the graph no longer uses.
    """
    view = graph_view(state)
    for name in view["outputs"]:
        if name.startswith(TEMPORARY_PREFIX):
            routers = _routes_into(view, name)
            return _cleanup_edit(
                number,
                f"remove-output-{name}",
                f"Remove the output {name}; anything sent there should be discarded instead.",
                [{"absent": f"/outputs/{escape(name)}"}],
                ["remove_output"],
                may_change=[
                    f"/{kind}/{escape(item)}/{field}" for kind, item in routers for field in ("on_success", "on_error", "routes", "fork_to")
                ],
                edges_touching=[name, *(item for _, item in routers)],
            )
    for node_id, node in view["nodes"].items():
        if node_id.startswith(TEMPORARY_PREFIX):
            feeders = [(kind, item) for kind, item in _routes_into(view, node["input"]) if item != node_id]
            consumers = [
                other for other, spec in view["nodes"].items() if node["on_success"] is not None and spec["input"] == node["on_success"]
            ]
            expect: list[JsonObject] = [{"absent": f"/nodes/{escape(node_id)}"}]
            may_change: list[str] = []
            if len(feeders) == 1 and len(consumers) == 1:
                expect.append({"chain": {"from": feeders[0][1], "to": consumers[0], "via": []}})
            else:
                may_change = [f"/{kind}/{escape(item)}/on_success" for kind, item in feeders]
                may_change += [f"/nodes/{escape(item)}/input" for item in consumers]
            return _cleanup_edit(
                number,
                f"remove-step-{node_id}",
                f"Remove the step {node_id} and connect what fed it directly to what it fed, so the flow is as it was before it was added.",
                expect,
                ["remove_node"],
                may_change=may_change,
                edges_touching=[node_id, *(item for _, item in feeders), *consumers],
            )
    for node_id in view["nodes"]:
        for key, label in (("prompt_template", "prompt"), ("system_prompt", "system prompt")):
            pointer = f"/nodes/{escape(node_id)}/options/{key}"
            _, text = text_at(view, pointer, read_text)
            if text is None:
                continue
            for sentence in _MARKER_SENTENCE.findall(text):
                if text.count(sentence) == 1:
                    return _cleanup_edit(
                        number,
                        f"remove-marker-{node_id}-{key}",
                        f'Remove the sentence "{sentence}" from the {label} of the step {node_id}, keeping the rest exactly as it is.',
                        [{"text": pointer, "remove": sentence}],
                        ["change_substring", "patch_node_options", "wire_blob_inline_ref"],
                        approve={"kind": "llm_prompt_template", "node": node_id},
                    )
    name = view["metadata"]["name"] if "name" in view["metadata"] else None
    if baseline_state is not None and type(name) is str and name.startswith("Battery check"):
        return _cleanup_edit(
            number,
            "restore-pipeline-name",
            f"Put the pipeline name back to {render_was(graph_view(baseline_state), '/metadata/name')}.",
            [{"restored": "/metadata/name"}],
            ["set_metadata"],
        )
    stale = leftover_files(files, state, baseline_files)
    if stale:
        return _cleanup_edit(
            number,
            "delete-files",
            f"Delete the files {', '.join(item['filename'] for item in stale)} from this session; no step uses them.",
            [],
            ["delete_blob"],
            delete_files=[item["id"] for item in stale],
        )
    return None


def leftover_files(files: Sequence[Mapping[str, Any]], state: Mapping[str, Any], baseline_files: Sequence[str] | None) -> list[JsonObject]:
    """Files the graph no longer uses that a battery attempt left: new since the attempt began, or (with no attempt) named battery_*."""
    referenced = referenced_files(state)
    return [
        {"id": item["id"], "filename": item["filename"]}
        for item in files
        if item["id"] not in referenced
        and (item["id"] not in baseline_files if baseline_files is not None else item["filename"].startswith(TEMPORARY_PREFIX))
    ]


def cleanup_dry_run(saved: Mapping[str, Any] | None, *, base_url: str, session_id: str, account: str) -> JsonObject:
    """What ``cleanup --execute`` would do, from local files only: no network call is made."""
    name = render_was(graph_view(saved["baseline"]), "/metadata/name") if saved is not None else None
    return {
        "base_url": base_url,
        "session_id": session_id,
        "account": account,
        "saved_attempt": saved["attempt"] if saved is not None else None,
        "would": [
            "sign in and read the graph, its pending review cards and the session's files",
            f"ask the Composer, one judged chat turn at a time (at most {CLEANUP_TURN_LIMIT}), to remove each {TEMPORARY_PREFIX}* "
            f"output, then each {TEMPORARY_PREFIX}* step (reconnecting what fed it to what it fed), then each battery marker "
            "sentence in a prompt (approving that prompt's review card only on a clean verdict)",
            (
                f"ask for the pipeline name to be put back to {name} (the saved attempt's starting graph)"
                if saved is not None
                else "report a battery pipeline name without changing it (no saved attempt gives the name to restore)"
            ),
            (
                "ask for files added since the saved attempt began that no step uses to be deleted"
                if saved is not None
                else f"ask for {TEMPORARY_PREFIX}* files that no step uses to be deleted"
            ),
            "stop at the first turn whose verdict is not clean, then report leftovers, files left and new review cards",
        ],
        "note": "nothing was contacted; pass --execute to run the cleanup",
    }


def cleanup(battery: EditBattery) -> int:
    """Ask the Composer, one judged chat turn at a time, to remove a battery attempt's leftovers; report what remains.

    Each turn is judged like a battery edit (and its prompt review card
    approved only on a clean verdict). The first failing turn stops the
    cleanup. With a saved attempt for this session, ``--attempt`` and
    ``--plan``, its starting graph gives the pipeline name to restore and the
    files that were already there.
    """
    sign_in(battery.client, battery.auth, prompt_secret=battery.prompt_secret)
    try:
        battery.require_auto_commit()
        saved = load_checkpoint(battery.store, battery.name, session_id=battery.session_id, attempt=battery.attempt, plan=battery.plan)
        baseline_state = saved["baseline"] if saved is not None else None
        baseline_files = saved["baseline_files"] if saved is not None else None
        cards_before = {event["id"] for event in battery.pending_reviews()}
        rows_offset = len(battery.rows_from(0))
        turns: list[JsonObject] = []
        for number in range(1, CLEANUP_TURN_LIMIT + 1):
            battery.keep_token_fresh()
            state = battery.state()
            edit = next_cleanup_edit(state, battery.read_text, battery.session_files(), baseline_state, baseline_files, number)
            if edit is None:
                break
            operation_id = str(uuid4())
            accepted = battery.send(operation_id, state["id"], edit["message"])
            operation, _ = battery.wait_for(operation_id, accepted["poll_after_ms"] if "poll_after_ms" in accepted else None)
            approved: list[JsonObject] = []
            fields, _, rows_offset = battery.verdict(
                edit,
                state,
                operation,
                operation_id,
                baseline_state=baseline_state or state,
                rows_offset=rows_offset,
                shed_before=None,
                approved=approved,
                save=lambda: None,
            )
            turn = {"number": number, "id": edit["id"], "message": edit["message"], "approvals": approved}
            turn.update({key: fields[key] for key in ("status", "passed", "findings", "warnings", "server_written")})
            turns.append(turn)
            emit("cleanup_turn", **turn)
            if not fields["passed"]:
                break
        final = battery.state()
        report = {
            "turns": len(turns),
            "failed_turn": next((turn["number"] for turn in turns if not turn["passed"]), None),
            "leftovers": leftover_findings(final, battery.read_text),
            "files_left": leftover_files(battery.session_files(), final, baseline_files),
            "new_pending_review_cards": [
                {"id": event["id"], "kind": event["kind"]} for event in battery.pending_reviews() if event["id"] not in cards_before
            ],
            "pipeline_name_reported_only": baseline_state is None and "/metadata/name" in leftover_findings(final, battery.read_text),
        }
        emit("cleanup_result", **report)
        clean = (
            report["failed_turn"] is None
            and not report["leftovers"]
            and not report["files_left"]
            and not report["new_pending_review_cards"]
        )
        return 0 if clean else 1
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
        choices=["run", "plan", "report", "cleanup", "setup"],
        help=(
            "setup: with --execute, create a generated --fixture's graph in an empty session (upload, import, approve its "
            "prompt reviews after checking them); run the battery; plan: validate and list the plan with its tool coverage; report: summarise a saved attempt "
            "(no network); cleanup: with --execute, ask the Composer in judged chat turns to remove a battery attempt's "
            "leftovers and report what remains (without --execute it only prints what it would do, with no network call)"
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
        "--workload",
        choices=["structural-cycle"],
        help=(
            "run a workload built in code instead of --plan: structural-cycle is the ten-step add/swap/remove cycle around "
            "an anchor step, rotating through --fixture's positions, judged by its own verifier (see the runbook)"
        ),
    )
    parser.add_argument(
        "--steps",
        type=int,
        default=structural_cycle.DEFAULT_STEPS,
        help=f"structural-cycle: steps to request, 1-{structural_cycle.MAX_STEPS} (default {structural_cycle.DEFAULT_STEPS})",
    )
    for role, default in structural_cycle.DEFAULT_NAMES.items():
        parser.add_argument(f"--{role}", help=f"structural-cycle without --fixture: the {role} of the one position (default {default})")
    parser.add_argument(
        "--fixture",
        type=Path,
        help=(
            "a directory written by scripts.staging.large_graph_fixture: setup creates its graph; structural-cycle "
            "rotates through its manifest's positions"
        ),
    )
    parser.add_argument(
        "--end-review-evidence",
        choices=["auto", "required", "if-recorded"],
        default="auto",
        help=(
            "structural-cycle: if-recorded fails a recorded end review that is not clean and admitted and warns when "
            "none is recorded; required also fails a step with no recorded end review; auto (default) is required "
            f"when the server's audit-grade messages view sets {END_REVIEW_VIEW_HEADER} and if-recorded when it does "
            "not (see the runbook)"
        ),
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=2.0,
        help="minimum seconds between operation polls and evidence reads (the server's poll_after_ms is honoured when longer)",
    )
    add_execute_flag(parser)
    return parser


def _setup(
    args: argparse.Namespace, session_id: str, *, client_factory: Callable[[str], WebClient], prompt_secret: Callable[[str], str]
) -> int:
    """The ``setup`` action: create a generated fixture's graph in an empty session (dry run unless ``--execute``)."""
    if args.fixture is None:
        raise UsageError("setup needs --fixture, a directory written by scripts.staging.large_graph_fixture")
    fixture = load_fixture(args.fixture)
    base_url = validate_base_url(setting(args.base_url, flag="--base-url", env="ELSPETH_OPS_BASE_URL"))
    auth = account_from(args, prefix="", env="ELSPETH_OPS_ACCOUNT")
    if not args.execute:
        emit(
            "dry_run",
            base_url=base_url,
            session_id=session_id,
            account=auth.username,
            uploads=[{"name": name, "bytes": len(content)} for name, content in fixture.contents.items()],
            yaml_bytes=len(fixture.yaml_text.encode()),
            measures=fixture.manifest["measures"],
            note="nothing was contacted; pass --execute to upload the files, import the graph and approve its prompt reviews",
        )
        return 0
    client = client_factory(base_url)
    try:
        sign_in(client, auth, prompt_secret=prompt_secret)
        setup_fixture(client, session_id, fixture, emit=emit)
    finally:
        client.close()
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[str], WebClient] = WebClient,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    prompt_secret: Callable[[str], str] = getpass.getpass,
) -> int:
    args = build_parser().parse_args(argv)
    names = {"source": args.source, "anchor": args.anchor, "sink": args.sink}
    if args.workload == "structural-cycle":
        if args.fixture is not None and any(value is not None for value in names.values()):
            raise UsageError("give --fixture or --source/--anchor/--sink, not both")
        if args.fixture is not None:
            positions = list(load_fixture(args.fixture).positions)
        else:
            chosen = {role: value or structural_cycle.DEFAULT_NAMES[role] for role, value in names.items()}
            positions = [Position(chosen["source"], chosen["anchor"], chosen["sink"], True)]
        plan = structural_cycle.structural_cycle_plan(steps=args.steps, positions=positions)
    else:
        plan = load_plan(args.plan)
    if args.action == "cleanup" and plan.verifier is not None:
        raise UsageError("cleanup removes the battery_* leftovers of a plan file; restore a workload fixture from a fresh copy")
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
            **({"start": plan.start, "pass_rule": plan.pass_rule, "attachments": sorted(plan.attachments)} if plan.attachments else {}),
        )
        for number, edit in enumerate(plan.edits, start=1):
            extra: JsonObject = {"attach": edit["attach"]} if "attach" in edit else {}
            emit("planned_edit", number=number, id=edit["id"], message=edit["message"], roles=sorted(plan.roles_of(edit)), **extra)
        return 0
    session_id = require_uuid(setting(args.session, flag="--session", env="ELSPETH_OPS_SESSION"), flag="--session")
    if args.action == "setup":
        return _setup(args, session_id, client_factory=client_factory, prompt_secret=prompt_secret)
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
    if args.action == "cleanup" and not args.execute:
        saved = load_checkpoint(store, checkpoint_name(args.attempt, session_id), session_id=session_id, attempt=args.attempt, plan=plan)
        emit("dry_run", **cleanup_dry_run(saved, base_url=base_url, session_id=session_id, account=auth.username))
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
        end_review_evidence=args.end_review_evidence,
    )
    if args.action == "cleanup":
        return cleanup(battery)
    summary = battery.run()
    # A plan file passes on gold (or, with pass_rule all_passed_as_found, on every edit passed and the
    # graph left as found); a workload passes, as the reference runner does, when every requested step passed.
    if plan.verifier is not None:
        return 0 if summary["all_passed"] else 1
    return 0 if summary["all_passed_as_found" if plan.pass_rule == "all_passed_as_found" else "gold"] else 1


if __name__ == "__main__":
    raise SystemExit(run_tool(main))
