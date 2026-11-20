"""The structural-cycle workload: its wording, its plan, its fixture check, its verifier and a battery run of it.

The battery reaches the in-memory deployment only through a real
:class:`~scripts.staging.opslib.WebClient`. The fake Composer rewires the
graph to the order each step asks for, as an obedient Composer would.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from scripts.staging import composer_edit_battery as battery
from scripts.staging import structural_cycle as cycle
from scripts.staging.edit_plan import resolve_edits
from scripts.staging.opslib import AccountAuth, CheckpointStore, OperationFailed, UsageError, WebClient

from tests.unit.scripts.staging.fake_deployment import BASE_URL, FakeDeployment, FakeTurn, Session

SESSION = "11111111-2222-4333-8444-555555555555"
SOURCE, ANCHOR, SINK = "source", "capacity_probe", "results"
NAMES = {"source": SOURCE, "anchor": ANCHOR, "sink": SINK}
type State = dict[str, Any]

SUFFIX = (
    " Apply only this structural edit and its wiring. New passthrough nodes use schema.mode=observed. "
    "Preserve existing prompts, queries, files, source options and sink options. Do not run the pipeline."
)
# The measured workload, word for word, for the default names on a fixture whose source feeds "c0".
CYCLE_ONE = [
    "Add a passthrough transform named compact_before_a between source and capacity_probe.",
    "Add a passthrough transform named compact_after_a between capacity_probe and the results sink.",
    "Add a passthrough transform named compact_before_b between source and compact_before_a.",
    "Add a passthrough transform named compact_after_b between compact_after_a and the results sink.",
    "Swap compact_before_a and compact_before_b so source flows through compact_before_a, compact_before_b, then capacity_probe.",
    "Swap compact_after_a and compact_after_b so capacity_probe flows through compact_after_b, compact_after_a, then results.",
    "Remove compact_before_a and reconnect source directly to compact_before_b.",
    "Remove compact_after_a and reconnect compact_after_b directly to results.",
    "Remove compact_before_b and reconnect source directly to capacity_probe.",
    'Remove compact_after_b and reconnect capacity_probe directly to results. Restore source.on_success="c0", '
    'capacity_probe.input="c0", and capacity_probe.on_success="results".',
]
CYCLE_ONE_ORDERS = [
    ["compact_before_a", ANCHOR],
    ["compact_before_a", ANCHOR, "compact_after_a"],
    ["compact_before_b", "compact_before_a", ANCHOR, "compact_after_a"],
    ["compact_before_b", "compact_before_a", ANCHOR, "compact_after_a", "compact_after_b"],
    ["compact_before_a", "compact_before_b", ANCHOR, "compact_after_a", "compact_after_b"],
    ["compact_before_a", "compact_before_b", ANCHOR, "compact_after_b", "compact_after_a"],
    ["compact_before_b", ANCHOR, "compact_after_b", "compact_after_a"],
    ["compact_before_b", ANCHOR, "compact_after_b"],
    [ANCHOR, "compact_after_b"],
    [ANCHOR],
]


def fixture(queries: int = 3) -> State:
    return {
        "id": "state-0",
        "version": 1,
        "sources": {SOURCE: {"plugin": "csv", "on_success": "c0", "options": {"path": "rows.csv"}, "on_validation_failure": "errors"}},
        "nodes": [
            {
                "id": ANCHOR,
                "node_type": "transform",
                "plugin": "llm",
                "input": "c0",
                "on_success": SINK,
                "on_error": "errors",
                "options": {"prompt_template": "Score the row.", "queries": {f"q{index}": {"field": "x"} for index in range(queries)}},
                "description": None,
            }
        ],
        "edges": [],
        "outputs": [
            {"name": SINK, "plugin": "json", "options": {"path": "out.jsonl"}, "on_write_failure": "discard"},
            {"name": "errors", "plugin": "json", "options": {"path": "errors.jsonl"}, "on_write_failure": "discard"},
        ],
        "metadata": {"name": "Large graph", "description": ""},
        "is_valid": True,
        "validation_errors": None,
    }


def passthrough(node_id: str) -> State:
    return {
        "id": node_id,
        "node_type": "transform",
        "plugin": "passthrough",
        "input": "",
        "on_success": "",
        "on_error": "discard",
        "options": {"schema": {"mode": "observed"}},
        "description": None,
    }


def rewire(state: State, order: list[str]) -> None:
    """Leave exactly ``order`` between the source and the sink, as an obedient Composer would."""
    kept = {node["id"]: node for node in state["nodes"]}
    nodes = [kept[name] if name in kept else passthrough(name) for name in order]
    route = "c0"
    state["sources"][SOURCE]["on_success"] = route
    for index, node in enumerate(nodes):
        node["input"] = route
        route = SINK if index == len(nodes) - 1 else f"{node['id']}_out"
        node["on_success"] = route
    state["nodes"] = nodes


class CycleComposer:
    """Rewires the graph to each step's order and records an end review, unless told otherwise."""

    def __init__(self, orders: list[list[str]]) -> None:
        self.orders = orders
        self.turn = 0
        self.messages: list[str] = []
        self.skip: set[int] = set()
        self.end_review: dict[int, tuple[str, str] | None] = {}
        self.extra: dict[int, Any] = {}
        self.segments: dict[int, list[State]] = {}

    def __call__(self, state: State, message: str) -> FakeTurn:
        self.turn += 1
        self.messages.append(message)
        number = self.turn
        if number not in self.skip:
            rewire(state, self.orders[number - 1])
        if number in self.extra:
            self.extra[number](state)
        state["id"] = f"state-{number}"
        audit: list[State] = [
            {
                "_kind": "llm_call_audit",
                "call": {
                    "status": "success",
                    "latency_ms": 2000,
                    "started_at": "2026-01-01T00:00:01+00:00",
                    "finished_at": "2026-01-01T00:00:03+00:00",
                    "prompt_tokens": 1000,
                    "request_byte_split": {"message_bytes": [900], "tools_bytes": 100, "total_bytes": 1000},
                    "context_budget": {
                        "shed_history_indices": [],
                        "stubbed_tool_call_ids": [],
                        "shed_hatch_event_indices": [],
                        "request_wire_bytes": 1000,
                        "estimated_tokens_before": 400,
                        "over_estimate": False,
                    },
                },
            }
        ]
        review = self.end_review[number] if number in self.end_review else ("clean", "ADMIT")
        if review is not None:
            audit.append(
                {
                    "_kind": "advisor_checkpoint_pass_audit",
                    "pass": {"phase": "end", "verdict": review[0], "publication_verdict": review[1], "source": "model"},
                }
            )
        calls = [{"id": f"call-{number}", "type": "function", "function": {"name": "upsert_node", "arguments": "{}"}, "outcome": "applied"}]
        segments = self.segments[number] if number in self.segments else [{"kind": "text", "content": "Done."}]
        return FakeTurn(state=state, audit=audit, assistant_tool_calls=calls, reply_segments=segments)


class Clock:
    def __init__(self) -> None:
        self.value = 1_000_000.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def orders_for(steps: int) -> list[list[str]]:
    return [cycle.planned_step(number, SOURCE, ANCHOR, SINK)[1] for number in range(1, steps + 1)]


def make(
    tmp_path: Path,
    steps: int = 10,
    composer: CycleComposer | None = None,
    *,
    keep_going: bool = False,
    end_review_evidence: cycle.EndReviewEvidence = "required",
    state: State | None = None,
) -> tuple[battery.EditBattery, FakeDeployment, CycleComposer]:
    composer = composer or CycleComposer(orders_for(steps))
    clock = Clock()
    fake = FakeDeployment(
        passwords={"tester": "pw"}, sessions={SESSION: Session(owner="tester", state=state or fixture())}, provider=composer, clock=clock
    )
    run = battery.EditBattery(
        client=WebClient(BASE_URL, transport=fake),
        auth=AccountAuth(username="tester", password_env="TEST_CYCLE_PASSWORD"),
        store=CheckpointStore(tmp_path / "state"),
        session_id=SESSION,
        attempt="1",
        plan=cycle.structural_cycle_plan(steps=steps, **NAMES),
        keep_going=keep_going,
        sleep=clock.sleep,
        now=clock,
        end_review_evidence=end_review_evidence,
    )
    return run, fake, composer


@pytest.fixture(autouse=True)
def _password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_CYCLE_PASSWORD", "pw")


def results(run: battery.EditBattery) -> list[dict[str, Any]]:
    saved = run.store.read(run.name)
    assert saved is not None
    found: list[dict[str, Any]] = saved["results"]
    return found


# ── the workload's wording and plan ───────────────────────────────────────


def test_the_first_cycle_is_the_measured_wording_word_for_word() -> None:
    plan = cycle.structural_cycle_plan(steps=10, **NAMES)
    edits = resolve_edits(plan, {}, fixture())
    assert [edit["message"] for edit in edits] == [text + SUFFIX for text in CYCLE_ONE]
    assert [edit["order"] for edit in edits] == CYCLE_ONE_ORDERS


def test_later_cycles_name_their_nodes_by_cycle_and_sixty_steps_end_on_a_restore() -> None:
    plan = cycle.structural_cycle_plan(steps=60, **NAMES)
    edits = resolve_edits(plan, {}, fixture())
    assert edits[10]["message"] == "Add a passthrough transform named cycle1_before_a between source and capacity_probe." + SUFFIX
    assert edits[59]["message"].startswith("Remove cycle5_after_b and reconnect capacity_probe directly to results. Restore ")
    assert edits[59]["order"] == [ANCHOR]
    assert [edit["id"] for edit in edits[:2]] == ["cycle-step-1", "cycle-step-2"]


def test_the_restore_step_names_the_fixture_s_own_routing_values() -> None:
    state = fixture()
    state["sources"][SOURCE]["on_success"] = "into llm"
    state["nodes"][0]["input"] = "into llm"
    state["nodes"][0]["on_success"] = SINK
    message = resolve_edits(cycle.structural_cycle_plan(steps=10, **NAMES), {}, state)[9]["message"]
    assert 'Restore source.on_success="into llm", capacity_probe.input="into llm", and capacity_probe.on_success="results".' in message


@pytest.mark.parametrize("steps", [0, 61])
def test_a_step_count_outside_one_to_sixty_is_refused(steps: int) -> None:
    with pytest.raises(UsageError, match="between 1 and 60"):
        cycle.structural_cycle_plan(steps=steps, **NAMES)


@pytest.mark.parametrize("names", [{**NAMES, "anchor": "a/b"}, {**NAMES, "sink": "{x}"}, {**NAMES, "sink": SOURCE}])
def test_component_names_must_be_plain_and_distinct(names: dict[str, str]) -> None:
    with pytest.raises(UsageError):
        cycle.structural_cycle_plan(steps=10, **names)


def test_the_plan_identity_changes_with_names_and_step_count() -> None:
    base = cycle.structural_cycle_plan(steps=25, **NAMES)
    assert base.name == "structural_cycle_25"
    assert base.sha256 == cycle.structural_cycle_plan(steps=25, **NAMES).sha256
    assert base.sha256 != cycle.structural_cycle_plan(steps=22, **NAMES).sha256
    assert base.sha256 != cycle.structural_cycle_plan(steps=25, **{**NAMES, "anchor": "other"}).sha256


# ── the fixture ────────────────────────────────────────────────────────────


def test_the_fixture_is_counted() -> None:
    assert cycle.check_fixture(fixture(queries=32), **NAMES) == {"sources": 1, "nodes": 1, "sinks": 2, "prompt_queries": 32}


def _second_node(state: State) -> None:
    state["nodes"].append(passthrough("extra"))


def _not_llm(state: State) -> None:
    state["nodes"][0]["plugin"] = "passthrough"


def _second_source(state: State) -> None:
    state["sources"]["other"] = copy.deepcopy(state["sources"][SOURCE])


def _no_sink(state: State) -> None:
    state["outputs"] = state["outputs"][1:]


def _invalid(state: State) -> None:
    state["is_valid"] = False


def _not_direct(state: State) -> None:
    state["nodes"][0]["on_success"] = "errors"


@pytest.mark.parametrize(
    ("damage", "match"),
    [
        (_second_node, "exactly one node"),
        (_not_llm, "exactly one node"),
        (_second_source, "one source"),
        (_no_sink, "one source"),
        (_invalid, "validate the fixture"),
        (_not_direct, "straight through"),
    ],
)
def test_a_graph_that_is_not_the_fixture_is_refused(damage: Any, match: str) -> None:
    state = fixture()
    damage(state)
    with pytest.raises(OperationFailed, match=match):
        cycle.check_fixture(state, **NAMES)


# ── the verifier: every code with a failing graph ──────────────────────────


def at_step(number: int) -> State:
    state = fixture()
    rewire(state, CYCLE_ONE_ORDERS[number - 1])
    return state


def verify(state: State, number: int) -> list[str]:
    return cycle.verify_state(fixture(), state, CYCLE_ONE_ORDERS[number - 1], **NAMES)


@pytest.mark.parametrize("number", range(1, 11))
def test_each_step_s_expected_graph_passes(number: int) -> None:
    assert verify(at_step(number), number) == []


def _anchor(state: State) -> State:
    node: State = next(node for node in state["nodes"] if node["id"] == ANCHOR)
    return node


def _drop_anchor_option(state: State) -> None:
    del _anchor(state)["options"]["queries"]["q0"]


def _change_source_option(state: State) -> None:
    state["sources"][SOURCE]["options"]["path"] = "other.csv"


def _change_sink(state: State) -> None:
    state["outputs"][0]["options"]["path"] = "elsewhere.jsonl"


def _change_metadata(state: State) -> None:
    state["metadata"]["name"] = "Renamed"


def _extra_node(state: State) -> None:
    state["nodes"].append(passthrough("stray"))


def _llm_aux(state: State) -> None:
    node = next(node for node in state["nodes"] if node["id"] == "compact_before_a")
    node["plugin"] = "llm"


def _broken_link(state: State) -> None:
    _anchor(state)["input"] = "nowhere"


def _wrong_end(state: State) -> None:
    next(node for node in state["nodes"] if node["id"] == "compact_after_a")["on_success"] = "errors"


def _invalid_now(state: State) -> None:
    state["is_valid"] = False


@pytest.mark.parametrize(
    ("damage", "codes"),
    [
        (_extra_node, ["node_inventory_mismatch"]),
        (_drop_anchor_option, ["original_llm_content_changed"]),
        (_change_source_option, ["source_content_changed"]),
        (_second_source, ["source_content_changed"]),
        (_change_sink, ["sink_content_changed"]),
        (_change_metadata, ["metadata_changed"]),
        (_llm_aux, ["unexpected_auxiliary_plugin"]),
        (_broken_link, ["routing_does_not_reach_sink", "routing_mismatch"]),
        (_wrong_end, ["routing_does_not_reach_sink"]),
        (_invalid_now, ["runtime_validation_failed"]),
    ],
)
def test_each_state_code_fires_on_its_own_damage(damage: Any, codes: list[str]) -> None:
    state = at_step(2)
    damage(state)
    assert verify(state, 2) == codes


def test_a_missing_anchor_is_both_an_inventory_and_a_content_change() -> None:
    state = at_step(2)
    state["nodes"] = [node for node in state["nodes"] if node["id"] != ANCHOR]
    assert verify(state, 2) == [
        "node_inventory_mismatch",
        "original_llm_content_changed",
        "routing_does_not_reach_sink",
        "routing_mismatch",
    ]


def test_the_anchor_s_own_routing_may_change_but_nothing_else_on_it() -> None:
    state = at_step(2)
    assert _anchor(state)["on_success"] != SINK
    assert verify(state, 2) == []
    _anchor(state)["description"] = "Described."
    assert verify(state, 2) == ["original_llm_content_changed"]


def test_the_end_of_a_cycle_must_restore_the_original_routing_values() -> None:
    state = at_step(10)
    assert verify(state, 10) == []
    # The flow still reaches the sink, but under other route names.
    state["sources"][SOURCE]["on_success"] = "renamed"
    _anchor(state)["input"] = "renamed"
    assert verify(state, 10) == ["baseline_routing_not_restored"]


# ── the verifier: the turn ─────────────────────────────────────────────────


CLEAN = [{"phase": "end", "verdict": "clean", "publication_verdict": "ADMIT", "source": "model"}]


def operation(segments: list[State] | None = None, *, status: str = "completed", rejection: State | None = None) -> State:
    if status != "completed":
        return {"status": status, "result": None}
    return {
        "status": status,
        "result": {"message": {"segments": segments or [{"kind": "text", "content": "Done."}], "rejection": rejection}},
    }


@pytest.mark.parametrize(
    ("op", "reviews", "codes"),
    [
        (operation(), CLEAN, []),
        (operation(status="failed"), CLEAN, ["operation_not_completed", "operation_result_unavailable"]),
        (operation(rejection={"tool_name": "x"}), CLEAN, ["publication_rejected"]),
        (operation([{"kind": "text", "content": "  "}]), CLEAN, ["no_published_reply"]),
        (
            operation(
                [{"kind": "text", "content": "Done."}, {"kind": "trusted_system_notice", "content": "Checkpoint save status: saved"}]
            ),
            CLEAN,
            [],
        ),
        (
            operation([{"kind": "text", "content": "Done."}, {"kind": "trusted_system_notice", "content": "Review needed."}]),
            CLEAN,
            ["additional_system_notice_requires_review"],
        ),
        (operation(), [], ["end_review_not_clean_and_admitted"]),
        (operation(), [{**CLEAN[0], "verdict": "flagged"}], ["end_review_not_clean_and_admitted"]),
        (operation(), [{**CLEAN[0], "publication_verdict": "WITHHOLD"}], ["end_review_not_clean_and_admitted"]),
        # Only the last end review counts: a flagged pass followed by a clean one is clean.
        (operation(), [{**CLEAN[0], "verdict": "flagged"}, CLEAN[0]], []),
    ],
)
def test_each_turn_code_fires_on_its_own_failure(op: State, reviews: list[State], codes: list[str]) -> None:
    assert cycle.operation_codes(op, reviews, end_review_evidence="required") == (codes, [])


def test_if_recorded_lets_an_unrecorded_end_review_through_as_a_warning_but_not_a_bad_one() -> None:
    assert cycle.operation_codes(operation(), [], end_review_evidence="if-recorded") == ([], ["end_review_not_recorded"])
    flagged = [{**CLEAN[0], "verdict": "flagged"}]
    assert cycle.operation_codes(operation(), flagged, end_review_evidence="if-recorded") == (["end_review_not_clean_and_admitted"], [])


# ── a battery run of the workload ──────────────────────────────────────────


def test_an_obedient_ten_step_cycle_passes_every_step_and_leaves_the_fixture_as_found(tmp_path: Path) -> None:
    run, fake, composer = make(tmp_path)
    summary = run.run()
    assert summary["all_passed"] is True
    assert summary["line"] == "longest run in a row: 10 of 10"
    assert summary["as_found_differences"] == []
    assert summary["gold"] is False  # ten edits are too few for gold
    assert summary["baseline_graph_counts"] == {"sources": 1, "nodes": 1, "sinks": 2, "prompt_queries": 3}
    assert summary["end_reviews_recorded"] == 10
    expected = [message + SUFFIX for message in CYCLE_ONE]
    assert composer.messages == expected
    assert [message["content"] for message in fake.sessions[SESSION].messages if message["role"] == "user"] == expected
    first = results(run)[0]
    assert first["graph_counts"] == {"sources": 1, "nodes": 2, "sinks": 2}
    assert first["evidence"]["calls"][0]["request_byte_split"] == {"message_bytes": [900], "tools_bytes": 100, "total_bytes": 1000}
    assert first["evidence"]["end_reviews"] == CLEAN


def test_fifty_obedient_steps_are_gold(tmp_path: Path) -> None:
    run, _, _ = make(tmp_path, steps=50)
    summary = run.run()
    assert summary["gold"] is True
    assert summary["all_passed"] is True


def test_a_step_without_its_end_review_fails_by_default_and_stops_the_attempt(tmp_path: Path) -> None:
    composer = CycleComposer(orders_for(10))
    composer.end_review = {3: None}
    run, _, _ = make(tmp_path, composer=composer)
    summary = run.run()
    assert results(run)[2]["findings"] == ["end_review_not_clean_and_admitted"]
    assert summary["judged"] == 3
    assert summary["stopped_reason"] == "edit 3 (cycle-step-3) failed verification"
    assert summary["all_passed"] is False


def test_if_recorded_end_review_evidence_passes_a_step_whose_review_was_not_returned(tmp_path: Path) -> None:
    composer = CycleComposer(orders_for(10))
    composer.end_review = dict.fromkeys(range(1, 11))
    run, _, _ = make(tmp_path, composer=composer, end_review_evidence="if-recorded")
    summary = run.run()
    assert summary["all_passed"] is True
    assert results(run)[0]["warnings"] == ["end_review_not_recorded"]


def test_keep_going_records_a_failed_step_and_judges_the_rest(tmp_path: Path) -> None:
    composer = CycleComposer(orders_for(10))
    composer.skip = {3}
    run, _, _ = make(tmp_path, composer=composer, keep_going=True)
    summary = run.run()
    # The chain breaks at the missing node, so it also never reaches the sink (the customer's rule).
    assert results(run)[2]["findings"] == ["node_inventory_mismatch", "routing_does_not_reach_sink", "routing_mismatch"]
    assert summary["judged"] == 10
    assert summary["passed"] == 9
    assert summary["first_failure"] == 3
    assert summary["all_passed"] is False


def test_a_step_that_changes_the_llm_node_fails_with_the_customer_s_code(tmp_path: Path) -> None:
    composer = CycleComposer(orders_for(10))
    composer.extra = {2: _drop_anchor_option}
    run, _, _ = make(tmp_path, composer=composer)
    run.run()
    assert results(run)[1]["findings"] == ["original_llm_content_changed"]


def test_a_graph_that_is_not_the_fixture_is_refused_before_anything_is_sent(tmp_path: Path) -> None:
    state = fixture()
    _second_node(state)
    run, fake, composer = make(tmp_path, state=state)
    with pytest.raises(OperationFailed, match="exactly one node"):
        run.run()
    assert composer.turn == 0
    assert not [path for verb, path, _ in fake.requests if verb == "POST" and path.endswith("/messages")]


# ── command line ───────────────────────────────────────────────────────────


def test_the_plan_action_lists_the_requested_steps(capsys: pytest.CaptureFixture[str]) -> None:
    assert battery.main(["plan", "--workload", "structural-cycle", "--steps", "22"]) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]["name"] == "structural_cycle_22"
    planned = [event for event in events if event["event"] == "planned_edit"]
    assert len(planned) == 22
    assert planned[0]["message"] == CYCLE_ONE[0] + SUFFIX


def test_cleanup_is_refused_for_a_workload(tmp_path: Path) -> None:
    argv = ["cleanup", "--workload", "structural-cycle", "--session", SESSION, "--state-dir", str(tmp_path)]
    with pytest.raises(UsageError, match="fresh copy"):
        battery.main(argv)


def test_a_workload_run_exits_zero_only_when_every_requested_step_passed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_CYCLE_PASSWORD", "pw")

    def exit_code(composer: CycleComposer, attempt: str) -> int:
        fake = FakeDeployment(passwords={"tester": "pw"}, sessions={SESSION: Session(owner="tester", state=fixture())}, provider=composer)
        clock = Clock()
        argv = ["run", "--workload", "structural-cycle", "--steps", "10", "--base-url", BASE_URL, "--session", SESSION]
        argv += ["--account", "tester", "--password-env", "TEST_CYCLE_PASSWORD", "--state-dir", str(tmp_path / attempt)]
        argv += ["--attempt", attempt, "--execute"]
        return battery.main(argv, client_factory=lambda url: WebClient(url, transport=fake), sleep=clock.sleep, now=clock)

    assert exit_code(CycleComposer(orders_for(10)), "1") == 0
    failing = CycleComposer(orders_for(10))
    failing.skip = {10}
    assert exit_code(failing, "2") == 1
