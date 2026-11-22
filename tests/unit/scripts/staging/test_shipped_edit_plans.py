"""The two shipped edit plans, executed end to end against a Composer that does exactly what each edit asks.

The simulated Composer reads each resolved edit's expectations and makes
precisely that change and nothing else (new prompt files for file-held
prompts, a pending review card for every prompt change). A plan that passes
here is executable: every edit's change is false before it is sent, every
verdict can pass, approvals settle, and the graph is left as found. It does not
show that a real model will make the change; the battery measures that.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from scripts.staging import composer_edit_battery as battery
from scripts.staging.edit_plan import (
    DEFAULT_PLAN,
    MIXED_PLAN,
    as_found_differences,
    graph_view,
    load_plan,
    resolve_edits,
    resolve_targets,
    value_at,
)
from scripts.staging.opslib import AccountAuth, CheckpointStore, WebClient
from scripts.staging.repair_copied_prompt_reviews import prompt_surface

from tests.unit.scripts.staging.fake_deployment import BASE_URL, Blob, FakeDeployment, FakeTurn, Session

SESSION = "44444444-4444-4444-8444-444444444444"
PROMPT = b"Score the review {{ row['review'] }} from 1 to 5.\n"
type State = dict[str, Any]


def file_marker(blob: Blob) -> State:
    return {"blob_ref": blob.id, "mode": "inline_content", "sha256": hashlib.sha256(blob.data).hexdigest()}


def graph(*, inline: bool, optional_roles: bool) -> tuple[State, dict[str, Blob]]:
    blob = Blob(id="prompt-file", filename="score.txt", data=PROMPT)
    score_options: State = {"prompt_template": file_marker(blob), "model": "m"}
    if optional_roles:
        score_options["api_key"] = {"secret_ref": "MODEL_KEY"}
    nodes: list[State] = [
        {
            "id": "clean",
            "node_type": "transform",
            "plugin": "passthrough",
            "input": "r0",
            "on_success": "r1",
            "on_error": "discard",
            "options": {},
        },
        {
            "id": "score",
            "node_type": "transform",
            "plugin": "llm",
            "input": "r1",
            "on_success": "r2",
            "on_error": "discard",
            "options": score_options,
        },
    ]
    if inline:
        nodes.append(
            {
                "id": "classify",
                "node_type": "transform",
                "plugin": "llm",
                "input": "r2",
                "on_success": "main",
                "on_error": "discard",
                "options": {"prompt_template": "Classify the row.", "model": "m"},
                "description": "Classifier.",
            }
        )
    else:
        nodes[1]["on_success"] = "main"
    for item in nodes:
        if "description" not in item:
            item["description"] = None
    state: State = {
        "id": "state-0",
        "sources": {
            "rows": {
                "plugin": "csv" if optional_roles else "json",
                "on_success": "r0",
                "options": {"path": "in.csv"},
                "on_validation_failure": "discard",
            }
        },
        "nodes": nodes,
        "edges": [{"id": "e1", "from_node": "rows", "to_node": "clean", "edge_type": "on_success", "label": None}],
        "outputs": [
            {"name": "main", "plugin": "json", "options": {"path": "main.jsonl"}, "on_write_failure": "discard", "description": None}
        ],
        "metadata": {"name": "Large graph", "description": None},
        "is_valid": True,
        "validation_errors": None,
    }
    return state, {blob.id: blob}


class PlanComposer:
    """Makes exactly the change each resolved edit expects, and nothing else."""

    def __init__(self, edits: list[State], baseline: State, session: Session) -> None:
        self.by_message = {edit["message"]: edit for edit in edits if edit["applicable"]}
        self.baseline = graph_view(baseline)
        self.session = session
        self.turn = 0

    def __call__(self, state: State, message: str) -> FakeTurn:
        self.turn += 1
        edit = self.by_message[message]
        before = prompt_surface_by_node(state)
        for expectation in edit["expect"]:
            self.apply(state, expectation)
        reviews: dict[str, Any] = {}
        for node in state["nodes"]:
            if node["id"] in before and prompt_surface(node["options"]) != before[node["id"]]:
                reviews.update(self.review(node))
        state["id"] = f"state-{self.turn}"
        tools = [*edit["tools_all"], *edit["tools_any"][:1]]
        calls = [
            {"id": f"c{self.turn}-{i}", "type": "function", "function": {"name": t, "arguments": "{}"}, "outcome": "applied"}
            for i, t in enumerate(tools)
        ]
        audit = [
            {
                "_kind": "llm_call_audit",
                "call": {
                    "status": "success",
                    "latency_ms": 900,
                    "started_at": "2026-01-01T00:00:01+00:00",
                    "finished_at": "2026-01-01T00:00:02+00:00",
                    "context_budget": None,
                },
            }
        ]
        return FakeTurn(state=state, audit=audit, assistant_tool_calls=calls, reviews=reviews)

    # -- changes ------------------------------------------------------------

    def apply(self, state: State, expectation: Mapping[str, Any]) -> None:
        if "renamed_node" in expectation:
            node(state, expectation["renamed_node"]["from"])["id"] = expectation["renamed_node"]["to"]
        elif "new_node" in expectation:
            item: State = {
                "id": expectation["new_node"],
                "input": "",
                "on_success": None,
                "on_error": None,
                "options": {},
                "description": None,
            }
            item.update(expectation["fields"])
            state["nodes"].append(item)
        elif "new_output" in expectation:
            options = {key: rule["equals"] if "equals" in rule else rule["ends_with"] for key, rule in expectation["options"].items()}
            state["outputs"].append({"name": expectation["new_output"], "options": options, "description": None, **expectation["fields"]})
        elif "absent" in expectation:
            set_at(state, expectation["absent"], None, delete=True)
        elif "equals" in expectation or "ends_with" in expectation:
            set_at(
                state, expectation["equals"] if "equals" in expectation else expectation["ends_with"], copy.deepcopy(expectation["value"])
            )
        elif "restored" in expectation:
            found, was = value_at(self.baseline, expectation["restored"])
            set_at(state, expectation["restored"], copy.deepcopy(was), delete=not found)
        elif "chain" in expectation:
            rewire(state, expectation["chain"])
        elif "text" in expectation:
            self.change_text(state, expectation)
        else:
            raise AssertionError(f"no simulation for {expectation}")

    def change_text(self, state: State, expectation: Mapping[str, Any]) -> None:
        options = owner(state, expectation["text"])
        current = options["prompt_template"]
        text = current if type(current) is str else self.session.blobs[current["blob_ref"]].data.decode()
        if "append" in expectation:
            text = text.rstrip() + "\n" + expectation["append"]
        elif "replace" in expectation:
            text = text.replace(*expectation["replace"])
        else:
            text = text.replace(expectation["remove"], "")
        if expectation["form"] == "inline":
            options["prompt_template"] = text
            return
        blob = Blob(id=f"prompt-file-{self.turn}", filename=f"battery_{self.turn}.txt", data=text.encode())
        self.session.blobs[blob.id] = blob
        options["prompt_template"] = file_marker(blob)

    def review(self, item: State) -> dict[str, Any]:
        options = item["options"]
        rows = options["interpretation_requirements"] if "interpretation_requirements" in options else []
        rows.append({"id": f"req-{self.turn}", "kind": "llm_prompt_template", "status": "pending"})
        options["interpretation_requirements"] = rows
        surface = prompt_surface(options)
        bindings = [
            {"blob_id": value["blob_ref"], "text": self.session.blobs[value["blob_ref"]].data.decode()}
            for value in surface.values()
            if type(value) is dict and "blob_ref" in value
        ]
        event_id = f"review-{self.turn}"
        return {
            event_id: {
                "event": {"id": event_id, "kind": "llm_prompt_template"},
                "snapshot": {"node_id": item["id"], "anchor": f"anchor-{self.turn}", "authored_surface": surface, "bindings": bindings},
                "pending": True,
            }
        }


def prompt_surface_by_node(state: State) -> dict[str, State]:
    return {item["id"]: prompt_surface(item["options"]) for item in state["nodes"]}


def node(state: State, node_id: str) -> State:
    found: State = next(item for item in state["nodes"] if item["id"] == node_id)
    return found


def _parts(pointer: str) -> list[str]:
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def _container(state: State, parts: list[str]) -> tuple[Any, list[str]]:
    head = parts[0]
    if head == "nodes":
        return node(state, parts[1]), parts[2:]
    if head == "outputs":
        return next(item for item in state["outputs"] if item["name"] == parts[1]), parts[2:]
    if head == "sources":
        return state["sources"][parts[1]], parts[2:]
    return state["metadata"], parts[1:]


def owner(state: State, pointer: str) -> State:
    container, rest = _container(state, _parts(pointer))
    for part in rest[:-1]:
        container = container[part]
    found: State = container
    return found


def set_at(state: State, pointer: str, value: Any, *, delete: bool = False) -> None:
    parts = _parts(pointer)
    if len(parts) == 2 and parts[0] in ("nodes", "outputs") and delete:
        key = "id" if parts[0] == "nodes" else "name"
        state[parts[0]] = [item for item in state[parts[0]] if item[key] != parts[1]]
        return
    container, rest = _container(state, parts)
    for part in rest[:-1]:
        container = container[part]
    if delete:
        if rest[-1] in ("description",):
            container[rest[-1]] = None
        elif rest[-1] in container:
            del container[rest[-1]]
    else:
        container[rest[-1]] = value


def rewire(state: State, chain: Mapping[str, Any]) -> None:
    start = chain["from"]
    route = state["sources"][start]["on_success"] if start in state["sources"] else node(state, start)["on_success"]
    for step in chain["via"]:
        item = node(state, step)
        item["input"] = route
        item["on_success"] = route = f"{step}_out"
    node(state, chain["to"])["input"] = route


def run_plan(
    tmp_path: Path, plan_path: Path, *, inline: bool, optional_roles: bool
) -> tuple[State, State, battery.EditBattery, FakeDeployment]:
    plan = load_plan(plan_path)
    state, blobs = graph(inline=inline, optional_roles=optional_roles)
    baseline = copy.deepcopy(state)
    overrides = {"spare_secret": "SPARE_KEY"} if optional_roles else {}
    session = Session(owner="tester", state=state, blobs=blobs)
    edits = resolve_edits(plan, resolve_targets(plan, baseline, overrides), baseline)
    fake = FakeDeployment(passwords={"tester": "pw"}, sessions={SESSION: session})
    fake.provider = PlanComposer(edits, baseline, session)
    run = battery.EditBattery(
        client=WebClient(BASE_URL, transport=fake),
        auth=AccountAuth(username="tester", password_env="TEST_BATTERY_PASSWORD"),
        store=CheckpointStore(tmp_path / "state"),
        session_id=SESSION,
        attempt="1",
        plan=plan,
        overrides=overrides,
        sleep=lambda _seconds: None,
        now=fake.clock,
    )
    summary = run.run()
    return summary, baseline, run, fake


@pytest.fixture(autouse=True)
def _password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_BATTERY_PASSWORD", "pw")


@pytest.mark.parametrize(("plan_path", "inline"), [(DEFAULT_PLAN, False), (MIXED_PLAN, True)])
@pytest.mark.parametrize("optional_roles", [False, True])
def test_a_shipped_plan_can_be_gold_and_leaves_the_graph_as_found(
    tmp_path: Path, plan_path: Path, inline: bool, optional_roles: bool
) -> None:
    summary, baseline, run, fake = run_plan(tmp_path, plan_path, inline=inline, optional_roles=optional_roles)
    checkpoint = run.store.read(run.name)
    assert checkpoint is not None
    failed = [(result["id"], result["findings"]) for result in checkpoint["results"] if "passed" in result and not result["passed"]]
    assert failed == []
    assert summary["gold"] is True
    assert summary["applicable"] == (62 if optional_roles else 58)
    assert len(summary["skipped_not_applicable"]) == (0 if optional_roles else 4)
    assert summary["as_found_differences"] == []
    # Prompt files the plan made are named in the summary, with whether the final graph still uses each.
    left = summary["files_left_in_session"]
    session = fake.sessions[SESSION]
    assert left, "the plan's file rounds create prompt files"
    assert {item["id"] for item in left} == set(session.blobs) - {"prompt-file"}
    assert all(item["filename"].startswith("battery_") for item in left)
    assert {item["id"] for item in left if item["referenced"]} == battery.referenced_files(session.state) - {"prompt-file"}

    def read_text(marker: Mapping[str, Any]) -> str:
        return fake.sessions[SESSION].blobs[marker["blob_ref"]].data.decode()

    assert as_found_differences(baseline, fake.sessions[SESSION].state, read_text) == []


def test_the_all_files_plan_has_no_inline_prompt_role_and_the_mixed_plan_has_one() -> None:
    gold, mixed = load_plan(DEFAULT_PLAN), load_plan(MIXED_PLAN)
    assert "llm_inline" not in gold.targets
    assert mixed.targets["llm_inline"] == {"kind": "llm_node", "prompt": "inline"}
    assert sum(1 for edit in json.loads(DEFAULT_PLAN.read_text())["edits"] if "approve" in edit) == 5


@pytest.mark.parametrize("plan_path", [DEFAULT_PLAN, MIXED_PLAN])
def test_every_shipped_edit_names_its_tools_and_no_message_names_a_tool(plan_path: Path) -> None:
    plan = load_plan(plan_path)
    assert all("tools_any" in edit or "tools_all" in edit for edit in plan.edits)
    assert not [edit["id"] for edit in plan.edits if any(tool in edit["message"] for tool in battery.EDITING_TOOLS)]
    coverage = battery.plan_coverage(plan)
    for tool in ("upsert_edge", "remove_edge", "patch_source_options", "create_blob", "wire_blob_inline_ref", "wire_secret_ref"):
        assert tool in coverage["exercised"]


SHIPPED_PLANS = ("composer_file_bindings.json", "composer_gold_50.json", "composer_mixed_50.json", "composer_prompt_edits_50.json")


def test_the_shipped_plans_are_the_ones_these_tests_run() -> None:
    """A new plan file must be added here, and run end to end by a test (the prompt plan's is test_prompt_edit_plan.py)."""
    assert tuple(sorted(path.name for path in DEFAULT_PLAN.parent.glob("*.json"))) == SHIPPED_PLANS


@pytest.mark.parametrize("name", SHIPPED_PLANS)
def test_every_shipped_plan_loads_and_no_message_names_a_tool(name: str) -> None:
    plan = load_plan(DEFAULT_PLAN.parent / name)
    assert not [edit["id"] for edit in plan.edits if any(tool in edit["message"] for tool in battery.EDITING_TOOLS)]
    assert plan.forbidden_tools == ("set_pipeline",)


@pytest.mark.parametrize("plan_path", [DEFAULT_PLAN, MIXED_PLAN])
def test_every_new_output_edit_allows_the_collision_policy_set_output_requires(plan_path: Path) -> None:
    outputs = [expectation for edit in load_plan(plan_path).edits for expectation in edit["expect"] if "new_output" in expectation]
    assert outputs
    assert all("collision_policy" in expectation["option_keys"] for expectation in outputs)


@pytest.mark.parametrize("plan_path", [DEFAULT_PLAN, MIXED_PLAN])
def test_on_the_acceptance_shape_profile_owned_edits_are_skipped_and_gold_stays_in_reach(plan_path: Path) -> None:
    """The acceptance fixture's shape: every LLM step bound to an operator profile, a csv source, no secret reference.

    The profile owns max_tokens and temperature, so those eight edits are skipped (``profile_owned``).
    Gold needs GOLD_MINIMUM_EDITS applicable edits; the plan keeps a margin of 2 so that a later
    plan change cannot silently put gold out of reach on this graph.
    """
    plan = load_plan(plan_path)
    state, _ = graph(inline=plan_path == MIXED_PLAN, optional_roles=True)
    for item in state["nodes"]:
        if item["plugin"] == "llm":
            item["options"]["profile"] = "task-role"
            if "api_key" in item["options"]:
                del item["options"]["api_key"]
    edits = resolve_edits(plan, resolve_targets(plan, state, {}), state)
    owned = [edit["id"] for edit in edits if "profile_owned" in edit]
    assert owned == [
        "max-tokens",
        "temperature",
        "restore-max-tokens",
        "restore-temperature",
        "max-tokens-2",
        "temperature-2",
        "restore-max-tokens-2",
        "restore-temperature-2",
    ]
    assert [edit["id"] for edit in edits if not edit["applicable"] and "profile_owned" not in edit] == ["secret-switch", "secret-restore"]
    assert sum(1 for edit in edits if edit["applicable"]) >= battery.GOLD_MINIMUM_EDITS + 2
