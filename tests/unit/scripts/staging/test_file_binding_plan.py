"""The file-binding plan (``composer_file_bindings.json``) and the battery rules it adds.

The plan connects and reconnects files from an empty pipeline. Here it is run
end to end against a simulated Composer that makes exactly the change each
edit asks for (the files the battery uploaded bound where the edit says, new
files written for the edits that ask the Composer to write one, a prompt
review card for every prompt change, with the bindings' authorship), so the
plan is shown executable: every edit's change is false before it is sent,
every verdict can pass, cards are of the expected kind, and the pipeline is
left empty as found. Each new judge rule is then broken on purpose and the
edit that should catch it is shown to fail. It does not show that a real
model makes the change; a live run measures that.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from scripts.staging import composer_edit_battery as battery
from scripts.staging import edit_plan
from scripts.staging.edit_plan import (
    DEFAULT_PLAN,
    MIXED_PLAN,
    PLAN_DIR,
    check_expectations,
    graph_view,
    load_plan,
    resolve_edits,
    resolve_targets,
)
from scripts.staging.opslib import AccountAuth, CheckpointStore, OperationFailed, UsageError, WebClient
from scripts.staging.repair_copied_prompt_reviews import prompt_surface

from tests.unit.scripts.staging.fake_deployment import BASE_URL, Blob, FakeDeployment, FakeTurn, Session

SESSION = "55555555-5555-4555-8555-555555555555"
# The names this plan adds are read from the module where they are used, so this file collects on a tree without them.
FILE_BINDINGS_PLAN = PLAN_DIR / "composer_file_bindings.json"
type State = dict[str, Any]
# The applicable edits of the shipped file-binding plan; a change to the plan must move this on purpose.
FILE_BINDING_PLAN_EDITS = 35


# ── a Composer that makes exactly the asked-for change ─────────────────────


def _parts(pointer: str) -> list[str]:
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def _component(state: State, parts: list[str]) -> tuple[Any, list[str]]:
    if parts[0] == "nodes":
        return next(item for item in state["nodes"] if item["id"] == parts[1]), parts[2:]
    if parts[0] == "outputs":
        return next(item for item in state["outputs"] if item["name"] == parts[1]), parts[2:]
    return state["sources"][parts[1]], parts[2:]


def _set(state: State, pointer: str, value: Any) -> None:
    container, rest = _component(state, _parts(pointer))
    for part in rest[:-1]:
        container = container.setdefault(part, {})
    container[rest[-1]] = value


def _marker(blob: Blob) -> State:
    return {"blob_ref": blob.id, "mode": "inline_content", "sha256": hashlib.sha256(blob.data).hexdigest()}


def _slot_paths(options: Mapping[str, Any]) -> list[tuple[str, Any]]:
    slots = [(key, options[key]) for key in ("prompt_template", "system_prompt") if key in options]
    queries = options["queries"] if "queries" in options else {}
    slots.extend((f"queries.{name}.template", entry["template"]) for name, entry in queries.items() if "template" in entry)
    return slots


class FileComposer:
    """Makes exactly the change each resolved edit expects; ``mutate`` lets a test break one turn on purpose."""

    def __init__(self, edits: list[State], session: Session, mutate: Callable[[str, State, FileComposer], None] | None = None) -> None:
        self.by_message = {edit["message"]: edit for edit in edits if edit["applicable"]}
        self.session = session
        self.mutate = mutate
        self.turn = 0
        self.open_cards = True

    def uploaded(self, name: str) -> Blob:
        return next(blob for blob in self.session.blobs.values() if blob.filename == name and blob.created_by == "user")

    def written(self, name: str, content: str) -> Blob:
        blob = Blob(
            id=f"written-{self.turn}-{name}",
            filename=name,
            data=content.encode(),
            created_by="assistant",
            creation_modality="llm_generated",
        )
        self.session.blobs[blob.id] = blob
        return blob

    def __call__(self, current: State | None, message: str) -> FakeTurn:
        self.turn += 1
        edit = self.by_message[message]
        state: State = edit_plan.empty_state() if current is None else current
        before = {node["id"]: prompt_surface(node["options"]) for node in state["nodes"]}
        for expectation in edit["expect"]:
            self.apply(state, edit, expectation)
        if "/sources/source" in edit["may_change"] or "/sources/source/options/blobs" in edit["may_change"]:
            self.file_rows(state, edit)
        errors = []
        if not state["sources"]:
            errors.append({"error_code": "no_source_configured", "component": "source", "message": "No source configured."})
        if not state["outputs"]:
            errors.append({"error_code": "no_sinks_configured", "component": "pipeline", "message": "No sinks configured."})
        state["validation_errors"] = errors
        if self.mutate is not None:
            self.mutate(edit["id"], state, self)
        state["validation_errors"] = state["validation_errors"] or None
        reviews: State = {}
        for item in state["nodes"]:
            surface = prompt_surface(item["options"])
            if surface and surface != (before[item["id"]] if item["id"] in before else {}) and self.open_cards:
                reviews.update(self.review(item))
        state["id"] = f"state-{self.turn}"
        tools = [*edit["tools_all"], *edit["tools_any"][:1]]
        calls = [
            {"id": f"c{self.turn}-{i}", "type": "function", "function": {"name": t, "arguments": "{}"}, "outcome": "applied"}
            for i, t in enumerate(tools)
        ]
        audit = [{"_kind": "llm_call_audit", "call": {"status": "success", "latency_ms": 900}}]
        return FakeTurn(state=state, audit=audit, assistant_tool_calls=calls, reviews=reviews)

    def apply(self, state: State, edit: Mapping[str, Any], expectation: Mapping[str, Any]) -> None:
        if "new_node" in expectation:
            item: State = {"id": expectation["new_node"], "input": "", "on_success": None, "options": {}, "description": None}
            item.update(expectation["fields"])
            state["nodes"].append(item)
        elif "new_output" in expectation:
            options = {key: rule["ends_with"] if "ends_with" in rule else rule["equals"] for key, rule in expectation["options"].items()}
            state["outputs"].append({"name": expectation["new_output"], "options": options, "description": None, **expectation["fields"]})
        elif "bound" in expectation:
            name = expectation["file"]
            in_plan = "attach" in edit and name in edit["attach"]
            blob = self.uploaded(name) if in_plan else self.written(name, expectation["content"])
            if expectation["bound"].startswith("/sources/"):
                _set(state, expectation["bound"], blob.id)
                _set(state, expectation["bound"].rsplit("/", 1)[0] + "/path", "<redacted>")
            else:
                _set(state, expectation["bound"], _marker(blob))
        elif "equals" in expectation or "ends_with" in expectation:
            pointer = expectation["equals"] if "equals" in expectation else expectation["ends_with"]
            parts = _parts(pointer)
            if parts[0] == "sources" and parts[1] not in state["sources"]:
                state["sources"][parts[1]] = {"plugin": None, "on_success": None, "options": {}, "on_validation_failure": "discard"}
            _set(state, pointer, copy.deepcopy(expectation["value"]))
        elif "absent" in expectation:
            parts = _parts(expectation["absent"])
            if parts[0] == "nodes":
                state["nodes"] = [item for item in state["nodes"] if item["id"] != parts[1]]
            elif parts[0] == "outputs":
                state["outputs"] = [item for item in state["outputs"] if item["name"] != parts[1]]
            else:
                del state["sources"][parts[1]]
        elif "chain" in expectation:
            self.rewire(state, expectation["chain"])
        elif "files" not in expectation:
            raise AssertionError(f"no simulation for {expectation}")

    def file_rows(self, state: State, edit: Mapping[str, Any]) -> None:
        """A one-row-per-file source binds exactly the files the edit names (its ``files`` expectation)."""
        names = next(expectation["files"] for expectation in edit["expect"] if "files" in expectation)
        source = state["sources"]["source"]
        if source["plugin"] == "blob_rows":
            source["options"] = {"blobs": [{"blob_id": self.uploaded(name).id, "filename": name} for name in names]}

    @staticmethod
    def rewire(state: State, chain: Mapping[str, Any]) -> None:
        nodes = {item["id"]: item for item in state["nodes"]}
        owner = state["sources"][chain["from"]] if chain["from"] in state["sources"] else nodes[chain["from"]]
        route = owner["on_success"] if chain["via"] else chain["to"]
        if chain["via"]:
            route = owner["on_success"] = f"{chain['from']}_out"
        for step in chain["via"]:
            nodes[step]["input"] = route
            route = nodes[step]["on_success"] = f"{step}_out"
        if chain["to"] in nodes:
            nodes[chain["to"]]["input"] = route
        elif chain["via"]:
            nodes[chain["via"][-1]]["on_success"] = chain["to"]
        else:
            owner["on_success"] = chain["to"]

    def review(self, item: State) -> State:
        options = item["options"]
        rows = options["interpretation_requirements"] if "interpretation_requirements" in options else []
        rows.append({"id": f"req-{self.turn}-{item['id']}", "kind": "llm_prompt_template", "status": "pending"})
        options["interpretation_requirements"] = rows
        surface = prompt_surface(options)
        bindings = []
        for path, value in _slot_paths(options):
            if type(value) is dict and "blob_ref" in value:
                blob = self.session.blobs[value["blob_ref"]]
                bindings.append(
                    {
                        "field_path": path,
                        "blob_id": blob.id,
                        "text": blob.data.decode(),
                        "created_by": blob.created_by,
                        "creation_modality": blob.creation_modality,
                    }
                )
        event_id = f"review-{self.turn}-{item['id']}"
        return {
            event_id: {
                "event": {"id": event_id, "kind": "llm_prompt_template"},
                "snapshot": {"node_id": item["id"], "anchor": f"anchor-{self.turn}", "authored_surface": surface, "bindings": bindings},
                "pending": True,
            }
        }


# ── running the plan ───────────────────────────────────────────────────────


def make_run(
    tmp_path: Path,
    *,
    plan_path: Path = FILE_BINDINGS_PLAN,
    mutate: Callable[[str, State, FileComposer], None] | None = None,
    state: State | None = None,
) -> tuple[battery.EditBattery, FakeDeployment, FileComposer]:
    plan = load_plan(plan_path)
    # A session with no graph yet: the state read answers null.
    session = Session(owner="tester", state=state)
    empty = edit_plan.empty_state()
    edits = resolve_edits(plan, resolve_targets(plan, empty, {}), empty)
    fake = FakeDeployment(passwords={"tester": "pw"}, sessions={SESSION: session})
    composer = FileComposer(edits, session, mutate)
    fake.provider = composer
    run = battery.EditBattery(
        client=WebClient(BASE_URL, transport=fake),
        auth=AccountAuth(username="tester", password_env="TEST_BATTERY_PASSWORD"),
        store=CheckpointStore(tmp_path / "state"),
        session_id=SESSION,
        attempt="1",
        plan=plan,
        sleep=lambda _seconds: None,
        now=fake.clock,
    )
    return run, fake, composer


def results(run: battery.EditBattery) -> list[State]:
    checkpoint = run.store.read(run.name)
    assert checkpoint is not None
    found: list[State] = checkpoint["results"]
    return found


def result_of(run: battery.EditBattery, edit_id: str) -> State:
    return next(result for result in results(run) if result["id"] == edit_id)


@pytest.fixture(autouse=True)
def _password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_BATTERY_PASSWORD", "pw")


def test_the_file_binding_plan_passes_end_to_end_and_leaves_the_pipeline_empty(tmp_path: Path) -> None:
    run, fake, _ = make_run(tmp_path)
    summary = run.run()
    failed = [(result["id"], result["findings"]) for result in results(run) if not result["passed"]]
    assert failed == []
    assert summary["applicable"] == FILE_BINDING_PLAN_EDITS
    assert summary["complete"] is True
    assert summary["all_passed_as_found"] is True
    assert summary["as_found_differences"] == []
    # Not gold: gold is the 50-edit measure, and this plan is judged by its own pass rule.
    assert summary["gold"] is False
    session = fake.sessions[SESSION]
    uploaded = sorted(blob.filename for blob in session.blobs.values() if blob.created_by == "user")
    assert uploaded == sorted(load_plan(FILE_BINDINGS_PLAN).attachments)
    # Each attachment was uploaded once, before the message that names it.
    uploads = fake.calls("POST", "/blobs")
    assert len(uploads) == len(uploaded)
    written = sorted(blob.filename for blob in session.blobs.values() if blob.created_by == "assistant")
    assert written == ["prompt_system_4.txt", "prompt_user_4.txt"]
    # Every prompt binding opened a card the battery approved, of the kind the edit named.
    approvals = {result["id"]: len(result["approvals"]) for result in results(run) if result["approvals"]}
    assert approvals == {
        "add-llm-step": 1,
        **{f"prompt-to-file-{n}": 1 for n in (1, 2, 3)},
        **{f"system-prompt-to-file-{n}": 1 for n in (1, 2, 3)},
        "system-prompt-written-file": 1,
        "prompt-written-file": 1,
        "add-query-step": 1,
        "query-to-file-2": 1,
        "query-to-file-3": 1,
    }


def test_the_plan_messages_order_and_attachments(tmp_path: Path) -> None:
    plan = load_plan(FILE_BINDINGS_PLAN)
    assert plan.start == "empty" and plan.pass_rule == "all_passed_as_found" and plan.targets == {}
    ids = [edit["id"] for edit in plan.edits]
    assert ids[:5] == ["create-source-and-sink", "source-to-orders-b", "source-to-orders-c", "source-to-orders-d", "sink-to-results-2"]
    assert ids[-1] == "remove-source-and-output"
    for edit in plan.edits:
        # Each message names its files; none names a tool.
        for name in edit["attach"] if "attach" in edit else []:
            assert name in edit["message"]
        assert not any(tool in edit["message"] for tool in battery.EDITING_TOOLS)
    # Tiny, synthetic attachments only.
    assert all(len(data) < 400 for data in plan.attachments.values())
    assert len(plan.attachments) == 27


def test_the_new_step_option_keys_are_the_catalog_keys() -> None:
    """Each new step may carry only its plugin's own option keys (from the live catalog) and the two the server adds."""
    from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
    from elspeth.web.catalog.service import CatalogServiceImpl

    catalog = CatalogServiceImpl(get_shared_plugin_manager())

    def keys(plugin: str) -> set[str]:
        schema = catalog.get_schema("transform", plugin).json_schema
        found = set(schema["properties"]) if "properties" in schema else set()
        for branch in schema["oneOf"] if "oneOf" in schema else []:
            found |= set(schema["$defs"][branch["$ref"].rsplit("/", 1)[1]]["properties"])
        return found

    for edit in load_plan(FILE_BINDINGS_PLAN).edits:
        for expectation in edit["expect"]:
            if "new_node" in expectation:
                plugin = expectation["fields"]["plugin"]
                assert set(expectation["option_keys"]) - keys(plugin) <= {"profile", "prompt_template_parts"}, expectation["new_node"]


def test_the_empty_state_stand_in_is_the_composers_initial_state() -> None:
    from elspeth.web.composer.state import CompositionState, PipelineMetadata

    # The state the Composer starts the first turn from when the session has none (composer/service.py).
    initial = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1).to_dict()
    assert {key: initial[key] for key in ("metadata", "sources", "nodes", "edges", "outputs")} == {
        key: edit_plan.EMPTY_STATE[key] for key in ("metadata", "sources", "nodes", "edges", "outputs")
    }


# ── the two existing plans are unaffected ──────────────────────────────────


@pytest.mark.parametrize("plan_path", [DEFAULT_PLAN, MIXED_PLAN])
def test_the_fifty_edit_plans_use_none_of_the_file_binding_extensions(plan_path: Path) -> None:
    plan = load_plan(plan_path)
    assert (plan.start, plan.pass_rule, dict(plan.attachments)) == (None, "gold", {})
    assert plan.sha256 == hashlib.sha256(plan_path.read_bytes()).hexdigest()
    for edit in plan.edits:
        assert not {"attach", "new_errors"} & set(edit)
        assert not any("bound" in expectation or "files" in expectation for expectation in edit["expect"])
        assert "approve" not in edit or "card" not in edit["approve"]


# ── each new rule, broken on purpose ───────────────────────────────────────


def test_a_rebind_that_leaves_the_old_file_fails_bound(tmp_path: Path) -> None:
    def keep_old_file(edit_id: str, state: State, composer: FileComposer) -> None:
        if edit_id == "reference-to-regions-c":
            state["nodes"][0]["options"]["reference_content"] = _marker(composer.uploaded("regions_b.csv"))

    run, _, _ = make_run(tmp_path, mutate=keep_old_file)
    run.run()
    result = result_of(run, "reference-to-regions-c")
    assert result["passed"] is False
    assert "expect[0]:bound:/nodes/battery_regions/options/reference_content" in result["findings"]


def test_a_rebind_that_binds_a_copy_instead_of_the_uploaded_file_fails_bound(tmp_path: Path) -> None:
    def copy_the_upload(edit_id: str, state: State, composer: FileComposer) -> None:
        if edit_id == "source-to-orders-b":
            state["sources"]["source"]["options"]["blob_ref"] = composer.written("orders_b.csv", "order_id\n").id

    run, _, _ = make_run(tmp_path, mutate=copy_the_upload)
    run.run()
    findings = result_of(run, "source-to-orders-b")["findings"]
    assert "expect[0]:bound:/sources/source/options/blob_ref" in findings


def test_a_written_file_with_other_text_fails_bound_content(tmp_path: Path) -> None:
    def other_text(edit_id: str, state: State, composer: FileComposer) -> None:
        if edit_id == "system-prompt-written-file":
            state["nodes"][1]["options"]["system_prompt"] = _marker(composer.written("prompt_system_4.txt", "Something else."))

    run, _, _ = make_run(tmp_path, mutate=other_text)
    run.run()
    assert "expect[0]:bound:/nodes/battery_llm/options/system_prompt" in result_of(run, "system-prompt-written-file")["findings"]


def test_a_stale_binding_left_elsewhere_fails_files() -> None:
    plan = load_plan(FILE_BINDINGS_PLAN)
    edits = resolve_edits(plan, {}, edit_plan.empty_state())
    edit = next(item for item in edits if item["id"] == "source-to-orders-b")
    after: State = {
        "sources": {"source": {"options": {"blob_ref": "id-b"}}},
        "nodes": {"stray": {"options": {"note": {"blob_ref": "id-a", "mode": "inline_content", "sha256": "0"}}}},
        "outputs": {},
        "edges": {},
        "metadata": {},
    }
    files = edit_plan.FileContext(
        uploads={"orders_a.csv": "id-a", "orders_b.csv": "id-b"},
        listing={"id-a": {"filename": "orders_a.csv", "created_by": "user"}, "id-b": {"filename": "orders_b.csv", "created_by": "user"}},
    )
    failures, _ = check_expectations(edit, after, after, after, lambda _marker: "", files=files)
    assert failures == ["expect[1]:files:files"]
    del after["nodes"]["stray"]
    assert check_expectations(edit, after, after, after, lambda _marker: "", files=files)[0] == []
    # Without the file context a file expectation never passes.
    assert check_expectations(edit, after, after, after, lambda _marker: "")[0] == [
        "expect[0]:bound:/sources/source/options/blob_ref",
        "expect[1]:files:files",
    ]


def test_a_card_of_the_other_kind_is_a_finding_and_is_not_approved(tmp_path: Path) -> None:
    def inline_system_prompt(edit_id: str, state: State, composer: FileComposer) -> None:
        # The step keeps an inline system prompt: the card for the uploaded prompt file is then not a user-files card.
        if edit_id == "add-llm-step":
            state["nodes"][1]["options"]["system_prompt"] = "Be brief."

    run, _, _ = make_run(tmp_path, mutate=inline_system_prompt)
    run.run()
    assert result_of(run, "add-llm-step")["passed"] is True
    result = result_of(run, "prompt-to-file-1")
    assert "review_card_kind:battery_llm:expected_user_files:shown_agent_written" in result["findings"]
    assert result["approvals"] == []


def test_a_named_card_that_never_opens_is_a_finding(tmp_path: Path) -> None:
    def no_card(edit_id: str, state: State, composer: FileComposer) -> None:
        composer.open_cards = edit_id != "system-prompt-to-file-2"

    run, _, _ = make_run(tmp_path, mutate=no_card)
    run.run()
    assert "expected_review_card_missing:battery_llm" in result_of(run, "system-prompt-to-file-2")["findings"]


def test_card_kind_follows_the_frontend_rule() -> None:
    user = {"field_path": "prompt_template", "blob_id": "a", "text": "t", "created_by": "user", "creation_modality": "verbatim"}
    marker = {"blob_ref": "a", "mode": "inline_content", "sha256": "0"}
    snapshot = {"is_live_review": True, "bindings": [user], "authored_surface": {"prompt_template": marker}}
    card_kind = edit_plan.card_kind
    assert card_kind(snapshot) == "user_files"
    assert card_kind({**snapshot, "bindings": [{**user, "created_by": "assistant"}]}) == "agent_written"
    assert card_kind({**snapshot, "bindings": [{**user, "creation_modality": "llm_generated"}]}) == "agent_written"
    assert card_kind({**snapshot, "authored_surface": {"prompt_template": marker, "system_prompt": "inline"}}) == "agent_written"
    assert card_kind({**snapshot, "bindings": []}) == "agent_written"
    assert card_kind({**snapshot, "is_live_review": False}) == "agent_written"


def test_emptying_the_pipeline_allows_only_the_errors_it_lists(tmp_path: Path) -> None:
    def unlisted_error(edit_id: str, state: State, composer: FileComposer) -> None:
        if edit_id == "remove-source-and-output":
            state["validation_errors"].append({"error_code": "empty_pipeline", "component": None, "message": "Pipeline is empty."})

    run, _, _ = make_run(tmp_path, mutate=unlisted_error)
    run.run()
    findings = result_of(run, "remove-source-and-output")["findings"]
    assert findings == ['new_validation_error:["empty_pipeline", null, "Pipeline is empty."]']


def test_without_new_errors_the_emptied_pipeline_fails(tmp_path: Path) -> None:
    raw = json.loads(FILE_BINDINGS_PLAN.read_text())
    del raw["edits"][-1]["new_errors"]
    (tmp_path / "files").mkdir()
    for name, data in load_plan(FILE_BINDINGS_PLAN).attachments.items():
        (tmp_path / "files" / name).write_bytes(data)
    (tmp_path / "plan.json").write_text(json.dumps(raw))
    run, _, _ = make_run(tmp_path, plan_path=tmp_path / "plan.json")
    run.run()
    assert result_of(run, "remove-source-and-output")["findings"] == [
        'new_validation_error:["no_source_configured", "source", "No source configured."]',
        'new_validation_error:["no_sinks_configured", "pipeline", "No sinks configured."]',
    ]


def test_a_plan_that_starts_empty_refuses_a_session_with_a_graph(tmp_path: Path) -> None:
    graph: State = {**edit_plan.empty_state(), "id": "s0", "outputs": [{"name": "kept", "plugin": "json", "options": {}}]}
    run, _, _ = make_run(tmp_path, state=graph)
    with pytest.raises(OperationFailed, match="starts from an empty pipeline"):
        run.run()


def test_a_session_already_holding_a_file_of_an_attachment_name_is_refused(tmp_path: Path) -> None:
    run, fake, _ = make_run(tmp_path)
    fake.sessions[SESSION].blobs["old"] = Blob(id="old", filename="regions_c.csv", data=b"x\n")
    with pytest.raises(OperationFailed, match=r"already holds files named as the plan's attachments \(regions_c.csv\)"):
        run.run()


def test_a_resumed_run_reuses_an_upload_it_had_not_recorded(tmp_path: Path) -> None:
    run, fake, _ = make_run(tmp_path)
    from scripts.staging.opslib import sign_in

    sign_in(run.client, run.auth, prompt_secret=run.prompt_secret)
    run.start()
    # The upload landed but the run stopped before it saved the file id.
    data = load_plan(FILE_BINDINGS_PLAN).attachments["orders_a.csv"]
    fake.sessions[SESSION].blobs["pre"] = Blob(id="pre", filename="orders_a.csv", data=data, mime_type="text/csv")
    summary = run.run()
    assert summary["all_passed_as_found"] is True
    checkpoint = run.store.read(run.name)
    assert checkpoint is not None and checkpoint["uploads"]["orders_a.csv"] == "pre"
    uploaded = [payload["upload"]["filename"] for verb, path, payload in fake.requests if verb == "POST" and path.endswith("/blobs")]
    assert "orders_a.csv" not in uploaded
    assert len(uploaded) == len(load_plan(FILE_BINDINGS_PLAN).attachments) - 1


def test_the_process_passes_on_the_plans_own_rule(tmp_path: Path) -> None:
    _, fake, _ = make_run(tmp_path)
    argv = ["run", "--execute", "--plan", str(FILE_BINDINGS_PLAN), "--base-url", BASE_URL, "--session", SESSION]
    argv += ["--account", "tester", "--password-env", "TEST_BATTERY_PASSWORD", "--state-dir", str(tmp_path / "cli")]
    assert battery.main(argv, client_factory=lambda url: WebClient(url, transport=fake), sleep=lambda _s: None, now=fake.clock) == 0


def test_plan_validation_refuses_malformed_file_fields(tmp_path: Path) -> None:
    raw = json.loads(FILE_BINDINGS_PLAN.read_text())
    (tmp_path / "files").mkdir()
    for name in load_plan(FILE_BINDINGS_PLAN).attachments:
        (tmp_path / "files" / name).write_bytes(load_plan(FILE_BINDINGS_PLAN).attachments[name])

    def write(plan: State) -> Path:
        path = tmp_path / "plan.json"
        path.write_text(json.dumps(plan))
        return path

    assert load_plan(write(raw)).sha256 != hashlib.sha256((tmp_path / "plan.json").read_bytes()).hexdigest()
    cases: list[tuple[Callable[[State], None], str]] = [
        (lambda p: p.update(start="full"), "plan start"),
        (lambda p: p.update(pass_rule="lenient"), "pass_rule"),
        (lambda p: p["edits"][1].update(attach=["orders_a.csv"]), "attached by an earlier edit"),
        (lambda p: p["edits"][1].update(attach=["Orders B.csv"]), "attach must list file names"),
        (lambda p: p["edits"][1].update(attach=["orders_z.csv"]), "cannot read attachment"),
        (lambda p: p["edits"][-1].update(new_errors=[]), "new_errors"),
        (lambda p: p["edits"][9]["approve"].update(card="light"), "optional card"),
        (lambda p: p["edits"][1]["expect"][0].update(extra=1), "malformed bound"),
        (lambda p: p["edits"][1]["expect"][1].update(files="orders_b.csv"), "malformed files"),
    ]
    for change, message in cases:
        broken = copy.deepcopy(raw)
        change(broken)
        with pytest.raises(UsageError, match=message):
            load_plan(write(broken))


def test_a_changed_attachment_is_a_different_plan(tmp_path: Path) -> None:
    (tmp_path / "files").mkdir()
    plan = load_plan(FILE_BINDINGS_PLAN)
    for name, data in plan.attachments.items():
        (tmp_path / "files" / name).write_bytes(data)
    path = tmp_path / "plan.json"
    path.write_bytes(FILE_BINDINGS_PLAN.read_bytes())
    assert load_plan(path).sha256 == plan.sha256
    (tmp_path / "files" / "notes_d.txt").write_bytes(b"Changed.\n")
    assert load_plan(path).sha256 != plan.sha256


def test_a_session_with_no_graph_reads_as_the_empty_pipeline(tmp_path: Path) -> None:
    run, _, _ = make_run(tmp_path)
    from scripts.staging.opslib import sign_in

    sign_in(run.client, run.auth, prompt_secret=run.prompt_secret)
    assert run.state() == edit_plan.empty_state()
    assert graph_view(run.state())["sources"] == {}
