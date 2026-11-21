"""The middle of three llm steps holds an inline prompt the plugin refuses: authoring names the step and the refusal.

Before the change, authoring validation built each llm step's plugin to probe
its output contract, and a construction failure became the class-name-only
warning "Computed contract probe for node 'bravo' failed during preview
(ValueError)". For a template the plugin refuses (over the byte bound, nested
too deeply, a number literal too long to read) that warning said neither why
nor which prompt, so with a dozen llm steps neither the user nor the model
could act on it, and no error entry was given at all. Now the step gets an
error entry carrying the plugin's own sentence; every other probe failure
keeps exactly the warning it gave before.

Covered: authoring validation directly, ``GET state`` after a YAML import, and
a composer turn (in-process ASGI client and the repository's job runner; only
the provider SDK call is scripted). Inputs are generated; expected texts are
spelled here so the file collects on the tree before the change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from tests.unit.web._sync_asgi_client import SyncASGITestClient
from tests.unit.web.composer.test_compose_loop_llm_audit import _make_llm_response as audited_response
from tests.unit.web.sessions.routes.composer.test_yaml_import_nested_template import _profile_app
from tests.unit.web.sessions.test_run_composer_turn import _run, _running_job

from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.yaml_importer import composition_state_from_runtime_yaml

LIMIT = 64 * 1024
STEPS = ("alpha", "bravo", "charlie")
GOOD = "Rate {{ row.text }}."

REFUSED = {
    "over-the-byte-bound": (
        "Rate {{ row.text }}. " + "x" * LIMIT,
        lambda text: (
            f"prompt_template is {len(text.encode())} bytes; the limit for one prompt template is {LIMIT} bytes. "
            "Shorten it, or move reference text into a file_inject step and read it from the row."
        ),
    ),
    "nested-loops": (
        "Rate {{ row.text }}. " + "{% for item in [1] %}" * 21 + "x" + "{% endfor %}" * 21,
        lambda _text: (
            "prompt_template is too deeply nested to compile: its loops or blocks nest deeper than Python allows. Flatten the nesting."
        ),
    ),
    "long-number-literal": (
        "Rate {{ row.text }}. {{ " + "9" * 5000 + " }}",
        lambda _text: "prompt_template contains a number literal too long to read. Shorten the number or quote it as text.",
    ),
}


def _pipeline(bravo_prompt: str) -> dict[str, Any]:
    connections = ["rows", "ab", "bc", "results"]
    return {
        "sources": {
            "source": {
                "plugin": "csv",
                "on_success": "rows",
                "options": {"schema": {"mode": "flexible", "fields": ["text: str"]}},
                "on_validation_failure": "discard",
            }
        },
        "transforms": [
            {
                "name": step,
                "plugin": "llm",
                "input": connections[index],
                "on_success": connections[index + 1],
                "on_error": "discard",
                "options": {
                    "profile": "text-model",
                    "system_prompt": "You rate short texts.",
                    "prompt_template": bravo_prompt if step == "bravo" else GOOD,
                    "response_field": f"r_{step}",
                    "required_input_fields": [],
                    "schema": {"mode": "observed"},
                },
            }
            for index, step in enumerate(STEPS)
        ],
        "sinks": {
            "results": {
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {"path": "outputs/out.jsonl", "format": "jsonl", "schema": {"mode": "observed"}},
            }
        },
    }


def _state(bravo_prompt: str) -> CompositionState:
    return composition_state_from_runtime_yaml(yaml.safe_dump(_pipeline(bravo_prompt), sort_keys=False))


def _probe_warnings(state: CompositionState) -> list[tuple[str, str]]:
    return [(entry.component, entry.message) for entry in state.validate().warnings if "contract probe" in entry.message]


@pytest.mark.parametrize("name", REFUSED)
def test_authoring_names_the_step_with_the_plugins_sentence(name: str) -> None:
    text, sentence = REFUSED[name]
    state = _state(text)

    validation = state.validate()

    assert ("node:bravo", "plugin_options_invalid", sentence(text)) in [
        (entry.component, entry.error_code, entry.message) for entry in validation.errors
    ]
    assert not any(entry.component in {"node:alpha", "node:charlie"} and "prompt template" in entry.message for entry in validation.errors)
    assert _probe_warnings(state) == []


def test_any_other_probe_failure_keeps_exactly_todays_warning() -> None:
    state = _state("Rate {{ row.text }")

    assert _probe_warnings(state) == [
        (
            "node:bravo",
            "Computed contract probe for node 'bravo' failed during preview (ValueError); pass-through output guarantees could not be established. "
            "Check this node's plugin options and run execution validation for an authoritative result.",
        )
    ]
    assert not any(entry.component == "node:bravo" and entry.error_code == "plugin_options_invalid" for entry in state.validate().errors)


async def _imported_session(tmp_path: Path, bravo_prompt: str) -> tuple[Any, Any, Any]:
    app, sessions = _profile_app(tmp_path)
    session = await sessions.create_session("alice", "Three steps", "local")
    imported = SyncASGITestClient(app).post(
        f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml.safe_dump(_pipeline(bravo_prompt), sort_keys=False)}
    )
    assert imported.status_code == 200, imported.text[:500]
    return app, sessions, session


@pytest.mark.asyncio
async def test_get_state_after_import_names_the_step(tmp_path: Path) -> None:
    text, sentence = REFUSED["over-the-byte-bound"]
    app, _sessions, session = await _imported_session(tmp_path, text)

    read = SyncASGITestClient(app).get(f"/api/sessions/{session.id}/state")

    assert read.status_code == 200, read.text[:500]
    entries = [(entry["component"], entry["message"]) for entry in read.json()["validation_errors"]]
    assert ("node:bravo", sentence(text)) in entries, entries


@pytest.mark.asyncio
async def test_a_turn_tells_the_planner_which_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.web.composer.availability import ComposerAvailability
    from elspeth.web.composer.service import ComposerServiceImpl

    text, sentence = REFUSED["over-the-byte-bound"]
    app, sessions, session = await _imported_session(tmp_path, text)
    planner_calls: list[dict[str, Any]] = []

    async def provider(**kwargs: Any) -> Any:
        if "response_format" in kwargs:
            verdict = {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "", "note": None, "publication_verdict": "ADMIT"}
            return audited_response(content=json.dumps(verdict))
        planner_calls.append(kwargs)
        return audited_response(content="The saved pipeline is unchanged.")

    monkeypatch.setattr("litellm.acompletion", provider)
    monkeypatch.setattr(
        "elspeth.web.composer.service.compute_availability",
        lambda **kwargs: ComposerAvailability(available=True, provider="test", model=kwargs["model"]),
    )
    app.state.composer_service = ComposerServiceImpl.for_trained_operator(
        catalog=app.state.catalog_service,
        settings=app.state.settings,
        sessions_service=sessions,
        session_engine=app.state.session_engine,
    )

    async with _running_job(app, sessions, session.id, kind="compose_message", content="Check the saved pipeline.") as job:
        completed = await _run(app, job)

    assert completed.status == "completed", completed
    told = json.dumps(planner_calls[0]["messages"])
    expected = json.dumps(sentence(text))[1:-1]
    assert expected in told, told[:3000]
    at = told.index(expected)
    assert "bravo" in told[max(0, at - 200) : at], told[max(0, at - 400) : at + 200]
