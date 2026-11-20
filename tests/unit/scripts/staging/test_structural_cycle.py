"""The structural-cycle workload: its wording, its plan, its fixture check, its verifier and a battery run of it.

The battery reaches the in-memory deployment only through a real
:class:`~scripts.staging.opslib.WebClient`. The fake Composer rewires the
graph to the order each step asks for, as an obedient Composer would.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
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
POS = cycle.Position(SOURCE, ANCHOR, SINK, True)
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


def position_of(names: dict[str, str]) -> cycle.Position:
    return cycle.Position(names["source"], names["anchor"], names["sink"], True)


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

    def __init__(self, orders: list[list[str]], positions: list[cycle.Position] | None = None) -> None:
        self.orders = orders
        # With positions, each turn rewires around its own position in a multi-node graph.
        self.positions = positions
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
            if self.positions is None:
                rewire(state, self.orders[number - 1])
            else:
                rewire_between(state, self.positions[number - 1], self.orders[number - 1])
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
    return [cycle.planned_step(number, POS)[1] for number in range(1, steps + 1)]


def make(
    tmp_path: Path,
    steps: int = 10,
    composer: CycleComposer | None = None,
    *,
    keep_going: bool = False,
    end_review_evidence: battery.EndReviewMode = "required",
    state: State | None = None,
    positions: list[cycle.Position] | None = None,
    end_review_view: bool = True,
    end_review_rows_omitted: int = 0,
) -> tuple[battery.EditBattery, FakeDeployment, CycleComposer]:
    composer = composer or CycleComposer(orders_for(steps))
    clock = Clock()
    fake = FakeDeployment(
        passwords={"tester": "pw"},
        sessions={SESSION: Session(owner="tester", state=state or fixture())},
        provider=composer,
        clock=clock,
        end_review_view=end_review_view,
        end_review_rows_omitted=end_review_rows_omitted,
    )
    run = battery.EditBattery(
        client=WebClient(BASE_URL, transport=fake),
        auth=AccountAuth(username="tester", password_env="TEST_CYCLE_PASSWORD"),
        store=CheckpointStore(tmp_path / "state"),
        session_id=SESSION,
        attempt="1",
        plan=cycle.structural_cycle_plan(steps=steps, positions=positions or [POS]),
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
    plan = cycle.structural_cycle_plan(steps=10, positions=[POS])
    edits = resolve_edits(plan, {}, fixture())
    assert [edit["message"] for edit in edits] == [text + SUFFIX for text in CYCLE_ONE]
    assert [edit["order"] for edit in edits] == CYCLE_ONE_ORDERS


def test_later_cycles_name_their_nodes_by_cycle_and_sixty_steps_end_on_a_restore() -> None:
    plan = cycle.structural_cycle_plan(steps=60, positions=[POS])
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
    message = resolve_edits(cycle.structural_cycle_plan(steps=10, positions=[POS]), {}, state)[9]["message"]
    assert 'Restore source.on_success="into llm", capacity_probe.input="into llm", and capacity_probe.on_success="results".' in message


@pytest.mark.parametrize("steps", [0, 61])
def test_a_step_count_outside_one_to_sixty_is_refused(steps: int) -> None:
    with pytest.raises(UsageError, match="between 1 and 60"):
        cycle.structural_cycle_plan(steps=steps, positions=[POS])


@pytest.mark.parametrize("names", [{**NAMES, "anchor": "a/b"}, {**NAMES, "sink": "{x}"}, {**NAMES, "sink": SOURCE}])
def test_component_names_must_be_plain_and_distinct(names: dict[str, str]) -> None:
    with pytest.raises(UsageError):
        cycle.structural_cycle_plan(steps=10, positions=[position_of(names)])


def test_the_plan_identity_changes_with_names_and_step_count() -> None:
    base = cycle.structural_cycle_plan(steps=25, positions=[POS])
    assert base.name == "structural_cycle_25"
    assert base.sha256 == cycle.structural_cycle_plan(steps=25, positions=[POS]).sha256
    assert base.sha256 != cycle.structural_cycle_plan(steps=22, positions=[POS]).sha256
    assert base.sha256 != cycle.structural_cycle_plan(steps=25, positions=[position_of({**NAMES, "anchor": "other"})]).sha256


# ── the fixture ────────────────────────────────────────────────────────────


def test_the_fixture_is_counted() -> None:
    assert cycle.check_fixture(fixture(queries=32), positions=[POS]) == {
        "sources": 1,
        "nodes": 1,
        "llm_nodes": 1,
        "sinks": 2,
        "prompt_queries": 32,
    }


def _no_anchor(state: State) -> None:
    state["nodes"][0]["id"] = "renamed"


def _cycle_leftover(state: State) -> None:
    rewire(state, ["compact_before_a", ANCHOR])


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
        (_no_anchor, "not a transform node"),
        (_no_sink, "not an output"),
        (_cycle_leftover, "already holds nodes a cycle adds"),
        (_invalid, "validate the fixture"),
        (_not_direct, "straight through"),
    ],
)
def test_a_graph_that_is_not_the_fixture_is_refused(damage: Any, match: str) -> None:
    state = fixture()
    damage(state)
    with pytest.raises(OperationFailed, match=match):
        cycle.check_fixture(state, positions=[POS])


# ── the verifier: every code with a failing graph ──────────────────────────


def at_step(number: int) -> State:
    state = fixture()
    rewire(state, CYCLE_ONE_ORDERS[number - 1])
    return state


def verify(state: State, number: int) -> list[str]:
    return cycle.verify_state(fixture(), state, CYCLE_ONE_ORDERS[number - 1], POS)


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
    assert summary["baseline_graph_counts"] == {"sources": 1, "nodes": 1, "llm_nodes": 1, "sinks": 2, "prompt_queries": 3}
    assert summary["end_reviews_recorded"] == 10
    assert summary["end_reviews"]["line"] == "end reviews: 10 of 10 steps recorded one, 10 clean and admitted"
    assert summary["end_reviews"]["verified"] is True
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


def test_a_step_without_its_end_review_fails_under_required_and_stops_the_attempt(tmp_path: Path) -> None:
    composer = CycleComposer(orders_for(10))
    composer.end_review = {3: None}
    run, _, _ = make(tmp_path, composer=composer, end_review_evidence="required")
    summary = run.run()
    assert results(run)[2]["findings"] == ["end_review_not_clean_and_admitted"]
    assert summary["judged"] == 3
    assert summary["stopped_reason"] == "edit 3 (cycle-step-3) failed verification"
    assert summary["all_passed"] is False


def test_if_recorded_end_review_evidence_passes_a_step_whose_review_was_not_returned(tmp_path: Path) -> None:
    composer = CycleComposer(orders_for(10))
    composer.end_review = dict.fromkeys(range(1, 11))
    run, _, _ = make(tmp_path, composer=composer, end_review_evidence="if-recorded", end_review_view=False)
    summary = run.run()
    assert summary["all_passed"] is True
    assert results(run)[0]["warnings"] == ["end_review_not_recorded"]
    # A run that saw no end review never reads as clean end reviews.
    assert summary["end_reviews"]["line"] == "end reviews: none exposed by this server; not verified"
    assert summary["end_reviews"]["verified"] is False
    assert summary["end_reviews"]["steps_not_verified"] == 10


def test_the_summary_counts_steps_whose_end_review_was_not_returned(tmp_path: Path) -> None:
    composer = CycleComposer(orders_for(10))
    composer.end_review = {2: None, 7: None}
    run, _, _ = make(tmp_path, composer=composer, end_review_evidence="if-recorded")
    review = run.run()["end_reviews"]
    assert review["line"] == "end reviews: 8 of 10 steps recorded one, 8 clean and admitted; 2 steps not verified"
    assert (review["verified"], review["steps_with_end_review"]) == (False, 8)


def test_auto_is_the_default_end_review_evidence() -> None:
    args = battery.build_parser().parse_args(["plan", "--workload", "structural-cycle"])
    assert args.end_review_evidence == "auto"
    assert next(field for field in dataclasses.fields(battery.EditBattery) if field.name == "end_review_evidence").default == "auto"


def test_auto_requires_end_reviews_from_a_server_with_the_end_review_view(tmp_path: Path) -> None:
    composer = CycleComposer(orders_for(10))
    composer.end_review = {3: None}
    run, _, _ = make(tmp_path, composer=composer, end_review_evidence="auto")
    summary = run.run()
    assert results(run)[2]["findings"] == ["end_review_not_clean_and_admitted"]
    assert results(run)[2]["end_review_view"] == {"exposed": True, "rows_omitted": 0}
    assert summary["stopped_reason"] == "edit 3 (cycle-step-3) failed verification"
    assert summary["end_reviews"]["server_exposes_end_reviews"] is True


def test_auto_falls_back_to_if_recorded_on_a_server_without_the_view(tmp_path: Path) -> None:
    # The Composer records every end review, but an older server never returns them.
    run, _, _ = make(tmp_path, end_review_evidence="auto", end_review_view=False)
    summary = run.run()
    assert summary["all_passed"] is True
    assert results(run)[0]["warnings"] == ["end_review_not_recorded"]
    assert results(run)[0]["end_review_view"] == {"exposed": False, "rows_omitted": 0}
    assert summary["end_reviews"]["line"] == "end reviews: none exposed by this server; not verified"
    assert summary["end_reviews"]["server_exposes_end_reviews"] is False
    assert summary["end_reviews"]["verified"] is False


def test_a_server_with_the_view_but_no_reviews_is_not_reported_as_having_none_exposed(tmp_path: Path) -> None:
    composer = CycleComposer(orders_for(10))
    composer.end_review = dict.fromkeys(range(1, 11))
    run, _, _ = make(tmp_path, composer=composer, end_review_evidence="if-recorded")
    review = run.run()["end_reviews"]
    assert review["line"] == "end reviews: 0 of 10 steps recorded one, 0 clean and admitted; 10 steps not verified"
    assert review["server_exposes_end_reviews"] is True


def test_the_summary_reports_end_review_rows_the_server_left_out(tmp_path: Path) -> None:
    run, _, _ = make(tmp_path, end_review_evidence="auto", end_review_rows_omitted=2)
    review = run.run()["end_reviews"]
    assert review["line"] == (
        "end reviews: 10 of 10 steps recorded one, 10 clean and admitted; the server left out 2 end review rows it could not read"
    )
    assert review["rows_omitted_by_server"] == 2


@pytest.mark.parametrize("headers", [{"X-Elspeth-End-Review-Rows-Omitted": "two"}, {"X-Elspeth-End-Review-Rows-Omitted": "-1"}])
def test_a_malformed_end_review_header_stops_the_run(tmp_path: Path, headers: dict[str, str]) -> None:
    run, _, _ = make(tmp_path, end_review_evidence="auto")
    with pytest.raises(OperationFailed, match="X-Elspeth-End-Review-Rows-Omitted"):
        run.note_end_review_view(headers)


def test_the_header_name_is_read_in_any_case(tmp_path: Path) -> None:
    run, _, _ = make(tmp_path, end_review_evidence="auto")
    run.note_end_review_view({"X-ELSPETH-END-REVIEW-ROWS-OMITTED": "3"})
    assert (run.server_end_review_view, run.end_review_rows_omitted, run.resolved_end_review_evidence()) == (True, 3, "required")
    run.note_end_review_view({})
    assert (run.server_end_review_view, run.end_review_rows_omitted, run.resolved_end_review_evidence()) == (False, 0, "if-recorded")


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
    _cycle_leftover(state)
    run, fake, composer = make(tmp_path, state=state)
    with pytest.raises(OperationFailed, match="already holds nodes"):
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


# ── a multi-node graph: generalised verifier and rotated positions ─────────

# src -> step_a (llm) -> middle (llm) -> step_b (llm) -> tail (field_mapper) -> out, with an error sink.
FIRST = cycle.Position("src", "step_a", "middle", False)
MID = cycle.Position("step_a", "middle", "step_b", False)
LAST = cycle.Position("step_b", "tail", "out", True)
_CYCLE_NODE = re.compile(r"(compact|cycle[0-9]+)_(before|after)_[ab]")


def chain() -> State:
    def node(node_id: str, plugin: str, route_in: str, route_out: str) -> State:
        return {
            "id": node_id,
            "node_type": "transform",
            "plugin": plugin,
            "input": route_in,
            "on_success": route_out,
            "on_error": "errors",
            "options": {"schema": {"mode": "observed"}, "note": f"{node_id} options"},
            "description": None,
        }

    return {
        "id": "state-0",
        "version": 1,
        "sources": {"src": {"plugin": "csv", "on_success": "r0", "options": {"path": "rows.csv"}, "on_validation_failure": "errors"}},
        "nodes": [
            node("step_a", "llm", "r0", "r1"),
            node("middle", "llm", "r1", "r2"),
            node("step_b", "llm", "r2", "r3"),
            node("tail", "field_mapper", "r3", "out"),
        ],
        "edges": [],
        "outputs": [
            {"name": "out", "plugin": "json", "options": {"path": "out.json"}, "on_write_failure": "discard"},
            {"name": "errors", "plugin": "json", "options": {"path": "errors.json"}, "on_write_failure": "discard"},
        ],
        "metadata": {"name": "Chain", "description": ""},
        "is_valid": True,
        "validation_errors": None,
    }


def rewire_between(state: State, position: cycle.Position, order: list[str]) -> None:
    """Leave exactly ``order`` between the position's upstream and downstream, keeping every other node as it is."""
    nodes = {node["id"]: node for node in state["nodes"] if not _CYCLE_NODE.fullmatch(node["id"]) or node["id"] in order}
    for name in order:
        if name not in nodes:
            nodes[name] = passthrough(name)
    if position.upstream in state["sources"]:
        route = state["sources"][position.upstream]["on_success"]
    else:
        route = nodes[position.upstream]["on_success"]
    target = position.downstream if position.downstream_is_sink else nodes[position.downstream]["input"]
    for index, name in enumerate(order):
        nodes[name]["input"] = route
        route = target if index == len(order) - 1 else f"{name}_out"
        nodes[name]["on_success"] = route
    state["nodes"] = list(nodes.values())


def chain_at(position: cycle.Position, number: int) -> State:
    state = chain()
    rewire_between(state, position, cycle.planned_step(number, position)[1])
    return state


def verify_chain(state: State, position: cycle.Position, number: int) -> list[str]:
    return cycle.verify_state(chain(), state, cycle.planned_step(number, position)[1], position)


@pytest.mark.parametrize("position", [FIRST, MID, LAST], ids=["after-source", "mid-chain", "before-sink"])
@pytest.mark.parametrize("number", range(1, 11))
def test_every_step_passes_at_every_position_of_a_multi_node_graph(position: cycle.Position, number: int) -> None:
    assert verify_chain(chain_at(position, number), position, number) == []


def test_a_downstream_node_may_take_the_new_route_instead_of_the_anchor_keeping_its_own() -> None:
    state = chain_at(MID, 2)
    after = next(node for node in state["nodes"] if node["id"] == "compact_after_a")
    after["on_success"] = "renamed"
    next(node for node in state["nodes"] if node["id"] == "step_b")["input"] = "renamed"
    assert verify_chain(state, MID, 2) == []


def _node(state: State, node_id: str) -> State:
    found: State = next(node for node in state["nodes"] if node["id"] == node_id)
    return found


@pytest.mark.parametrize(
    ("damage", "codes"),
    [
        (lambda state: state["nodes"].append(passthrough("stray")), ["node_inventory_mismatch"]),
        # A node the step did not name, and a named one outside the wiring it may touch.
        (lambda state: _node(state, "tail")["options"].update(note="changed"), ["original_llm_content_changed"]),
        (lambda state: _node(state, "step_b")["options"].update(note="changed"), ["original_llm_content_changed"]),
        (lambda state: _node(state, "step_a").update(on_error="discard"), ["original_llm_content_changed"]),
        (lambda state: state["sources"]["src"].update(on_success="moved"), ["source_content_changed"]),
        (lambda state: state["outputs"][0]["options"].update(path="elsewhere.json"), ["sink_content_changed"]),
        (lambda state: state["metadata"].update(name="Renamed"), ["metadata_changed"]),
        (lambda state: _node(state, "compact_after_a").update(plugin="llm"), ["unexpected_auxiliary_plugin"]),
        (lambda state: _node(state, "middle").update(input="nowhere"), ["routing_does_not_reach_sink", "routing_mismatch"]),
        (lambda state: _node(state, "compact_after_a").update(on_success="nowhere"), ["routing_does_not_reach_sink"]),
        (lambda state: state.update(is_valid=False), ["runtime_validation_failed"]),
    ],
)
def test_each_state_code_fires_on_a_multi_node_graph(damage: Any, codes: list[str]) -> None:
    state = chain_at(MID, 2)
    damage(state)
    assert verify_chain(state, MID, 2) == codes


def test_rewiring_a_connection_the_step_did_not_name_is_reported_even_when_the_graph_stays_connected() -> None:
    state = chain_at(MID, 2)
    _node(state, "step_b")["on_success"] = "renamed"
    _node(state, "tail")["input"] = "renamed"
    assert verify_chain(state, MID, 2) == ["original_llm_content_changed"]


def test_the_end_of_a_cycle_must_restore_the_route_names_around_a_mid_chain_anchor() -> None:
    state = chain_at(MID, 10)
    assert verify_chain(state, MID, 10) == []
    _node(state, "step_a")["on_success"] = "renamed"
    _node(state, "middle")["input"] = "renamed"
    assert verify_chain(state, MID, 10) == ["baseline_routing_not_restored"]


def test_a_node_downstream_is_named_without_the_word_sink_and_a_sink_with_it() -> None:
    assert cycle.planned_step(2, MID)[0].startswith("Add a passthrough transform named compact_after_a between middle and step_b.")
    assert cycle.planned_step(2, LAST)[0].startswith("Add a passthrough transform named compact_after_a between tail and the out sink.")


def test_positions_must_be_wired_straight_through_in_the_graph() -> None:
    assert cycle.check_fixture(chain(), positions=[FIRST, MID, LAST])["llm_nodes"] == 3
    with pytest.raises(OperationFailed, match="straight through"):
        cycle.check_fixture(chain(), positions=[cycle.Position("src", "middle", "step_b", False)])
    with pytest.raises(OperationFailed, match="not a node"):
        cycle.check_fixture(chain(), positions=[cycle.Position("step_a", "middle", "out", False)])


def test_a_battery_run_rotates_its_cycles_through_the_positions_and_leaves_the_graph_as_found(tmp_path: Path) -> None:
    positions = [FIRST, MID, LAST]
    plan = cycle.structural_cycle_plan(steps=30, positions=positions)
    turns = [cycle.Position.from_json(edit["position"]) for edit in plan.edits]
    assert turns == [FIRST] * 10 + [MID] * 10 + [LAST] * 10
    composer = CycleComposer([edit["order"] for edit in plan.edits], positions=turns)
    run, _, _ = make(tmp_path, steps=30, composer=composer, state=chain(), positions=positions)
    summary = run.run()
    assert summary["all_passed"] is True
    assert summary["as_found_differences"] == []
    assert "between step_a and middle" in composer.messages[10]
