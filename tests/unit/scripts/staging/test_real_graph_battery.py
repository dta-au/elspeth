"""Safety properties of scripts/staging/real_graph_battery.py, against an in-memory API.

The fake provider is "obedient": it applies each instruction's expected
passthrough chain to the graph, so an honest turn verifies and a test makes
it misbehave on purpose to prove the checks catch it.
"""

from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from scripts.staging import real_graph_battery as battery
from scripts.staging.opslib import AccountAuth, CheckpointStore, OperationFailed, WebClient, run_tool
from scripts.staging.repair_copied_prompt_reviews import checkpoint_name as review_checkpoint_name

from tests.unit.scripts.staging.fake_deployment import BASE_URL, FakeDeployment, Session, success_call

SESSION = "33333333-3333-4333-8333-333333333333"


def baseline() -> dict[str, Any]:
    return {
        "id": "state-0",
        "is_valid": True,
        "sources": {"rows": {"plugin": "csv", "on_success": "raw", "options": {"path": "/data/x.csv"}}},
        "nodes": [
            {
                "id": "head",
                "plugin": "llm",
                "node_type": "transform",
                "input": "raw",
                "on_success": "scored",
                "options": {"schema": {"a": 1}, "model": "m"},
            },
            {"id": "tail", "plugin": "passthrough", "node_type": "transform", "input": "scored", "on_success": "main", "options": {}},
        ],
        "edges": [{"from": "rows", "to": "head"}],
        "outputs": [{"name": "main", "options": {}}],
        "metadata": {"name": "copy"},
    }


def with_chain(base: dict[str, Any], order: list[str], version: int) -> dict[str, Any]:
    state = copy.deepcopy(base)
    state["id"] = f"state-{version}"
    route = "raw"
    state["sources"]["rows"]["on_success"] = route
    for name in order:
        state["nodes"].append(
            {"id": name, "plugin": "passthrough", "node_type": "transform", "input": route, "on_success": name + "_out", "options": {}}
        )
        route = name + "_out"
    head = next(node for node in state["nodes"] if node["id"] == "head")
    head["input"] = route
    state["edges"] = [{"display": version}]
    return state


class ObedientProvider:
    def __init__(self, base: dict[str, Any], *, cycles: int = 1) -> None:
        self.base = base
        self.orders = dict(battery.edit_instructions(base, cycles))
        self.turns = 0
        self.misbehave_on: int | None = None
        self.calls: list[dict[str, Any]] = [success_call()]

    def __call__(self, state: dict[str, Any], message: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        self.turns += 1
        if message in (battery.REPAIR_INSTRUCTION, battery.REVIEWED_REPAIR_INSTRUCTION):
            new = copy.deepcopy(state)
            new["id"] = f"state-{self.turns}"
        else:
            new = with_chain(self.base, self.orders[message], self.turns)
        if self.misbehave_on == self.turns:
            new["nodes"][0]["options"]["model"] = "silently-changed"
        return new, list(self.calls)


def make(tmp_path: Path, provider: ObedientProvider | None = None) -> tuple[battery.Battery, FakeDeployment, ObedientProvider]:
    provider = provider or ObedientProvider(baseline())
    fake = FakeDeployment(passwords={"tester": "pw"}, sessions={SESSION: Session(owner="tester", state=baseline())}, provider=provider)
    run = battery.Battery(
        client=WebClient(BASE_URL, transport=fake),
        auth=AccountAuth("tester"),
        store=CheckpointStore(tmp_path / "state", repo_root=tmp_path / "repo"),
        session_id=SESSION,
        attempt="1",
        sleep=lambda seconds: None,
        now=fake.clock,
        prompt_secret=lambda prompt: "pw",
    )
    return run, fake, provider


def submitted(fake: FakeDeployment) -> list[Any]:
    return list(fake.calls("POST", "/messages"))


def test_obedient_turns_all_verify_through_ordinary_messages_only(tmp_path: Path) -> None:
    run, fake, _provider = make(tmp_path)
    assert run.run(reviewed=False, cycles=1) is True
    assert len(submitted(fake)) == 11
    writes = {path for verb, path, _ in fake.requests if verb == "POST"}
    assert writes == {"/api/auth/login", "/api/auth/logout", f"/api/sessions/{SESSION}/messages"}


def test_changed_existing_content_stops_the_phase_and_the_attempt(tmp_path: Path) -> None:
    provider = ObedientProvider(baseline())
    provider.misbehave_on = 3
    run, fake, _ = make(tmp_path, provider)
    assert run.run(reviewed=False, cycles=1) is False
    assert len(submitted(fake)) == 3
    edits = run.store.read(battery.phase_name("1", "edits", SESSION))
    assert edits is not None and edits["stopped_reason"] == "turn_failed_verification"
    assert any("/nodes/0/options/model" in path for path in edits["steps"][-1]["difference_paths"])

    # Even if the graph is put right out of band, the stopped attempt must not resume.
    fake.sessions[SESSION].state["nodes"][0]["options"]["model"] = "m"
    assert run.run(reviewed=False, cycles=1) is False
    assert len(submitted(fake)) == 3, "a stopped attempt must not submit again"


def test_turn_without_a_successful_provider_call_fails(tmp_path: Path) -> None:
    provider = ObedientProvider(baseline())
    provider.calls = [{"_kind": "llm_call_audit", "call": {"status": "timeout", "latency_ms": None}}]
    run, _fake, _ = make(tmp_path, provider)
    assert run.run(reviewed=False, cycles=1) is False
    repair = run.store.read(battery.phase_name("1", "repair", SESSION))
    assert repair is not None
    assert "no_successful_provider_call" in repair["steps"][0]["publication_failures"]


def test_resume_polls_the_saved_operation_instead_of_resubmitting(tmp_path: Path) -> None:
    run, fake, _ = make(tmp_path)
    run.client.login("tester", "pw")
    state = run.state()
    operation = "44444444-4444-4444-8444-444444444444"
    run.client.post_json(
        f"/api/sessions/{SESSION}/messages", {"operation_id": operation, "state_id": state["id"], "content": battery.REPAIR_INSTRUCTION}
    )
    run.client.close()
    step = {
        "number": 1,
        "operation_id": operation,
        "base_state_id": state["id"],
        "message": battery.REPAIR_INSTRUCTION,
        "expected_order": [],
        "submitted": True,
    }
    run.store.write(
        battery.phase_name("1", "repair", SESSION),
        {"session_id": SESSION, "baseline": state, "last_state": state, "steps": [step], "complete": False},
    )
    before = len(submitted(fake))

    assert run.run(reviewed=False, cycles=1) is True
    repair_posts = [payload for payload in submitted(fake)[before:] if payload["content"] == battery.REPAIR_INSTRUCTION]
    assert repair_posts == []


def test_malformed_checkpoint_refuses(tmp_path: Path) -> None:
    run, fake, _ = make(tmp_path)
    run.store.write(battery.phase_name("1", "repair", SESSION), {"session_id": SESSION, "steps": [{"number": 1}]})
    with pytest.raises(OperationFailed, match="not a complete, valid checkpoint"):
        run.run(reviewed=False, cycles=1)
    assert submitted(fake) == []


def test_edits_require_a_complete_repair_matching_the_live_graph(tmp_path: Path) -> None:
    run, fake, _ = make(tmp_path)
    stale = baseline()
    stale["metadata"]["name"] = "older"
    run.store.write(
        battery.phase_name("1", "repair", SESSION),
        {"session_id": SESSION, "baseline": stale, "last_state": stale, "steps": [], "complete": True},
    )
    with pytest.raises(OperationFailed, match="complete repair phase"):
        run.run(reviewed=False, cycles=1)
    assert submitted(fake) == []


def test_run_reviewed_needs_a_completed_valid_review_repair(tmp_path: Path) -> None:
    run, fake, _ = make(tmp_path)
    with pytest.raises(OperationFailed, match="complete review repair"):
        run.run(reviewed=True, cycles=1)
    run.store.write(review_checkpoint_name(SESSION), {"complete": True, "validation": {"is_valid": True}})
    assert run.run(reviewed=True, cycles=1) is True
    assert len(submitted(fake)) == 11


def test_graph_checker_rejects_changes_and_accepts_the_honest_chain() -> None:
    base = baseline()
    battery.prove_checker(base)
    assert battery.check_graph(base, with_chain(base, ["x"], 1), ["x"]) == []
    assert battery.check_graph(base, with_chain(base, ["x"], 1), ["y"]) != []
    wrong_plugin = with_chain(base, ["x"], 1)
    wrong_plugin["nodes"][-1]["plugin"] = "llm"
    assert "/passthrough_wiring/x" in battery.check_graph(base, wrong_plugin, ["x"])
    invalid = copy.deepcopy(base)
    invalid["is_valid"] = False
    assert "/runtime_validation" in battery.check_graph(base, invalid, [])


def test_repair_turn_may_change_only_schema_declarations() -> None:
    base = baseline()
    schema_only = copy.deepcopy(base)
    schema_only["nodes"][0]["options"]["schema"] = {"a": 2}
    assert battery.check_graph(base, schema_only, [], repair=True) == []
    prompt_change = copy.deepcopy(base)
    prompt_change["nodes"][0]["options"]["model"] = "other"
    assert battery.check_graph(base, prompt_change, [], repair=True) != []


def test_token_refreshed_before_expiry(tmp_path: Path) -> None:
    run, fake, _ = make(tmp_path)
    fake.token_lifetime = 60.0
    assert run.run(reviewed=False, cycles=1) is True
    assert len(fake.calls("POST", "/api/auth/token")) == 11


def test_dry_run_contacts_nothing(tmp_path: Path) -> None:
    fake = FakeDeployment(passwords={})
    argv = ["run", "--base-url", BASE_URL, "--session", SESSION, "--account", "tester", "--state-dir", str(tmp_path / "state")]
    assert battery.main(argv, client_factory=lambda url: WebClient(url, transport=fake)) == 0
    assert fake.requests == []


@pytest.mark.parametrize("bad", ["http://deployment.invalid", "https://user:pw@deployment.invalid", "https://deployment.invalid/path"])
def test_base_url_must_be_a_bare_https_origin(bad: str, tmp_path: Path) -> None:
    argv = ["run", "--base-url", bad, "--session", SESSION, "--account", "tester", "--state-dir", str(tmp_path / "state"), "--execute"]
    assert run_tool(battery.main, argv) == 2


def test_help_needs_no_environment() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "scripts.staging.real_graph_battery", "--help"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(battery.__file__).resolve().parents[2])},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--execute" in result.stdout
