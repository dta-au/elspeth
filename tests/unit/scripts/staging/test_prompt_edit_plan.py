"""The prompt-wording plan (``composer_prompt_edits_50.json``) on the fixture it is written for, and the rules it adds.

The plan changes prompt wording by chat in prompts held in files on the 30-step
acceptance fixture (``fx-acc30``). The fixture is rebuilt here from its spec with
``scripts.staging.large_graph_fixture`` (the generator is seeded; the digest pin
below fails when it drifts), imported with the server's own YAML importer, and
every prompt slot bound to a file the user uploaded, as ``setup`` binds it.

The plan is then run end to end against a simulated Composer that makes exactly
the change each edit asks for, the way the real one must: a bound file cannot be
rewritten, so it writes a new file holding the old text with exactly that change
and re-points the slot; it opens a prompt review card on each step it changed,
with that step's ``interpretation_review_pending`` error, which the approval
clears. A plan that passes here is executable: every edit's change is false before
it is sent (and every no-op's premise true), every verdict can pass, and the graph
is left as found, so the plan can be gold. Each new judge rule is then broken on
purpose and shown to fail the edit. It does not show that a real model makes the
change; a live run measures that.
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
    FILE_BINDINGS_PLAN,
    MIXED_PLAN,
    PLAN_DIR,
    as_found_differences,
    check_expectations,
    graph_view,
    leftover_findings,
    load_plan,
    preconditions,
    resolve_edits,
    resolve_targets,
)
from scripts.staging.large_graph_fixture import build_fixture, preset_spec
from scripts.staging.opslib import AccountAuth, CheckpointStore, UsageError, WebClient
from scripts.staging.repair_copied_prompt_reviews import prompt_surface

from tests.unit.scripts.staging.fake_deployment import BASE_URL, Blob, FakeDeployment, FakeTurn, Session

SESSION = "66666666-6666-4666-8666-666666666666"
PROMPT_EDITS_PLAN = PLAN_DIR / "composer_prompt_edits_50.json"
# fx-acc30: the acceptance preset with 30 steps, 14 LLM steps and 300,000 prompt bytes (its manifest's spec).
FIXTURE_SPEC = {"steps": 30, "llm_steps": 14, "total_prompt_bytes": 300_000}
# SHA-256 over the generated prompt files' names and digests, measured against fx-acc30's manifest.json.
FIXTURE_FILES_DIGEST = "7056d0fd1f357bff96b9dc0f78a8e88ecac225a312d6adce20584d11245102d3"
PROMPT_EDITS = 54
type State = dict[str, Any]


# ── the fixture as setup leaves it ──────────────────────────────────────────


def _marker(blob: Blob) -> State:
    return {"blob_ref": blob.id, "mode": "inline_content", "sha256": hashlib.sha256(blob.data).hexdigest()}


def _parts(pointer: str) -> list[str]:
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def _owner(state: State, pointer: str) -> tuple[State, str]:
    """The mapping holding a node option pointer's last key, and that key."""
    parts = _parts(pointer)
    assert parts[0] == "nodes" and parts[2] == "options", pointer
    container: State = next(item for item in state["nodes"] if item["id"] == parts[1])["options"]
    for part in parts[3:-1]:
        container = container[part]
    return container, parts[-1]


def _slot_pointer(slot: str) -> str:
    """``node:STEP.options.a.b`` (a manifest slot) as a pointer into the state."""
    node, path = slot.removeprefix("node:").split(".options.", 1)
    return f"/nodes/{node}/options/" + path.replace(".", "/")


def fixture_digest(files: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode() + b"\0" + hashlib.sha256(files[name]).digest())
    return digest.hexdigest()


def fixture_graph() -> tuple[State, dict[str, Blob], dict[str, bytes]]:
    """The fx-acc30 graph as ``setup`` leaves it: imported, every prompt slot bound to its uploaded file, cards approved."""
    from elspeth.web.composer.yaml_importer import composition_state_from_runtime_yaml

    fixture = build_fixture(preset_spec("acceptance", **FIXTURE_SPEC))
    state: State = composition_state_from_runtime_yaml(fixture.yaml_text).to_dict()
    state.update(id="state-0", is_valid=True, validation_errors=None)
    for node in state["nodes"]:
        # setup approves each import card; the rows it settles are server-written and play no part here.
        if "interpretation_requirements" in node["options"]:
            del node["options"]["interpretation_requirements"]
        if "description" not in node:
            node["description"] = None
    blobs: dict[str, Blob] = {}
    for number, file in enumerate(fixture.files):
        blob = Blob(id=f"upload-{number}", filename=file.name, data=file.content, mime_type=file.mime_type)
        blobs[blob.id] = blob
        assert file.slot is not None
        container, key = _owner(state, _slot_pointer(file.slot))
        container[key] = _marker(blob)
    return state, blobs, {file.name: file.content for file in fixture.files}


# ── a Composer that makes exactly the asked-for change ─────────────────────


def _pending_error(node_id: str) -> State:
    # The shape the server's state carries while a prompt review card is open (test_composer_edit_battery.py).
    return {
        "error_code": "interpretation_review_pending",
        "component": node_id,
        "message": f"llm_prompt_template review pending for transform '{node_id}': llm_prompt_template:{node_id}",
    }


def _slot_paths(options: Mapping[str, Any]) -> list[tuple[str, Any]]:
    slots = [(key, options[key]) for key in ("prompt_template", "system_prompt") if key in options]
    queries = options["queries"] if "queries" in options else {}
    slots.extend((f"queries.{name}.template", entry["template"]) for name, entry in queries.items() if "template" in entry)
    return slots


class PromptComposer:
    """Rewrites each asked-for prompt file as a new file holding exactly the change; ``mutate`` breaks one turn on purpose."""

    def __init__(self, edits: list[State], session: Session, mutate: Callable[[str, State, PromptComposer], None] | None = None) -> None:
        self.by_message = {edit["message"]: edit for edit in edits if edit["applicable"]}
        self.session = session
        self.mutate = mutate
        self.turn = 0
        self.open_errors = True
        # Steps whose card this turn does not open (a mutation sets it for one turn).
        self.hidden_cards: set[str] = set()

    def text(self, state: State, pointer: str) -> str:
        container, key = _owner(state, pointer)
        return self.session.blobs[container[key]["blob_ref"]].data.decode()

    def write(self, state: State, pointer: str, text: str, name: str | None = None) -> Blob:
        """A new file the Composer wrote, bound at ``pointer`` (``wire_blob_inline_ref``)."""
        node = _parts(pointer)[1]
        blob = Blob(
            id=f"written-{self.turn}-{len(self.session.blobs)}",
            filename=name or f"{node}_{self.turn}.md",
            data=text.encode(),
            created_by="assistant",
            creation_modality="llm_generated",
        )
        self.session.blobs[blob.id] = blob
        container, key = _owner(state, pointer)
        container[key] = _marker(blob)
        return blob

    def __call__(self, current: State, message: str) -> FakeTurn:
        self.turn += 1
        edit = self.by_message[message]
        state = current
        before = {node["id"]: prompt_surface(node["options"]) for node in state["nodes"]}
        names = {expectation["bound"]: expectation["file"] for expectation in edit["expect"] if "bound" in expectation}
        for expectation in edit["expect"]:
            if "text" in expectation:
                old, new = expectation["replace"]
                pointer = expectation["text"]
                self.write(state, pointer, self.text(state, pointer).replace(old, new), names[pointer] if pointer in names else None)
            else:
                assert "bound" in expectation or "unchanged" in expectation, expectation
        self.hidden_cards = set()
        if self.mutate is not None:
            self.mutate(edit["id"], state, self)
        reviews: State = {}
        errors = list(state["validation_errors"] or [])
        for item in state["nodes"]:
            if prompt_surface(item["options"]) != before[item["id"]]:
                if item["id"] not in self.hidden_cards:
                    reviews.update(self.review(item))
                if self.open_errors:
                    errors.append(_pending_error(item["id"]))
        state["validation_errors"] = errors or None
        state["is_valid"] = not errors
        state["id"] = f"state-{self.turn}"
        tools = [*edit["tools_all"], *edit["tools_any"][:1]]
        calls = [
            {"id": f"c{self.turn}-{i}", "type": "function", "function": {"name": t, "arguments": "{}"}, "outcome": "applied"}
            for i, t in enumerate(tools)
        ]
        audit = [{"_kind": "llm_call_audit", "call": {"status": "success", "latency_ms": 900, "completion_tokens": 100 + self.turn}}]
        return FakeTurn(state=state, audit=audit, assistant_tool_calls=calls, reviews=reviews)

    def review(self, item: State) -> State:
        options = item["options"]
        rows = options["interpretation_requirements"] if "interpretation_requirements" in options else []
        rows.append({"id": f"req-{self.turn}-{item['id']}", "kind": "llm_prompt_template", "status": "pending"})
        options["interpretation_requirements"] = rows
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
                "snapshot": {
                    "node_id": item["id"],
                    "anchor": f"anchor-{self.turn}",
                    "authored_surface": prompt_surface(options),
                    "bindings": bindings,
                },
                "pending": True,
            }
        }


def make_run(
    tmp_path: Path, *, plan_path: Path = PROMPT_EDITS_PLAN, mutate: Callable[[str, State, PromptComposer], None] | None = None
) -> tuple[battery.EditBattery, FakeDeployment, PromptComposer, State]:
    plan = load_plan(plan_path)
    state, blobs, _ = fixture_graph()
    baseline = copy.deepcopy(state)
    session = Session(owner="tester", state=state, blobs=blobs)
    edits = resolve_edits(plan, resolve_targets(plan, baseline, {}), baseline)
    fake = FakeDeployment(passwords={"tester": "pw"}, sessions={SESSION: session})
    composer = PromptComposer(edits, session, mutate)
    fake.provider = composer

    def resolve_clears(method: str, path: str) -> None:
        # The resolve settles the card, and with it the step's pending-review error.
        if method == "POST" and path.endswith("/resolve"):
            live = fake.sessions[SESSION].state
            node = fake.sessions[SESSION].reviews[path.rsplit("/", 2)[1]]["snapshot"]["node_id"]
            left = [error for error in live["validation_errors"] or [] if error != _pending_error(node)]
            live["validation_errors"] = left or None
            live["is_valid"] = not left

    fake.on_request = resolve_clears
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
    return run, fake, composer, baseline


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


# ── the plan on its fixture ─────────────────────────────────────────────────


def test_the_rebuilt_fixture_is_fx_acc30() -> None:
    _, _, files = fixture_graph()
    assert len(files) == 43
    assert fixture_digest(files) == FIXTURE_FILES_DIGEST


def test_the_plan_resolves_on_the_fixture_and_every_target_is_an_llm_step_with_file_prompts() -> None:
    plan = load_plan(PROMPT_EDITS_PLAN)
    state, _, _ = fixture_graph()
    targets = resolve_targets(plan, state, {})
    llm_steps = sorted(node["id"] for node in state["nodes"] if node["plugin"] == "llm")
    assert sorted(targets) == llm_steps and len(llm_steps) == 14
    assert all(targets[name] == name for name in targets)
    edits = resolve_edits(plan, targets, state)
    assert len(edits) == PROMPT_EDITS and all(edit["applicable"] for edit in edits)
    assert len(edits) >= battery.GOLD_MINIMUM_EDITS + 2
    # Every LLM step has a prompt edited.
    pointers = [expectation[key] for edit in edits for expectation in edit["expect"] for key in ("text", "unchanged") if key in expectation]
    assert {_parts(pointer)[1] for pointer in pointers} == set(llm_steps)
    # On the fixture, the first edit of each prompt has its change false and each no-op its premise true.
    _, blobs, _ = fixture_graph()

    def read_text(marker: Mapping[str, Any]) -> str:
        return blobs[marker["blob_ref"]].data.decode()

    view = graph_view(state)
    first_touch: dict[str, State] = {}
    for edit in edits:
        for expectation in edit["expect"]:
            for key in ("text", "unchanged"):
                if key in expectation and expectation[key] not in first_touch:
                    first_touch[expectation[key]] = edit
    # 26 of the fixture's 41 prompt slots (14 system prompts, 10 prompts, 17 query templates) are edited.
    assert len(first_touch) == 26
    for pointer, edit in first_touch.items():
        assert preconditions(edit, view, view, read_text) == [], (edit["id"], pointer)


def test_a_target_the_graph_lacks_or_that_is_not_a_file_prompt_llm_step_is_refused() -> None:
    plan = load_plan(PROMPT_EDITS_PLAN)
    state, _, _ = fixture_graph()
    without = copy.deepcopy(state)
    without["nodes"] = [node for node in without["nodes"] if node["id"] != "risk_review"]
    with pytest.raises(UsageError, match=r"risk_review \(the LLM step risk_review with a prompt held in a file\)"):
        resolve_targets(plan, without, {})
    with pytest.raises(UsageError, match="--target risk_review=prepare_risk_review is not the LLM step risk_review"):
        resolve_targets(plan, state, {"risk_review": "prepare_risk_review"})
    inline = copy.deepcopy(state)
    options = next(node for node in inline["nodes"] if node["id"] == "equity_review")["options"]
    options["system_prompt"], options["prompt_template"] = "Be fair.", "Review {{ row['programme'] }}."
    with pytest.raises(UsageError, match="equity_review"):
        resolve_targets(plan, inline, {})


def test_the_prompt_plan_runs_gold_end_to_end_and_leaves_the_graph_as_found(tmp_path: Path) -> None:
    run, fake, _, baseline = make_run(tmp_path)
    summary = run.run()
    failed = [(result["id"], result["findings"]) for result in results(run) if not result["passed"]]
    assert failed == []
    assert summary["applicable"] == PROMPT_EDITS and summary["complete"] is True
    assert summary["gold"] is True
    assert summary["as_found_differences"] == []
    # Every card the plan names was approved: one per changed step, two for a two-step message, none for a no-op.
    approvals = {result["id"]: sorted(item["node_id"] for item in result["approvals"]) for result in results(run)}
    assert approvals["reviews-system-add"] == ["additional_review_1", "additional_review_2"]
    assert approvals["letter-and-safety-remove"] == ["cs2_scoring", "feedback_letter"]
    assert [edit_id for edit_id, nodes in approvals.items() if not nodes] == [
        "risk-system-no-op",
        "letter-system-no-op",
        "overall-evidence-no-op",
    ]
    assert all(len(nodes) in (1, 2) for edit_id, nodes in approvals.items() if not edit_id.endswith("no-op"))
    # The files the Composer wrote are listed, the two it was asked to name among them.
    written = {item["filename"] for item in summary["files_left_in_session"]}
    assert {"battery_equity_system.txt", "battery_moderation_prompt.txt"} <= written
    session = fake.sessions[SESSION]
    assert {blob.created_by for blob in session.blobs.values() if blob.filename in written} == {"assistant"}
    # Output tokens are recorded per edit (the simulated turn n reports 100 + n) and totalled.
    assert [result["evidence"]["completion_tokens"] for result in results(run)] == [100 + turn for turn in range(1, PROMPT_EDITS + 1)]
    assert summary["totals"]["completion_tokens"] == sum(100 + turn for turn in range(1, PROMPT_EDITS + 1))
    assert [result["completion_tokens"] for result in results(run)] == [100 + turn for turn in range(1, PROMPT_EDITS + 1)]
    # Every edit is judged byte for byte, each prompt's size before and after recorded: a wording
    # edit's sizes differ by exactly its replacement, a no-op's not at all.
    assert summary["byte_exact"] == {"edits_checked": PROMPT_EDITS, "exact": PROMPT_EDITS, "not_exact": []}
    plan = {edit["id"]: edit for edit in load_plan(PROMPT_EDITS_PLAN).edits}
    for result in results(run):
        assert result["byte_exact"] is True
        expect = [item for item in plan[result["id"]]["expect"] if "text" in item or "unchanged" in item]
        assert len(result["text_checks"]) == len(expect)
        for record, item in zip(result["text_checks"], expect, strict=True):
            grown = len(item["replace"][1].encode()) - len(item["replace"][0].encode()) if "text" in item else 0
            assert record["bytes_after"] - record["bytes_before"] == grown and record["detail"] is None, result["id"]

    def read_text(marker: Mapping[str, Any]) -> str:
        return session.blobs[marker["blob_ref"]].data.decode()

    assert as_found_differences(baseline, session.state, read_text) == []
    assert leftover_findings(session.state, read_text) == []


def test_every_message_names_its_step_and_no_tool_and_every_wording_edit_requires_a_blocking_agent_written_card() -> None:
    plan = load_plan(PROMPT_EDITS_PLAN)
    assert plan.forbidden_tools == ("set_pipeline",) and plan.pass_rule == "gold" and plan.start is None
    for edit in plan.edits:
        assert not any(tool in edit["message"] for tool in battery.EDITING_TOOLS), edit["id"]
        # Normal use is measured: no message coaches the model to be careful with the rest of the text,
        # tells a no-op what to do when the sentence is already there, or calls a prompt a "file" for its storage.
        for coaching in ("exactly", "every other character", "If it already", "prompt file", "template file"):
            assert coaching not in edit["message"], (edit["id"], coaching)
        if edit_plan.is_no_op(edit):
            assert edit["message"].startswith("Make sure the "), edit["id"]
            assert set(edit) == {"id", "message", "expect"}
            continue
        cards = edit["approve"] if type(edit["approve"]) is list else [edit["approve"]]
        nodes = {_parts(expectation["text"])[1] for expectation in edit["expect"] if "text" in expectation}
        assert {card["node"] for card in cards} == nodes, edit["id"]
        assert all(
            card == {"kind": "llm_prompt_template", "node": card["node"], "card": "agent_written", "blocks_run": True} for card in cards
        )
        # Every wording change is judged byte for byte, in a file.
        assert all("replace" in item and item["form"] == "file" and item["exact"] is True for item in edit["expect"] if "text" in item)
        assert "wire_blob_inline_ref" in edit["tools_all"]


# ── the byte-exact text check on a 21 KB prompt file ───────────────────────

RISK_POINTER = "/nodes/risk_review/options/system_prompt"
MARKER = " Battery marker risk one."


def _risk_rubric() -> bytes:
    """risk_review.rubric.md as fx-acc30 ships it: 21,755 bytes, ASCII, LF line ends, one final newline."""
    state, blobs, _ = fixture_graph()
    container, key = _owner(state, RISK_POINTER)
    data = blobs[container[key]["blob_ref"]].data
    assert len(data) == 21_755 and data.endswith(b"describe.\n") and b"\r" not in data and data.isascii()
    return data


def _risk_anchor() -> str:
    """The sentence the plan's first risk_review system-prompt edit adds its marker after."""
    edit = next(item for item in load_plan(PROMPT_EDITS_PLAN).edits if item["id"] == "risk-system-add")
    old, new = edit["expect"][0]["replace"]
    assert new == old + MARKER
    anchor: str = old
    return anchor


def _byte_verdict(was: bytes, now: bytes, old: str, new: str, *, pinned: bytes | None = None) -> tuple[State, list[str]]:
    """The exact-text record and the check's failures for one file rewrite (``pinned``: the bytes the new marker's sha256 names)."""
    files = {"before": was, "after": now}

    def view(name: str, sha_of: bytes) -> State:
        marker = {"blob_ref": name, "mode": "inline_content", "sha256": hashlib.sha256(sha_of).hexdigest()}
        return {"sources": {}, "nodes": {"risk_review": {"options": {"system_prompt": marker}}}, "outputs": {}, "edges": {}, "metadata": {}}

    def read_bytes(marker: Mapping[str, Any]) -> bytes:
        return files[marker["blob_ref"]]

    def read_text(marker: Mapping[str, Any]) -> str:
        return files[marker["blob_ref"]].decode("utf-8")

    expectation = {"text": RISK_POINTER, "replace": [old, new], "form": "file", "exact": True}
    edit = {"expect": [expectation], "edges_touching": [], "may_change": []}
    before, after = view("before", was), view("after", now if pinned is None else pinned)
    record = edit_plan.exact_text_record(expectation, before, after, read_text, read_bytes)
    failures, _ = check_expectations(edit, before, after, before, read_text, read_bytes=read_bytes)
    return record, failures


def _flip_case(data: bytes, offset: int) -> bytes:
    assert chr(data[offset]).isalpha(), (offset, data[offset : offset + 1])
    return data[:offset] + chr(data[offset]).swapcase().encode() + data[offset + 1 :]


def _letter_from(data: bytes, start: int, step: int) -> int:
    """The first ASCII letter's offset at or after ``start`` (``step`` 1) or at or before it (``step`` -1)."""
    offset = start
    while not chr(data[offset]).isalpha():
        offset += step
    return offset


def test_exactly_the_requested_change_to_a_21_kb_file_is_byte_exact() -> None:
    was, anchor = _risk_rubric(), _risk_anchor()
    correct = was.replace(anchor.encode(), (anchor + MARKER).encode())
    record, failures = _byte_verdict(was, correct, anchor, anchor + MARKER)
    assert failures == []
    assert record == {
        "pointer": RISK_POINTER,
        "bytes_before": 21_755,
        "bytes_after": 21_755 + len(MARKER),
        "byte_exact": True,
        "first_difference_at": None,
        "detail": None,
    }


def _wrong(name: str, was: bytes, anchor: str) -> tuple[bytes, bytes, str, int]:
    """``(before, after, new sentence, first differing byte)`` for one way of getting the file wrong."""
    new = anchor + MARKER
    correct = was.replace(anchor.encode(), new.encode())
    edited_at = correct.index(new.encode())
    if name == "one_char_far_away":
        # The last letter of the file, more than 10 KB from the edited sentence.
        offset = _letter_from(correct, len(correct) - 1, -1)
        assert abs(offset - edited_at) > 10_000
        return was, _flip_case(correct, offset), new, offset
    if name == "final_newline_added":
        return was, correct + b"\n", new, len(correct)
    if name == "final_newline_removed":
        return was, correct[:-1], new, len(correct) - 1
    if name == "lf_to_crlf":
        return was, correct.replace(b"\n", b"\r\n"), new, correct.index(b"\n")
    if name == "requested_e_acute_decomposed":
        # The asked-for sentence holds é (U+00E9); the file holds e + U+0301, which looks the same.
        new = anchor + " Battery marker café."
        correct = was.replace(anchor.encode(), new.encode())
        decomposed = correct.replace("é".encode(), "é".encode())
        return was, decomposed, new, correct.index("é".encode())
    if name == "untouched_e_acute_decomposed":
        # An é already in the file, far from the edit, decomposed while the edit itself is right.
        with_acute = was[:-1] + " Café review.\n".encode()
        correct = with_acute.replace(anchor.encode(), new.encode())
        return with_acute, correct.replace("é".encode(), "é".encode()), new, correct.index("é".encode())
    assert name == "neighbour_sentence_changed"
    # The requested change is made, and one letter of the very next sentence is changed too.
    offset = _letter_from(correct, edited_at + len(new.encode()), 1)
    after = _flip_case(correct, offset)
    assert new.encode() in after
    return was, after, new, offset


@pytest.mark.parametrize(
    "name",
    [
        "one_char_far_away",
        "final_newline_added",
        "final_newline_removed",
        "lf_to_crlf",
        "requested_e_acute_decomposed",
        "untouched_e_acute_decomposed",
        "neighbour_sentence_changed",
    ],
)
def test_any_other_byte_difference_in_a_21_kb_file_fails_and_names_the_first_differing_byte(name: str) -> None:
    anchor = _risk_anchor()
    was, after, new, offset = _wrong(name, _risk_rubric(), anchor)
    expected = was.replace(anchor.encode(), new.encode())
    assert expected != after
    record, failures = _byte_verdict(was, after, anchor, new)
    detail = f"first_difference_at_byte_{offset}_expected_{len(expected)}_bytes_got_{len(after)}"
    assert failures == [f"expect[0]:text:{RISK_POINTER}:{detail}"]
    assert record["byte_exact"] is False and record["first_difference_at"] == offset and record["detail"] == detail
    assert record["bytes_before"] == len(was) and record["bytes_after"] == len(after)


def test_a_file_whose_bytes_are_not_the_bytes_its_marker_pins_fails() -> None:
    was, anchor = _risk_rubric(), _risk_anchor()
    correct = was.replace(anchor.encode(), (anchor + MARKER).encode())
    record, failures = _byte_verdict(was, correct, anchor, anchor + MARKER, pinned=correct + b"\n")
    assert failures == [f"expect[0]:text:{RISK_POINTER}:bytes_differ_from_pinned_sha256"]
    assert record["byte_exact"] is False and record["first_difference_at"] is None


def test_an_exact_edit_whose_sentence_is_not_in_the_file_exactly_once_is_not_sent() -> None:
    was, anchor = _risk_rubric(), _risk_anchor()
    files = {"twice": was + anchor.encode() + b"\n", "absent": was.replace(anchor.encode(), b"")}

    def view(name: str) -> State:
        marker = {"blob_ref": name, "mode": "inline_content", "sha256": hashlib.sha256(files[name]).hexdigest()}
        return {"sources": {}, "nodes": {"risk_review": {"options": {"system_prompt": marker}}}, "outputs": {}, "edges": {}, "metadata": {}}

    def read_bytes(marker: Mapping[str, Any]) -> bytes:
        return files[marker["blob_ref"]]

    def read_text(marker: Mapping[str, Any]) -> str:
        return files[marker["blob_ref"]].decode()

    edit = {
        "expect": [{"text": RISK_POINTER, "replace": [anchor, anchor + MARKER], "form": "file", "exact": True}],
        "edges_touching": [],
        "may_change": [],
    }
    assert preconditions(edit, view("twice"), view("twice"), read_text, read_bytes=read_bytes) == ["precondition:expect[0]_anchor_count_2"]
    assert preconditions(edit, view("absent"), view("absent"), read_text, read_bytes=read_bytes) == [
        "precondition:expect[0]_anchor_count_0"
    ]


# ── each new rule, broken on purpose ───────────────────────────────────────


def test_a_rewritten_file_that_loses_its_final_newline_fails_the_byte_exact_check(tmp_path: Path) -> None:
    pointer = "/nodes/cs1_scoring/options/system_prompt"
    expected_sizes: list[int] = []

    def drop_newline(edit_id: str, state: State, composer: PromptComposer) -> None:
        if edit_id == "cs1s-rubric-add":
            correct = composer.text(state, pointer)
            expected_sizes.append(len(correct.encode()))
            composer.write(state, pointer, correct.rstrip("\n"))

    run, _, _, _ = make_run(tmp_path, mutate=drop_newline)
    run.run()
    result = result_of(run, "cs1s-rubric-add")
    [size] = expected_sizes
    # The text failure names the first differing byte (the missing final newline), and the
    # card it opened is left unapproved (its pending error then counts as new).
    assert result["findings"][0] == f"expect[0]:text:{pointer}:first_difference_at_byte_{size - 1}_expected_{size}_bytes_got_{size - 1}"
    # The result records the file's sizes and the byte-exact verdict, not only the finding.
    assert result["byte_exact"] is False
    assert result["text_checks"] == [
        {
            "pointer": pointer,
            # cs1_scoring.rubric.md as fx-acc30 ships it (its manifest: 20,953 bytes).
            "bytes_before": 20_953,
            "bytes_after": size - 1,
            "byte_exact": False,
            "first_difference_at": size - 1,
            "detail": f"first_difference_at_byte_{size - 1}_expected_{size}_bytes_got_{size - 1}",
        }
    ]
    assert result["findings"][1:] == [
        'new_validation_error:["interpretation_review_pending", "cs1_scoring", '
        + json.dumps(_pending_error("cs1_scoring")["message"])
        + "]"
    ]
    assert result["approvals"] == []


def test_a_rewritten_file_with_one_other_word_changed_fails_the_byte_exact_check(tmp_path: Path) -> None:
    pointer = "/nodes/risk_review/options/system_prompt"
    seen: list[tuple[int, int]] = []

    def other_word(edit_id: str, state: State, composer: PromptComposer) -> None:
        if edit_id == "risk-system-change-1":
            correct = composer.text(state, pointer).encode()
            seen.append((correct.index(b"assessment panel") + len(b"assessment "), len(correct)))
            composer.write(state, pointer, correct.decode().replace("assessment panel", "assessment board", 1))

    run, _, _, _ = make_run(tmp_path, mutate=other_word)
    run.run()
    result = result_of(run, "risk-system-change-1")
    [(offset, size)] = seen
    assert result["findings"][0] == f"expect[0]:text:{pointer}:first_difference_at_byte_{offset}_expected_{size}_bytes_got_{size}"
    assert result["byte_exact"] is False and result["text_checks"][0]["first_difference_at"] == offset
    assert result["passed"] is False and result["approvals"] == []
    # The edit before it, judged on the same file, passed: the instrument is not red on every turn.
    assert result_of(run, "risk-system-add")["passed"] is True


def test_a_change_to_another_slot_or_another_step_is_an_unrelated_change(tmp_path: Path) -> None:
    def also_elsewhere(edit_id: str, state: State, composer: PromptComposer) -> None:
        if edit_id == "cs1a-prompt-add":
            # The same text written again into a new file for the step's other slot, and another step's option touched.
            pointer = "/nodes/cs1_assessment/options/system_prompt"
            composer.write(state, pointer, composer.text(state, pointer))
            next(node for node in state["nodes"] if node["id"] == "cs2_assessment")["options"]["response_field"] = "cs2_other"

    run, _, _, _ = make_run(tmp_path, mutate=also_elsewhere)
    run.run()
    findings = result_of(run, "cs1a-prompt-add")["findings"]
    assert "unrelated_change:/nodes/cs1_assessment/options/system_prompt/blob_ref" in findings
    assert "unrelated_change:/nodes/cs2_assessment/options/response_field" in findings


def test_a_wording_edit_that_moves_the_text_to_another_slot_fails(tmp_path: Path) -> None:
    def moved(edit_id: str, state: State, composer: PromptComposer) -> None:
        if edit_id == "cs1e-prompt-add":
            options = next(node for node in state["nodes"] if node["id"] == "cs1_evidence_check")["options"]
            options["system_prompt"], options["prompt_template"] = options["prompt_template"], options["system_prompt"]

    run, _, _, _ = make_run(tmp_path, mutate=moved)
    run.run()
    findings = result_of(run, "cs1e-prompt-add")["findings"]
    assert any(
        item.startswith("expect[0]:text:/nodes/cs1_evidence_check/options/prompt_template:first_difference_at_byte_") for item in findings
    )
    assert any(item.startswith("unrelated_change:/nodes/cs1_evidence_check/options/system_prompt") for item in findings)


def test_a_card_that_does_not_block_the_run_is_a_finding_and_is_not_approved(tmp_path: Path) -> None:
    def no_error(edit_id: str, state: State, composer: PromptComposer) -> None:
        composer.open_errors = edit_id != "cs2a-system-add"

    run, fake, _, _ = make_run(tmp_path, mutate=no_error)
    run.run()
    result = result_of(run, "cs2a-system-add")
    assert result["findings"] == ["review_did_not_block_run:cs2_assessment"]
    assert result["approvals"] == []
    assert not [path for verb, path, _ in fake.requests if verb == "POST" and path.endswith("/resolve") and "cs2_assessment" in path]


def test_without_blocks_run_the_same_turn_passes(tmp_path: Path) -> None:
    """The control for the test above: the flag is what catches a card that does not block the run."""
    raw = json.loads(PROMPT_EDITS_PLAN.read_text())
    for edit in raw["edits"]:
        approve: Any = edit["approve"] if "approve" in edit else []
        cards: list[State] = approve if type(approve) is list else [approve]
        for card in cards:
            del card["blocks_run"]
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(raw))

    def no_error(edit_id: str, state: State, composer: PromptComposer) -> None:
        composer.open_errors = edit_id != "cs2a-system-add"

    run, _, _, _ = make_run(tmp_path, plan_path=path, mutate=no_error)
    run.run()
    assert result_of(run, "cs2a-system-add")["passed"] is True


def test_a_two_step_message_that_changes_only_one_step_fails(tmp_path: Path) -> None:
    def one_step_only(edit_id: str, state: State, composer: PromptComposer) -> None:
        if edit_id == "reviews-system-add":
            # The second step's file is put back: only one step changed.
            options = next(node for node in state["nodes"] if node["id"] == "additional_review_2")["options"]
            original = next(blob for blob in composer.session.blobs.values() if blob.filename == "additional_review_2.system.md")
            options["system_prompt"] = _marker(original)

    run, _, _, _ = make_run(tmp_path, mutate=one_step_only)
    run.run()
    result = result_of(run, "reviews-system-add")
    # The file left as it was differs from the asked-for one where the new sentence should start.
    assert any(
        item.startswith("expect[1]:text:/nodes/additional_review_2/options/system_prompt:first_difference_at_byte_")
        for item in result["findings"]
    )
    assert [record["byte_exact"] for record in result["text_checks"]] == [True, False] and result["byte_exact"] is False
    assert result["approvals"] == []


def test_a_two_step_message_whose_second_card_never_opens_is_a_finding(tmp_path: Path) -> None:
    def second_card_hidden(edit_id: str, state: State, composer: PromptComposer) -> None:
        if edit_id == "letter-and-safety-add":
            composer.hidden_cards = {"cs2_scoring"}

    run, _, _, _ = make_run(tmp_path, mutate=second_card_hidden)
    run.run()
    result = result_of(run, "letter-and-safety-add")
    assert "expected_review_card_missing:cs2_scoring" in result["findings"]
    # The card that did open is approved; the step whose card never opened stays blocked, which also fails the edit.
    assert [item["node_id"] for item in result["approvals"]] == ["feedback_letter"]
    assert any(
        item.startswith('after_approval:new_validation_error:["interpretation_review_pending", "cs2_scoring"')
        for item in result["findings"]
    )


def test_a_no_op_that_rewrites_the_file_with_the_same_text_fails(tmp_path: Path) -> None:
    def rewrite(edit_id: str, state: State, composer: PromptComposer) -> None:
        if edit_id == "letter-system-no-op":
            pointer = "/nodes/feedback_letter/options/system_prompt"
            composer.write(state, pointer, composer.text(state, pointer))

    run, _, _, _ = make_run(tmp_path, mutate=rewrite)
    run.run()
    findings = result_of(run, "letter-system-no-op")["findings"]
    assert "expect[0]:unchanged:/nodes/feedback_letter/options/system_prompt" in findings
    assert "unrelated_change:/nodes/feedback_letter/options/system_prompt/blob_ref" in findings
    # The card the rewrite opened is not approved, and its pending error is a new validation error.
    assert any(item.startswith('new_validation_error:["interpretation_review_pending", "feedback_letter"') for item in findings)
    assert any(item.startswith("no_op_file_added:") for item in findings)


def test_a_no_op_that_leaves_a_stray_file_fails(tmp_path: Path) -> None:
    def stray(edit_id: str, state: State, composer: PromptComposer) -> None:
        if edit_id == "overall-evidence-no-op":
            composer.session.blobs["stray"] = Blob(id="stray", filename="copy.md", data=b"x", created_by="assistant")

    run, _, _, _ = make_run(tmp_path, mutate=stray)
    run.run()
    assert result_of(run, "overall-evidence-no-op")["findings"] == ["no_op_file_added:stray"]


def test_a_no_op_whose_premise_is_false_is_not_sent(tmp_path: Path) -> None:
    raw = json.loads(PROMPT_EDITS_PLAN.read_text())
    edit = next(item for item in raw["edits"] if item["id"] == "letter-system-no-op")
    edit["expect"][0]["contains"] = "A sentence the file does not hold."
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(raw))
    run, _, _, _ = make_run(tmp_path, plan_path=path)
    run.run()
    result = result_of(run, "letter-system-no-op")
    assert result["status"] == "not_sent"
    assert result["findings"] == ["precondition:expect[0]_no_op_not_already_met"]
    checkpoint = run.store.read(run.name)
    assert checkpoint is not None and "no-op it asks for is not already met" in checkpoint["stopped_reason"]


def test_unchanged_holds_only_for_the_same_binding_with_the_pinned_bytes_and_the_sentence_once() -> None:
    blob = Blob(id="f", filename="p.md", data=b"One sentence. Two sentence.\n")
    view: State = {
        "sources": {},
        "nodes": {"s": {"options": {"system_prompt": _marker(blob)}}},
        "outputs": {},
        "edges": {},
        "metadata": {},
    }
    edit = {
        "expect": [{"unchanged": "/nodes/s/options/system_prompt", "contains": "Two sentence."}],
        "edges_touching": [],
        "may_change": [],
    }
    texts = {"f": blob.data.decode(), "g": blob.data.decode()}

    def read_text(marker: Mapping[str, Any]) -> str:
        return texts[marker["blob_ref"]]

    assert check_expectations(edit, view, view, view, read_text)[0] == []
    rebound = copy.deepcopy(view)
    rebound["nodes"]["s"]["options"]["system_prompt"]["blob_ref"] = "g"
    assert check_expectations(edit, view, rebound, view, read_text)[0] == ["expect[0]:unchanged:/nodes/s/options/system_prompt"]
    texts["f"] = "One sentence. Two sentence. Changed bytes.\n"
    assert check_expectations(edit, view, view, view, read_text)[0] == ["expect[0]:unchanged:/nodes/s/options/system_prompt"]
    texts["f"] = blob.data.decode()
    twice = {**edit, "expect": [{"unchanged": "/nodes/s/options/system_prompt", "contains": "sentence."}]}
    assert check_expectations(twice, view, view, view, read_text)[0] == ["expect[0]:unchanged:/nodes/s/options/system_prompt"]


def test_a_marker_left_in_a_query_template_is_a_leftover_and_cleanup_asks_for_it() -> None:
    state, blobs, _ = fixture_graph()
    pointer = "/nodes/overall_rubric/options/queries/overall_ethics/template"
    container, key = _owner(state, pointer)
    original = blobs[container[key]["blob_ref"]]
    marked = Blob(id="marked", filename="q.md", data=original.data + b"Battery marker left behind.\n", created_by="assistant")
    blobs[marked.id] = marked
    container[key] = _marker(marked)

    def read_text(marker: Mapping[str, Any]) -> str:
        return blobs[marker["blob_ref"]].data.decode()

    assert leftover_findings(state, read_text) == [pointer]
    edit = battery.next_cleanup_edit(state, read_text, [], None, None, 1)
    assert edit is not None
    assert edit["expect"] == [{"text": pointer, "remove": "Battery marker left behind."}]
    assert "template of the query overall_ethics of the step overall_rubric" in edit["message"]


def test_as_found_compares_query_templates_by_text() -> None:
    state, blobs, _ = fixture_graph()
    baseline = copy.deepcopy(state)
    pointer = "/nodes/cs1_scoring/options/queries/cs1_safety/template"
    container, key = _owner(state, pointer)
    original = blobs[container[key]["blob_ref"]]
    same = Blob(id="same", filename="copy.md", data=original.data, created_by="assistant")
    blobs[same.id] = same
    container[key] = _marker(same)

    def read_text(marker: Mapping[str, Any]) -> str:
        return blobs[marker["blob_ref"]].data.decode()

    # A query template re-bound to a file with the same text is the same prompt...
    assert as_found_differences(baseline, state, read_text) == []
    other = Blob(id="other", filename="copy2.md", data=original.data + b"One more line.\n", created_by="assistant")
    blobs[other.id] = other
    container[key] = _marker(other)
    # ...and one with other text is a difference.
    assert as_found_differences(baseline, state, read_text) == [pointer + "/text"]


def test_plan_validation_refuses_malformed_prompt_plan_fields(tmp_path: Path) -> None:
    raw = json.loads(PROMPT_EDITS_PLAN.read_text())

    def write(plan: State) -> Path:
        path = tmp_path / "plan.json"
        path.write_text(json.dumps(plan))
        return path

    def edit(plan: State, edit_id: str) -> State:
        found: State = next(item for item in plan["edits"] if item["id"] == edit_id)
        return found

    def as_append(expectation: State) -> None:
        # ``exact`` judges a replacement only: an append ignores trailing whitespace.
        del expectation["replace"]
        expectation["append"] = "Battery marker appended."

    cases: list[tuple[Callable[[State], None], str]] = [
        (lambda p: p["targets"]["risk_review"].update(id=""), "id must name a step"),
        (lambda p: p["targets"]["risk_review"].update(prompt="file"), "takes"),
        (lambda p: edit(p, "letter-system-no-op").update(tools_any=["create_blob"]), "no-op edit"),
        (lambda p: edit(p, "letter-system-no-op")["expect"][0].update(contains=" "), "malformed unchanged"),
        (lambda p: edit(p, "cs1a-system-add")["expect"].append({"unchanged": "/nodes/x", "contains": "y"}), "cannot be mixed"),
        (lambda p: edit(p, "cs1a-system-add")["approve"].update(blocks_run=False), "blocks_run true"),
        (lambda p: edit(p, "reviews-system-add").update(approve=[]), "approve must be"),
        (lambda p: edit(p, "reviews-system-add")["approve"][1].update(node="{additional_review_1}"), "one per node"),
        (lambda p: edit(p, "cs1a-system-add")["expect"][0].update(exact=False), "malformed text"),
        (lambda p: edit(p, "cs1a-system-add")["expect"][0].update(exact="yes"), "malformed text"),
        (lambda p: as_append(edit(p, "cs1a-system-add")["expect"][0]), "malformed text"),
    ]
    for change, message in cases:
        broken = copy.deepcopy(raw)
        change(broken)
        with pytest.raises(UsageError, match=message):
            load_plan(write(broken))


# ── the other plans are unaffected ─────────────────────────────────────────


@pytest.mark.parametrize("plan_path", [DEFAULT_PLAN, MIXED_PLAN, FILE_BINDINGS_PLAN])
def test_the_other_shipped_plans_use_none_of_the_prompt_plan_extensions(plan_path: Path) -> None:
    plan = load_plan(plan_path)
    assert not any(spec["kind"] == "named_llm_step" for spec in plan.targets.values())
    for item in plan.edits:
        assert not any("unchanged" in expectation or "exact" in expectation for expectation in item["expect"])
        assert "approve" not in item or (type(item["approve"]) is dict and "blocks_run" not in item["approve"])
