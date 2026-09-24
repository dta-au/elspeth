"""Exercise battery pause/resume over the real offline scenario oracles."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest
from scripts.composer_acceptance import runner
from scripts.composer_acceptance.runner import ApiClient, _preview_seen, compose, run_case
from scripts.composer_acceptance.runner import assert_frozen as measured_assert_frozen
from scripts.composer_acceptance.runner import runtime_evidence as measured_runtime_evidence
from scripts.composer_acceptance.serve import ACCEPTANCE_PLUGIN_ALLOWLIST, check_fixture_availability, validate_resume

from tests.unit.scripts.test_composer_convergence_checks import CASES, _evidence, _positive


class ScenarioApi(ApiClient):
    def __init__(self, case: dict):
        super().__init__("http://127.0.0.1:18473")
        self.case = case
        self.state, self.run, self.outputs = _positive(case)
        self.state.update(session_id="session-test", version=1, edges=[])
        self.sent: list[tuple[str, dict | None]] = []
        self.pending = False
        self.approved = False
        self.turns = 0

    def json(self, path: str, body: dict | None = None):
        self.sent.append((path, body))
        if path == "/api/sessions":
            return {"id": "session-test"}
        if path.endswith("/blobs/inline"):
            return {"id": "blob-test"}
        if path.endswith("/interpretations?status=pending"):
            return {
                "events": [
                    {
                        "id": "review-test",
                        "kind": self.case["review_policy"]["allowed_types"][0],
                        "llm_draft": "Review the synthetic fixture",
                    }
                ]
                if self.pending
                else []
            }
        if path.endswith("/interpretations/review-test/resolve"):
            assert body == {"choice": "accepted_as_drafted"}
            self.pending = False
            self.approved = True
            return {"resolved": True}
        if path.endswith("/messages") and body is not None:
            self.turns += 1
            if not self.approved:
                self.pending = True
            return {"message": {"content": "Fixture proposal"}}
        if path.endswith("/messages?include_raw_content=true"):
            return [
                {
                    "composition_state_id": self.state["id"],
                    "tool_calls": [{"function": {"name": "preview_pipeline"}, "outcome": "completed", "wire_conformant": True}],
                }
            ]
        if path.endswith("/state"):
            return self.state
        if "/state/versions?" in path:
            return [self.state]
        if path.endswith("/validate"):
            return {"is_valid": True, "errors": []}
        if path.endswith("/execute"):
            return {"run_id": "run-test"}
        if path == "/api/runs/run-test":
            return self.run
        if path.endswith("/outputs"):
            return {"artifacts": [{"path_or_uri": name, "artifact_id": str(i)} for i, name in enumerate(self.outputs)]}
        raise AssertionError(path)

    def raw(self, path: str, body: dict | None = None) -> bytes:
        assert path.endswith("/content")
        index = int(path.split("/")[-2])
        return list(self.outputs.values())[index]


@pytest.fixture(autouse=True)
def isolated_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "assert_frozen", lambda output: "frozen-test-source")
    monkeypatch.setattr(
        runner, "runtime_evidence", lambda output, out, case, state, run: {**_evidence(case, state, run), "sink_effects": []}
    )


def _session_store(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    with sqlite3.connect(tmp_path / "data" / "sessions.db") as db:
        db.execute("CREATE TABLE chat_messages(session_id TEXT, tool_calls TEXT, sequence_no INTEGER)")


def test_review_requires_exact_explicit_acceptance_and_resume_preserves_turns(tmp_path: Path) -> None:
    case = next(case for case in CASES if case["id"] == "01_cleanup_edit")
    api = ScenarioApi(case)
    _session_store(tmp_path)
    first = run_case(api, case, tmp_path, set())
    assert first["status"] == "needs_review"
    assert api.turns == 1
    assert not api.approved
    still_waiting = run_case(api, case, tmp_path, {"a-different-event"})
    assert still_waiting["status"] == "needs_review"
    assert api.turns == 1
    final = run_case(api, case, tmp_path, {"review-test"})
    assert final["status"] == "passed"
    assert api.approved
    assert api.turns == len(case["turns"])
    assert final["repair_turns"] == 0
    assert all("/state" not in path or body is None for path, body in api.sent)
    checkpoint = json.loads((tmp_path / "cases" / case["id"] / "checkpoint.json").read_text())
    assert checkpoint["status"] == "passed"


@pytest.mark.parametrize(
    "state_id,outcome,conformant,expected",
    [
        ("current", "completed", True, True),
        ("old", "completed", True, False),
        ("current", "ARG_ERROR", True, False),
        ("current", "completed", False, False),
    ],
)
def test_preview_requires_bound_state_success(state_id: str, outcome: str, conformant: bool, expected: bool) -> None:
    messages = [
        {
            "composition_state_id": state_id,
            "tool_calls": [{"function": {"name": "preview_pipeline"}, "outcome": outcome, "wire_conformant": conformant}],
        }
    ]
    state = _preview_state("current", 1)
    assert _preview_seen(messages, state, [state]) is expected


def _preview_state(state_id: str, version: int) -> dict:
    return {
        "id": state_id,
        "version": version,
        "session_id": "session-test",
        "sources": {"input": {"plugin": "csv", "options": {"blob_ref": "source-blob"}}},
        "nodes": [{"id": "trim", "node_type": "transform", "plugin": "truncate", "options": {"fields": {"note": 3}}}],
        "edges": [],
        "outputs": [{"name": "cleaned", "plugin": "csv", "options": {"path": "cleaned.csv"}}],
        "metadata": {"name": "cleanup"},
    }


def test_preview_before_mutation_in_same_message_is_stale() -> None:
    preview = {"function": {"name": "preview_pipeline"}, "outcome": "completed", "wire_conformant": True}
    edit = {"function": {"name": "patch_node_options"}, "outcome": "applied", "applied_state_version": 2}
    old = _preview_state("old", 1)
    state = _preview_state("current", 2)
    old["nodes"][0]["options"]["fields"]["note"] = 5
    versions = [old, state]
    assert not _preview_seen([{"composition_state_id": "old", "tool_calls": [preview, edit]}], state, versions)
    assert _preview_seen([{"composition_state_id": "old", "tool_calls": [edit, preview]}], state, versions)
    assert not _preview_seen(
        [{"composition_state_id": "old", "tool_calls": [{**edit, "applied_state_version": None}, preview]}], state, versions
    )


def test_preview_survives_post_compose_and_metadata_only_saves() -> None:
    # Captured live ordering: preview(v4), post_compose(v5), metadata(v6),
    # post_compose(v7). The executable pipeline did not change after preview.
    messages = [
        {
            "composition_state_id": "v4",
            "tool_calls": [{"function": {"name": "preview_pipeline"}, "outcome": "completed", "wire_conformant": True}],
        },
        {
            "composition_state_id": "v5",
            "tool_calls": [{"function": {"name": "set_metadata"}, "outcome": "applied", "applied_state_version": 6}],
        },
    ]
    versions = [_preview_state(f"v{i}", i) for i in range(4, 8)]
    versions[2]["metadata"]["description"] = "Three characters"
    versions[3]["metadata"] = versions[2]["metadata"]
    assert _preview_seen(messages, versions[-1], versions)


@pytest.mark.parametrize("changed", ["nodes", "sources", "edges", "outputs", "source_order", "branch_order"])
def test_preview_cannot_cover_changed_executable_content(changed: str) -> None:
    before = _preview_state("previewed", 1)
    before["sources"]["second"] = {"plugin": "csv", "options": {"blob_ref": "second-blob"}}
    before["nodes"].append({"id": "join", "node_type": "coalesce", "branches": {"left": "left_done", "right": "right_done"}})
    final = deepcopy(before)
    final.update(id="final", version=2)
    if changed == "nodes":
        final["nodes"][0]["options"]["fields"]["note"] = 4
    elif changed == "sources":
        final["sources"]["input"]["options"]["blob_ref"] = "different-blob"
    elif changed == "edges":
        final["edges"] = [{"from_node": "trim", "to_node": "cleaned", "edge_type": "on_error"}]
    elif changed == "outputs":
        final["outputs"][0]["options"]["path"] = "other.csv"
    elif changed == "source_order":
        final["sources"] = dict(reversed(list(final["sources"].items())))
    else:
        final["nodes"][1]["branches"] = {"right": "right_done", "left": "left_done"}
    messages = [
        {
            "composition_state_id": "previewed",
            "tool_calls": [{"function": {"name": "preview_pipeline"}, "outcome": "completed", "wire_conformant": True}],
        }
    ]
    assert not _preview_seen(messages, final, [before, final])


def test_preview_rejects_missing_or_foreign_history() -> None:
    state = _preview_state("current", 1)
    messages = [
        {
            "composition_state_id": "current",
            "tool_calls": [{"function": {"name": "preview_pipeline"}, "outcome": "completed", "wire_conformant": True}],
        }
    ]
    assert not _preview_seen(messages, state, [])
    with pytest.raises(ValueError, match="another session"):
        _preview_seen(messages, state, [{**state, "session_id": "other"}])
    with pytest.raises(ValueError, match="duplicate identities"):
        _preview_seen(messages, state, [state, state])


def test_preview_option_dictionary_key_order_is_not_executable_order() -> None:
    before = _preview_state("old", 1)
    before["nodes"][0]["options"]["suffix"] = ""
    final = deepcopy(before)
    final.update(id="final", version=2)
    final["nodes"][0]["options"] = dict(reversed(list(final["nodes"][0]["options"].items())))
    messages = [
        {
            "composition_state_id": "old",
            "tool_calls": [{"function": {"name": "preview_pipeline"}, "outcome": "completed", "wire_conformant": True}],
        }
    ]
    assert _preview_seen(messages, final, [before, final])


class HistoryApi(ApiClient):
    def __init__(self) -> None:
        super().__init__("http://127.0.0.1:18473")
        self.paths: list[str] = []

    def json(self, path: str, body: dict | None = None):
        self.paths.append(path)
        assert body is None
        if path.endswith("offset=0"):
            return [_preview_state(f"v{i}", i) for i in range(200)]
        assert path.endswith("offset=200")
        return [_preview_state("last", 200)]


def test_preview_history_fetches_all_version_pages() -> None:
    api = HistoryApi()
    versions = runner._state_versions(api, "/api/sessions/session-test")
    assert len(versions) == 201
    assert versions[-1]["id"] == "last"
    assert len(api.paths) == 2


class TimeoutApi(ApiClient):
    def json(self, path: str, body: dict | None = None):
        raise TimeoutError("HTTP worker failed")


@pytest.mark.timeout(5)
def test_worker_timeout_propagates_instead_of_polling_forever(tmp_path: Path) -> None:
    with pytest.raises(TimeoutError, match="HTTP worker failed"):
        compose(TimeoutApi("http://127.0.0.1:18473"), "/api/sessions/test", "test", tmp_path, 1)


class InterruptedApi(ScenarioApi):
    interrupted = False

    def json(self, path: str, body: dict | None = None):
        if path == "/api/runs/run-test" and not self.interrupted:
            self.interrupted = True
            raise KeyboardInterrupt
        return super().json(path, body)


@pytest.mark.parametrize("receipt_only", [False, True])
def test_runtime_resume_does_not_execute_again(tmp_path: Path, receipt_only: bool) -> None:
    case = CASES[0]
    api = InterruptedApi(case)
    api.approved = True
    _session_store(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        run_case(api, case, tmp_path, set())
    path = tmp_path / "cases" / case["id"] / "checkpoint.json"
    if receipt_only:
        checkpoint = json.loads(path.read_text())
        checkpoint["status"] = "execute_requested"
        del checkpoint["run_id"]
        runner.save(path, checkpoint)
    result = run_case(api, case, tmp_path, set())
    assert result["status"] == "passed"
    assert result["run_id"] == "run-test"
    assert sum(path.endswith("/execute") for path, _ in api.sent) == 1


def test_scenario_mutation_cannot_relabel_previous_result(tmp_path: Path) -> None:
    case = CASES[0]
    api = ScenarioApi(case)
    _session_store(tmp_path)
    run_case(api, case, tmp_path, set())
    with pytest.raises(ValueError, match="Scenario changed"):
        run_case(api, {**case, "title": "changed"}, tmp_path, set())


def test_frozen_check_rejects_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner.save(tmp_path / "source-before.json", {"content_sha256": "original"})
    monkeypatch.setattr(runner, "fingerprint", lambda root: {"content_sha256": "original"})
    assert measured_assert_frozen(tmp_path) == "original"
    monkeypatch.setattr(runner, "fingerprint", lambda root: {"content_sha256": "changed"})
    with pytest.raises(ValueError, match="source changed"):
        measured_assert_frozen(tmp_path)


def test_resume_cannot_change_service_model(tmp_path: Path) -> None:
    config = {"planner": "original", "runtime_model": "runtime", "max_provider_calls": 200}
    source = {"content_sha256": "same"}
    runner.save(tmp_path / "service-config.json", config)
    runner.save(tmp_path / "source-before.json", source)
    validate_resume(tmp_path, config, source)
    with pytest.raises(ValueError, match="configuration changed"):
        validate_resume(tmp_path, {**config, "planner": "other"}, source)


def test_fixture_policy_requires_every_requested_plugin() -> None:
    from pydantic import SecretBytes

    from elspeth.web.config import WebSettings

    scenarios = Path(__file__).resolve().parents[2] / "fixtures/composer_convergence"
    settings = WebSettings(
        plugin_allowlist=ACCEPTANCE_PLUGIN_ALLOWLIST,
        composer_max_composition_turns=12,
        composer_max_discovery_turns=6,
        composer_timeout_seconds=900,
        composer_transport_idle_ceiling_seconds=960,
        composer_rate_limit_per_minute=100,
        shareable_link_signing_key=SecretBytes(secrets.token_bytes(32)),
        secret_key=secrets.token_urlsafe(48),
    )
    check_fixture_availability(settings, scenarios)
    without_truncation = settings.model_copy(
        update={"plugin_allowlist": tuple(p for p in ACCEPTANCE_PLUGIN_ALLOWLIST if p != "transform:truncate")}
    )
    with pytest.raises(ValueError, match="transform:truncate"):
        check_fixture_availability(without_truncation, scenarios)
    with pytest.raises(ValueError, match="disabled"):
        check_fixture_availability(settings.model_copy(update={"plugin_allowlist": ()}), scenarios)


def test_custom_fixture_mutation_during_execution_cannot_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    case = CASES[0]
    api = ScenarioApi(case)
    api.approved = True
    _session_store(tmp_path)
    fixture = tmp_path / "custom.json"
    runner.save(fixture, case)

    def changed_fixture_evidence(output, out, scenario, state, run):
        runner.save(fixture, {**case, "title": "changed while running"})
        return {**_evidence(scenario, state, run), "sink_effects": []}

    monkeypatch.setattr(runner, "runtime_evidence", changed_fixture_evidence)
    result = run_case(api, case, tmp_path, set(), fixture)
    assert result["status"] == "harness_error"
    assert "fixture changed" in result["failures"][0]


def test_unrecorded_execution_receipt_never_repeats_request(tmp_path: Path) -> None:
    case = CASES[0]
    api = ScenarioApi(case)
    _session_store(tmp_path)
    run_case(api, case, tmp_path, set())
    path = tmp_path / "cases" / case["id"] / "checkpoint.json"
    checkpoint = json.loads(path.read_text())
    checkpoint["status"] = "execute_requested"
    runner.save(path, checkpoint)
    result = run_case(api, case, tmp_path, set())
    assert result["status"] == "harness_error"
    assert not any(path.endswith("/execute") for path, _ in api.sent)


def test_collector_reads_only_run_bound_runtime_calls_and_physical_blobs(tmp_path: Path) -> None:
    from elspeth.core.payload_store import FilesystemPayloadStore

    _session_store(tmp_path)
    data = tmp_path / "data"
    (data / "runs").mkdir()
    (data / "blobs").mkdir()
    content = "id\n1\n"
    blob_path = data / "blobs" / "input.csv"
    blob_path.write_text(content)
    digest = hashlib.sha256(content.encode()).hexdigest()
    out = tmp_path / "case"
    out.mkdir()
    runner.save(out / "input-0.json", {"id": "blob"})
    runner.save(tmp_path / "service-config.json", {"runtime_model": "actual-model"})
    state = {"id": "state", "session_id": "session", "sources": {"source": {"options": {"blob_ref": "blob"}}}}
    runner.save(out / "turn-1.state.json", state)
    with sqlite3.connect(data / "sessions.db") as db:
        db.execute("CREATE TABLE runs(id TEXT, session_id TEXT, state_id TEXT, landscape_run_id TEXT)")
        db.execute("INSERT INTO runs VALUES ('run','session','executed-state','landscape')")
        db.execute("CREATE TABLE blobs(id TEXT, filename TEXT, content_hash TEXT, storage_path TEXT, status TEXT)")
        db.execute("INSERT INTO blobs VALUES (?, ?, ?, ?, ?)", ("blob", "input.csv", digest, str(blob_path), "available"))
    store = FilesystemPayloadStore(data / "payloads")
    request_ref = store.store(json.dumps({"model": "actual-model", "messages": ["not copied into capture"]}).encode())
    with sqlite3.connect(data / "runs" / "audit.db") as db:
        db.executescript("""
            CREATE TABLE sink_effects(effect_id TEXT,artifact_id TEXT,sink_node_id TEXT,stream_id TEXT,stream_sequence INTEGER,predecessor_effect_id TEXT,state TEXT,run_id TEXT);
            INSERT INTO sink_effects VALUES ('effect','artifact','sink','stream',0,NULL,'finalized','landscape');
            INSERT INTO sink_effects VALUES ('foreign-effect','foreign-artifact','sink','stream',1,'effect','finalized','foreign');
            CREATE TABLE rows(row_id TEXT,row_index INTEGER,run_id TEXT);
            CREATE TABLE tokens(token_id TEXT,row_id TEXT,run_id TEXT,join_group_id TEXT);
            CREATE TABLE token_parents(token_id TEXT,parent_token_id TEXT,run_id TEXT,ordinal INTEGER);
            CREATE TABLE token_outcomes(token_id TEXT,outcome TEXT,path TEXT,completed INTEGER,sink_name TEXT,run_id TEXT);
            CREATE TABLE token_lineage_frames(token_id TEXT,depth INTEGER,kind TEXT,group_id TEXT,member_key TEXT,run_id TEXT);
            CREATE TABLE nodes(node_id TEXT,plugin_name TEXT,node_type TEXT,run_id TEXT);
            CREATE TABLE node_states(state_id TEXT,token_id TEXT,run_id TEXT,node_id TEXT);
            CREATE TABLE calls(call_id TEXT,state_id TEXT,call_type TEXT,status TEXT,prompt_tokens INTEGER,completion_tokens INTEGER,request_ref TEXT);
            INSERT INTO rows VALUES ('row',0,'landscape');
            INSERT INTO tokens VALUES ('token','row','landscape',NULL);
            INSERT INTO nodes VALUES ('node','llm','transform','landscape');
            INSERT INTO node_states VALUES ('node-state','token','landscape','node');
            INSERT INTO node_states VALUES ('foreign-state','token','foreign','node');
        """)
        for call_id, state_id in [("runtime", "node-state"), ("preflight", None), ("foreign", "foreign-state")]:
            db.execute("INSERT INTO calls VALUES (?,?,'llm','success',10,2,?)", (call_id, state_id, request_ref))
    scenario = {"inputs": [{"filename": "input.csv", "content": content}]}
    run = {"run_id": "run", "landscape_run_id": "landscape", "session_id": "session"}
    evidence = measured_runtime_evidence(tmp_path, out, scenario, state, run)
    assert [effect["effect_id"] for effect in evidence["sink_effects"]] == ["effect"]
    assert [call["call_id"] for call in evidence["runtime_calls"]] == ["runtime"]
    assert evidence["composition_state_id"] == "executed-state"
    assert evidence["composition_state_id"] != state["id"]
    assert evidence["runtime_calls"][0]["model"] == "actual-model"
    assert "request_ref" not in evidence["runtime_calls"][0]
    assert evidence["source_rows"] == [{"row_id": "row", "row_index": 0}]
    assert evidence["input_blobs"][0]["physical_after_hash"] == digest
    blob_path.write_text("changed")
    changed = measured_runtime_evidence(tmp_path, out, scenario, state, run)
    assert changed["input_blobs"][0]["physical_after_hash"] != digest
