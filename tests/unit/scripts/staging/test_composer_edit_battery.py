"""The Composer edit battery against an in-memory deployment.

The fake Composer applies a scripted change per turn and records the rows the
server would store (assistant tool calls with outcomes, timed LLM-call audit
rows, review cards). The battery reaches it only through a real
:class:`~scripts.staging.opslib.WebClient`.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from scripts.staging import composer_edit_battery as battery
from scripts.staging.edit_plan import PLAN_FORMAT, load_plan
from scripts.staging.opslib import AccountAuth, CheckpointStore, OperationFailed, WebClient
from scripts.staging.repair_copied_prompt_reviews import prompt_surface

from tests.unit.scripts.staging.fake_deployment import BASE_URL, FakeDeployment, FakeTurn, Session

SESSION = "11111111-2222-4333-8444-555555555555"
type State = dict[str, Any]
type Change = Callable[[State], None]


def baseline() -> State:
    return {
        "id": "state-0",
        "version": 1,
        "sources": {"rows": {"plugin": "csv", "on_success": "c0", "options": {}, "on_validation_failure": "discard"}},
        "nodes": [
            {
                "id": "classify",
                "node_type": "transform",
                "plugin": "llm",
                "input": "c0",
                "on_success": "out",
                "on_error": "discard",
                "options": {"prompt_template": "Classify the row.", "model": "m"},
                "description": None,
            },
            {
                "id": "audit_copy",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "c9",
                "on_success": "out",
                "on_error": "discard",
                "options": {},
                "description": "Unrelated step.",
            },
        ],
        "edges": [{"id": "e1", "from_node": "rows", "to_node": "classify", "edge_type": "on_success", "label": None}],
        "outputs": [{"name": "out", "plugin": "json", "options": {"path": "out.jsonl"}, "on_write_failure": "discard"}],
        "metadata": {"name": "Pipeline", "description": ""},
        "is_valid": True,
        "validation_errors": None,
    }


PLAN_EDITS: list[dict[str, Any]] = [
    {
        "id": "rename",
        "message": 'Rename the pipeline to "Battery check".',
        "expect": [{"equals": "/metadata/name", "value": "Battery check"}],
        "tools_any": ["set_metadata"],
    },
    {
        "id": "insert",
        "message": "Insert a passthrough transform with the id battery_a between {source} and {head}.",
        "expect": [{"present": "/nodes/battery_a"}, {"chain": {"from": "{source}", "to": "{head}", "via": ["battery_a"]}}],
        "may_change": ["/edges"],
        "tools_any": ["splice_transform", "upsert_node"],
    },
    {
        "id": "max-tokens",
        "message": "Set max_tokens on {llm_inline} to 777.",
        "expect": [{"equals": "/nodes/{llm_inline}/options/max_tokens", "value": 777}],
        "tools_any": ["patch_node_options"],
    },
    {
        "id": "prompt",
        "message": 'Add the sentence "Battery marker one." at the end of the prompt of {llm_inline}.',
        "expect": [{"text": "/nodes/{llm_inline}/options/prompt_template", "append": "Battery marker one.", "form": "inline"}],
        "may_change": ["/nodes/{llm_inline}/options/interpretation_requirements"],
        "tools_any": ["change_substring", "patch_node_options"],
        "approve": {"kind": "llm_prompt_template", "node": "{llm_inline}"},
    },
]


def rename(state: State) -> None:
    state["metadata"]["name"] = "Battery check"


def insert(state: State) -> None:
    state["sources"]["rows"]["on_success"] = "ca"
    state["nodes"].append(
        {
            "id": "battery_a",
            "node_type": "transform",
            "plugin": "passthrough",
            "input": "ca",
            "on_success": "c0",
            "on_error": None,
            "options": {},
        }
    )
    state["edges"].append({"id": "e2", "from_node": "rows", "to_node": "battery_a", "edge_type": "on_success", "label": None})


def node(state: State, node_id: str) -> State:
    found: State = next(item for item in state["nodes"] if item["id"] == node_id)
    return found


def max_tokens(state: State) -> None:
    node(state, "classify")["options"]["max_tokens"] = 777


def prompt(state: State) -> None:
    options = node(state, "classify")["options"]
    options["prompt_template"] += "\nBattery marker one."
    options["interpretation_requirements"] = [{"kind": "llm_prompt_template", "status": "pending"}]


CHANGES: list[tuple[Change, str]] = [
    (rename, "set_metadata"),
    (insert, "splice_transform"),
    (max_tokens, "patch_node_options"),
    (prompt, "change_substring"),
]


class FakeComposer:
    """Applies the scripted change for each turn, like an obedient Composer, unless told to misbehave."""

    def __init__(self) -> None:
        self.turn = 0
        self.skip: set[int] = set()
        self.extra: dict[int, Change] = {}
        self.tool_override: dict[int, tuple[str, str]] = {}
        self.status: dict[int, str] = {}
        self.review_surface: dict[int, Change] = {}

    def __call__(self, state: State, message: str) -> FakeTurn:
        self.turn += 1
        number = self.turn
        change, tool = CHANGES[(number - 1) % len(CHANGES)]
        outcome = "applied"
        if number in self.tool_override:
            tool, outcome = self.tool_override[number]
        if number not in self.skip:
            change(state)
        if number in self.extra:
            self.extra[number](state)
        state["id"] = f"state-{number}"
        reviews: dict[str, Any] = {}
        if change is prompt and number not in self.skip:
            surface = prompt_surface(node(state, "classify")["options"])
            if number in self.review_surface:
                self.review_surface[number](surface)
            reviews = {
                f"review-{number}": {
                    "event": {"id": f"review-{number}", "kind": "llm_prompt_template"},
                    "snapshot": {"node_id": "classify", "anchor": f"anchor-{number}", "authored_surface": surface, "bindings": []},
                    "pending": True,
                }
            }
        audit = [
            {
                "_kind": "llm_call_audit",
                "call": {
                    "status": "success",
                    "latency_ms": 2000,
                    "started_at": "2026-01-01T00:00:01+00:00",
                    "finished_at": "2026-01-01T00:00:03+00:00",
                    "context_budget": {
                        "shed_history_indices": [0] if number >= 3 else [],
                        "stubbed_tool_call_ids": [],
                        "shed_hatch_event_indices": [],
                        "request_wire_bytes": 5000,
                        "estimated_tokens_before": 2000,
                    },
                },
            }
        ]
        calls = [{"id": f"call-{number}", "type": "function", "function": {"name": tool, "arguments": "{}"}, "outcome": outcome}]
        status = self.status[number] if number in self.status else "completed"
        return FakeTurn(state=state, audit=audit, assistant_tool_calls=calls, reviews=reviews, status=status)


def plan_file(tmp_path: Path, edits: list[dict[str, Any]] | None = None) -> Path:
    path = tmp_path / "plan.json"
    path.write_text(
        json.dumps(
            {
                "format": PLAN_FORMAT,
                "name": "unit-plan",
                "description": "",
                "forbidden_tools": ["set_pipeline"],
                "targets": {
                    "source": {"kind": "first_source"},
                    "head": {"kind": "consumer_of", "target": "source"},
                    "llm_inline": {"kind": "llm_node", "prompt": "inline"},
                },
                "edits": PLAN_EDITS if edits is None else edits,
            }
        )
    )
    return path


class Clock:
    def __init__(self) -> None:
        self.value = 1_000_000.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def make(
    tmp_path: Path, composer: FakeComposer | None = None, *, keep_going: bool = False, attempt: str = "1"
) -> tuple[battery.EditBattery, FakeDeployment, FakeComposer]:
    composer = composer or FakeComposer()
    clock = Clock()
    fake = FakeDeployment(
        passwords={"tester": "pw"}, sessions={SESSION: Session(owner="tester", state=baseline())}, provider=composer, clock=clock
    )
    run = battery.EditBattery(
        client=WebClient(BASE_URL, transport=fake),
        auth=AccountAuth(username="tester", password_env="TEST_BATTERY_PASSWORD"),
        store=CheckpointStore(tmp_path / "state"),
        session_id=SESSION,
        attempt=attempt,
        plan=load_plan(plan_file(tmp_path)),
        keep_going=keep_going,
        sleep=clock.sleep,
        now=clock,
    )
    return run, fake, composer


@pytest.fixture(autouse=True)
def _password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_BATTERY_PASSWORD", "pw")


def writes(fake: FakeDeployment) -> list[tuple[str, str]]:
    return [(method, path) for method, path, _ in fake.requests if method != "GET" and not path.startswith("/api/auth/")]


def saved(run: battery.EditBattery) -> dict[str, Any]:
    checkpoint = run.store.read(run.name)
    assert checkpoint is not None
    return checkpoint


# ── verdicts and the K-of-N summary ───────────────────────────────────────


def test_every_edit_is_an_ordinary_chat_message_and_an_obedient_run_is_gold(tmp_path: Path) -> None:
    run, fake, _ = make(tmp_path)
    summary = run.run()
    assert summary["line"] == "longest run in a row: 4 of 4"
    assert summary["gold"] is True
    session_writes = [path for _, path in writes(fake)]
    assert session_writes.count(f"/api/sessions/{SESSION}/messages") == 4
    assert set(session_writes) == {f"/api/sessions/{SESSION}/messages", f"/api/sessions/{SESSION}/interpretations/review-4/resolve"}


def test_each_edit_records_tools_time_provider_calls_and_shedding(tmp_path: Path) -> None:
    run, _, _ = make(tmp_path)
    summary = run.run()
    results = saved(run)["results"]
    assert results[1]["evidence"]["tool_calls_by_name"] == {"splice_transform": 1}
    assert results[1]["timing"]["span_ms"] == 5000
    assert results[1]["timing"]["provider_busy_ms"] == 2000
    assert results[1]["timing"]["server_ms"] == 3000
    assert results[1]["history_shed"] is False
    assert results[2]["history_shed"] is True
    assert results[2]["evidence"]["history"]["newly_shed_history"] == [0]
    assert results[3]["history_shed"] is True
    assert results[3]["evidence"]["history"]["newly_shed_history"] == []
    assert summary["totals"]["provider_calls"] == 4
    assert summary["totals"]["span_ms"] == 20_000
    assert summary["totals"]["server_ms"] == 12_000
    assert summary["server_time_share_of_span"] == 0.6
    assert summary["history_shed"] == {"edits": 2, "passed": 2}
    assert summary["not_available_from_api"]


def test_an_edit_whose_change_did_not_land_fails_and_stops_the_attempt(tmp_path: Path) -> None:
    composer = FakeComposer()
    composer.skip = {2}
    run, fake, _ = make(tmp_path, composer)
    summary = run.run()
    results = saved(run)["results"]
    assert results[1]["passed"] is False
    assert "expect[0]:present:/nodes/battery_a" in results[1]["findings"]
    assert len(results) == 2
    assert summary["first_failure"] == 2
    assert summary["line"] == "longest run in a row: 1 of 4"
    assert summary["gold"] is False
    assert [path for _, path in writes(fake)].count(f"/api/sessions/{SESSION}/messages") == 2
    again, _, _ = make(tmp_path, composer)
    assert again.run()["stopped_reason"] == "edit 2 (insert) failed verification"


def test_an_unrelated_change_in_the_same_turn_fails_the_edit(tmp_path: Path) -> None:
    composer = FakeComposer()

    def drift(state: State) -> None:
        node(state, "audit_copy")["description"] = "rewritten"

    composer.extra = {3: drift}
    run, _, _ = make(tmp_path, composer)
    run.run()
    result = saved(run)["results"][2]
    assert result["passed"] is False
    assert result["findings"] == ["unrelated_change:/nodes/audit_copy/description"]


def test_keep_going_judges_every_edit_and_reports_the_longest_run(tmp_path: Path) -> None:
    composer = FakeComposer()
    composer.skip = {1}
    run, _, _ = make(tmp_path, composer, keep_going=True)
    summary = run.run()
    assert summary["judged"] == 4
    assert summary["passed"] == 3
    assert summary["first_failure"] == 1
    assert summary["line"] == "longest run in a row: 3 of 4"


def test_a_turn_that_did_not_complete_fails(tmp_path: Path) -> None:
    composer = FakeComposer()
    composer.status = {1: "failed"}
    run, _, _ = make(tmp_path, composer)
    run.run()
    assert saved(run)["results"][0]["findings"][0].startswith("turn_not_completed:failed")


def test_an_applied_forbidden_tool_or_no_expected_tool_fails_the_edit(tmp_path: Path) -> None:
    composer = FakeComposer()
    composer.tool_override = {1: ("set_pipeline", "applied"), 3: ("patch_node_options", "rejected")}
    run, _, _ = make(tmp_path, composer, keep_going=True)
    run.run()
    results = saved(run)["results"]
    assert results[0]["findings"] == ["forbidden_tool_applied:set_pipeline", "expected_tool_not_used:set_metadata"]
    assert results[2]["findings"] == ["expected_tool_not_used:patch_node_options"]


def test_a_new_validation_error_fails_the_edit(tmp_path: Path) -> None:
    composer = FakeComposer()

    def invalidate(state: State) -> None:
        state["is_valid"] = False
        state["validation_errors"] = [{"error_code": "broken", "component": "battery_a", "message": "broken"}]

    composer.extra = {2: invalidate}
    run, _, _ = make(tmp_path, composer)
    run.run()
    assert saved(run)["results"][1]["findings"] == ['new_validation_error:["broken", "battery_a", "broken"]']


# ── review approval ────────────────────────────────────────────────────────


def test_a_review_card_is_approved_only_after_its_edit_verified(tmp_path: Path) -> None:
    composer = FakeComposer()

    def wrong_prompt(state: State) -> None:
        node(state, "classify")["options"]["prompt_template"] = "Something else entirely."

    composer.extra = {4: wrong_prompt}
    run, fake, _ = make(tmp_path, composer)
    run.run()
    assert saved(run)["results"][3]["passed"] is False
    assert not [path for _, path in writes(fake) if path.endswith("/resolve")]


def test_an_approved_card_settles_the_requirement_and_the_edit_passes(tmp_path: Path) -> None:
    run, fake, _ = make(tmp_path)
    run.run()
    result = saved(run)["results"][3]
    assert result["passed"] is True
    assert result["approvals"] == [{"event_id": "review-4", "kind": "llm_prompt_template", "node_id": "classify"}]
    options = node(fake.sessions[SESSION].state, "classify")["options"]
    assert options["interpretation_requirements"] == [{"kind": "llm_prompt_template", "status": "resolved"}]
    # The state the next edit is judged from is the live, post-approval state.
    assert saved(run)["last_state"] == fake.sessions[SESSION].state


def test_a_card_showing_other_text_than_the_node_is_never_approved(tmp_path: Path) -> None:
    composer = FakeComposer()

    def tamper(surface: State) -> None:
        surface["prompt_template"] = "Text the node does not hold."

    composer.review_surface = {4: tamper}
    run, fake, _ = make(tmp_path, composer)
    with pytest.raises(OperationFailed, match="does not show the node's current prompt"):
        run.run()
    assert not [path for _, path in writes(fake) if path.endswith("/resolve")]


# ── resume ─────────────────────────────────────────────────────────────────


class Interrupted(Exception):
    pass


def test_resume_polls_the_saved_operation_instead_of_resubmitting(tmp_path: Path) -> None:
    run, fake, composer = make(tmp_path)
    posts = 0

    def interrupt(method: str, path: str) -> None:
        nonlocal posts
        if method == "POST" and path.endswith("/messages"):
            posts += 1
            if posts == 2:
                raise Interrupted

    fake.on_request = interrupt
    with pytest.raises(Interrupted):
        run.run()
    interrupted = saved(run)["results"][1]
    assert "submitted_at" not in interrupted
    fake.on_request = None
    resumed = battery.EditBattery(
        client=WebClient(BASE_URL, transport=fake),
        auth=run.auth,
        store=run.store,
        session_id=SESSION,
        attempt="1",
        plan=run.plan,
        sleep=run.sleep,
        now=run.now,
    )
    assert resumed.run()["gold"] is True
    assert composer.turn == 4
    operation_ids = [
        payload["operation_id"]
        for method, path, payload in fake.requests
        if method == "POST" and path.endswith("/messages") and isinstance(payload, dict)
    ]
    assert operation_ids.count(interrupted["operation_id"]) == 2
    assert len(set(operation_ids)) == 4


def test_resume_after_a_recorded_submission_only_polls(tmp_path: Path) -> None:
    run, fake, composer = make(tmp_path)

    def interrupt(method: str, path: str) -> None:
        if method == "GET" and "/operations/" in path and len(saved(run)["results"]) == 2:
            raise Interrupted

    fake.on_request = interrupt
    with pytest.raises(Interrupted):
        run.run()
    recorded = saved(run)["results"][1]
    assert "submitted_at" in recorded
    fake.on_request = None
    resumed = battery.EditBattery(
        client=WebClient(BASE_URL, transport=fake),
        auth=run.auth,
        store=run.store,
        session_id=SESSION,
        attempt="1",
        plan=run.plan,
        sleep=run.sleep,
        now=run.now,
    )
    assert resumed.run()["gold"] is True
    assert composer.turn == 4
    operation_ids = [
        payload["operation_id"]
        for method, path, payload in fake.requests
        if method == "POST" and path.endswith("/messages") and isinstance(payload, dict)
    ]
    assert operation_ids.count(recorded["operation_id"]) == 1


def test_resume_reuses_the_targets_and_messages_resolved_at_the_start(tmp_path: Path) -> None:
    run, _, _ = make(tmp_path)
    run.store.write(run.name, {})
    with pytest.raises(OperationFailed, match="not a complete, valid checkpoint"):
        run.run()
    (tmp_path / "second").mkdir()
    run2, _, _ = make(tmp_path / "second")
    run2.run()
    checkpoint = saved(run2)
    assert checkpoint["targets"]["resolved"] == {"source": "rows", "head": "classify", "llm_inline": "classify"}
    assert checkpoint["edits"][1]["message"] == "Insert a passthrough transform with the id battery_a between rows and classify."


def test_resume_with_a_different_plan_is_refused(tmp_path: Path) -> None:
    run, fake, _ = make(tmp_path)
    run.run()
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    changed = battery.EditBattery(
        client=WebClient(BASE_URL, transport=fake),
        auth=run.auth,
        store=run.store,
        session_id=SESSION,
        attempt="1",
        plan=load_plan(plan_file(other_dir, copy.deepcopy(PLAN_EDITS[:2]))),
    )
    with pytest.raises(OperationFailed, match="different plan"):
        changed.start()


def test_a_graph_changed_outside_the_battery_stops_the_attempt(tmp_path: Path) -> None:
    run, fake, _ = make(tmp_path)
    state_reads = 0

    def outside(method: str, path: str) -> None:
        # The third state read is edit 1's after-turn read; the change lands before edit 2 starts.
        nonlocal state_reads
        if method == "GET" and path.endswith("/state"):
            state_reads += 1
            if state_reads == 3:
                node(fake.sessions[SESSION].state, "audit_copy")["description"] = "edited by hand"

    fake.on_request = outside
    with pytest.raises(OperationFailed, match="changed outside the battery"):
        run.run()


# ── command line ───────────────────────────────────────────────────────────


def test_dry_run_contacts_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def no_client(base_url: str) -> WebClient:
        raise AssertionError("dry run built a client")

    code = battery.main(
        ["run", "--base-url", "http://localhost:8451", "--session", SESSION, "--account", "tester", "--state-dir", str(tmp_path / "s")],
        client_factory=no_client,
    )
    assert code == 0
    event = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert event["event"] == "dry_run"
    assert event["edits"] == 50


def test_report_summarises_a_saved_attempt_without_the_network(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run, _, _ = make(tmp_path)
    run.run()
    capsys.readouterr()

    def no_client(base_url: str) -> WebClient:
        raise AssertionError("report built a client")

    code = battery.main(
        ["report", "--session", SESSION, "--state-dir", str(tmp_path / "state"), "--plan", str(tmp_path / "plan.json")],
        client_factory=no_client,
    )
    assert code == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[-1]["line"] == "longest run in a row: 4 of 4"
    assert [line["number"] for line in lines if line["event"] == "edit_result"] == [1, 2, 3, 4]


def test_help_needs_no_environment(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        battery.main(["--help"])
    assert exit_info.value.code == 0
    assert "longest run in a row: K of N" in " ".join(capsys.readouterr().out.split())


def test_target_flags_must_be_name_equals_id() -> None:
    assert battery.parse_targets(["head=classify"]) == {"head": "classify"}
    with pytest.raises(battery.UsageError):
        battery.parse_targets(["head"])
