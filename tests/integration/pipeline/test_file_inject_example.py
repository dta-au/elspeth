"""The shipped file_inject example runs, and its content is configuration all the way down.

Every case is a real ``elspeth run`` (in-process CLI) read back through the
exit code, the sink files and the Landscape. The example settings are run from
a copy: the bound files sit beside the copied settings file, because
``content_file`` resolves against the settings directory.
"""

from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
import yaml
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.contracts.token_usage import TokenUsage
from elspeth.engine.executors.sink_effects import SinkEffectCoordinator, SinkEffectExecutionSeam, SinkEffectInjectedFault
from elspeth.plugins.transforms.llm.provider import FinishReason, LLMProvider, LLMQueryResult
from elspeth.plugins.transforms.llm.transform import LLMTransform
from tests.integration.pipeline.test_declared_input_miss import _query, _routed_reasons, _terminal_outcomes

EXAMPLE = Path("examples/file_inject")
_OBSERVED: dict[str, Any] = {"mode": "observed"}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _json_sink(path: Path) -> dict[str, Any]:
    return {"plugin": "json", "on_write_failure": "discard", "options": {"path": str(path), "format": "jsonl", "schema": _OBSERVED}}


def _cli(*args: str) -> Any:
    return CliRunner().invoke(app, list(args))


def _run_example(tmp_path: Path, settings_name: str) -> Any:
    """Run one shipped settings file from a copy of the example directory.

    Only the paths that name the repository are repointed. The ``files``
    entries are left exactly as shipped, so the copy proves ``content_file``
    resolution as a reader of the example would meet it.
    """
    shutil.copytree(EXAMPLE, tmp_path, dirs_exist_ok=True, ignore=shutil.ignore_patterns("output", "runs"))
    config = yaml.safe_load((tmp_path / settings_name).read_text(encoding="utf-8"))
    for source in config["sources"].values():
        source["options"]["path"] = str(tmp_path / Path(source["options"]["path"]).name)
    for name, sink in config["sinks"].items():
        sink["options"]["path"] = str(tmp_path / f"{name}.jsonl")
    config["landscape"]["url"] = f"sqlite:///{tmp_path / 'audit.db'}"
    config["payload_store"] = {"backend": "filesystem", "base_path": str(tmp_path / "payloads")}
    settings = tmp_path / "settings_under_test.yaml"
    settings.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return _cli("run", "--settings", str(settings), "--execute")


def _guidance_a() -> str:
    return (EXAMPLE / "guidance_a.md").read_text(encoding="utf-8")


def _guidance_b() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((EXAMPLE / "guidance_b.json").read_text(encoding="utf-8"))
    return loaded


def _guidance_c() -> list[dict[str, str]]:
    with (EXAMPLE / "guidance_c.csv").open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def test_select_example_writes_matching_guidance_per_row(tmp_path: Path) -> None:
    result = _run_example(tmp_path, "settings.yaml")

    assert result.exit_code == 0, result.output
    rows = _read_jsonl(tmp_path / "output.jsonl")
    with (EXAMPLE / "input.csv").open(newline="", encoding="utf-8") as stream:
        arriving = list(csv.DictReader(stream))
    assert [row["ticket_id"] for row in rows] == [row["ticket_id"] for row in arriving]
    assert {row["type"] for row in rows} == {"A", "B", "C"}
    expected: dict[str, Any] = {"A": _guidance_a(), "B": _guidance_b()["rubric"], "C": _guidance_c()}
    for row, original in zip(rows, arriving, strict=True):
        assert row == {**original, "guidance": expected[row["type"]]}
    # The three value shapes, stated outright so a fixture edit cannot blur them.
    assert type(expected["A"]) is str and expected["A"].endswith("\n")
    assert type(expected["B"]) is dict
    assert type(expected["C"]) is list and all(type(cell) is str for record in expected["C"] for cell in record.values())
    assert _terminal_outcomes(tmp_path) == {"success/default_flow": len(arriving)}


def test_all_example_writes_every_field_on_every_row(tmp_path: Path) -> None:
    result = _run_example(tmp_path, "settings_all.yaml")

    assert result.exit_code == 0, result.output
    rows = _read_jsonl(tmp_path / "output.jsonl")
    with (EXAMPLE / "input.csv").open(newline="", encoding="utf-8") as stream:
        arriving = list(csv.DictReader(stream))
    assert rows == [
        {**original, "style_guide": _guidance_a(), "review_rules": _guidance_b(), "checklist": _guidance_c()} for original in arriving
    ]
    assert len(rows) > 1


def test_miss_example_routes_the_unmatched_row_whole(tmp_path: Path) -> None:
    result = _run_example(tmp_path, "settings_miss.yaml")

    assert result.exit_code == 1, result.output
    with (EXAMPLE / "input_with_unknown.csv").open(newline="", encoding="utf-8") as stream:
        arriving = list(csv.DictReader(stream))
    unknown = [row for row in arriving if row["type"] not in {"A", "B", "C"}]
    assert len(unknown) == 1
    written = _read_jsonl(tmp_path / "output.jsonl")
    assert [row["ticket_id"] for row in written] == [row["ticket_id"] for row in arriving if row not in unknown]
    assert all("guidance" in row for row in written)
    # The routed row is the arriving row: no guidance field, not even a null.
    assert _read_jsonl(tmp_path / "unmatched.jsonl") == unknown
    assert _terminal_outcomes(tmp_path) == {"success/default_flow": len(written), "failure/on_error_routed": 1}
    (reason,) = _routed_reasons(tmp_path)
    assert (reason["reason"], reason["field"]) == ("file_inject_miss", "type")
    assert unknown[0]["type"] not in json.dumps(reason)


def _select_settings(tmp_path: Path, *, rows: list[dict[str, Any]], transforms: list[dict[str, Any]], inject_on_success: str) -> Path:
    """jsonl source -> file_inject (select on ``type``, files beside the settings) -> ``transforms`` -> sink."""
    (tmp_path / "in.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    shutil.copyfile(EXAMPLE / "guidance_a.md", tmp_path / "a.md")
    (tmp_path / "b.md").write_text("Guidance for B: confirm the billing period first.\n", encoding="utf-8")
    settings: dict[str, Any] = {
        "sources": {
            "src": {
                "plugin": "json",
                "on_success": "rows",
                "options": {
                    "path": str(tmp_path / "in.jsonl"),
                    "format": "jsonl",
                    "on_validation_failure": "discard",
                    "schema": {"mode": "flexible", "fields": ["ticket_id: str", "type: str", "summary: str"]},
                },
            }
        },
        "concurrency": {"max_workers": 1},
        "transforms": [
            {
                "name": "attach_guidance",
                "plugin": "file_inject",
                "input": "rows",
                "on_success": inject_on_success,
                "on_error": "quarantine",
                "options": {
                    "schema": _OBSERVED,
                    "mode": "select",
                    "key_field": "type",
                    "output_field": "guidance",
                    "files": {
                        "A": {"content_file": "a.md", "format": "text"},
                        "B": {"content_file": "b.md", "format": "text"},
                    },
                },
            },
            *transforms,
        ],
        "sinks": {"out": _json_sink(tmp_path / "out.jsonl"), "quarantine": _json_sink(tmp_path / "q.jsonl")},
        "landscape": {"url": f"sqlite:///{tmp_path / 'audit.db'}"},
        "payload_store": {"backend": "filesystem", "base_path": str(tmp_path / "payloads")},
    }
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(settings, sort_keys=False), encoding="utf-8")
    return path


_TICKETS = [
    {"ticket_id": "T-1", "type": "A", "summary": "Parcel arrived damaged"},
    {"ticket_id": "T-2", "type": "B", "summary": "Charged twice in March"},
]


def test_resume_is_refused_after_a_bound_file_is_edited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real crashed run, a real ``elspeth resume``: the edit refuses it, and undoing the edit admits it."""
    settings = _select_settings(tmp_path, rows=_TICKETS, transforms=[], inject_on_success="out")
    original = (tmp_path / "a.md").read_text(encoding="utf-8")

    def fail_at_reservation(self: SinkEffectCoordinator, seam: SinkEffectExecutionSeam) -> None:
        if seam is SinkEffectExecutionSeam.BEFORE_RESERVATION:
            raise SinkEffectInjectedFault(seam)

    with monkeypatch.context() as patched:
        patched.setattr(SinkEffectCoordinator, "_fault", fail_at_reservation)
        crashed = _cli("run", "-s", str(settings), "--execute")
    assert crashed.exit_code != 0, crashed.output
    assert "injected sink-effect fault at before_reservation" in crashed.output
    ((run_id,),) = _query(tmp_path, "select run_id from runs")
    assert not (tmp_path / "out.jsonl").exists()

    (tmp_path / "a.md").write_text(original + "One more instruction, added after the run started.\n", encoding="utf-8")
    refused = _cli("resume", run_id, "-s", str(settings), "--execute")

    assert refused.exit_code == 1, refused.output
    assert f"Cannot resume run {run_id}" in refused.output
    assert "configuration changed since checkpoint was created" in " ".join(refused.output.split())
    assert not (tmp_path / "out.jsonl").exists()

    # Control: the refusal is the edit and nothing else. With the file's
    # content restored, the same command resumes the same run to completion
    # and the rows carry the content the run started with.
    (tmp_path / "a.md").write_text(original, encoding="utf-8")
    resumed = _cli("resume", run_id, "-s", str(settings), "--execute")

    assert resumed.exit_code == 0, resumed.output
    written = {row["ticket_id"]: row["guidance"] for row in _read_jsonl(tmp_path / "out.jsonl")}
    assert written == {"T-1": original, "T-2": "Guidance for B: confirm the billing period first.\n"}


def test_field_is_usable_in_an_llm_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """file_inject -> llm: the injected field renders in the prompt, and the llm node may REQUIRE it."""
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "file-inject-example-fingerprint-key")
    providers: list[Mock] = []

    def create_provider(self: LLMTransform, *, llm_call_governance: Any = None) -> LLMProvider:
        del llm_call_governance
        provider = Mock(spec=LLMProvider)
        provider.execute_query.return_value = LLMQueryResult(
            content="Drafted reply.", usage=TokenUsage.known(10, 5), model="fake-model", finish_reason=FinishReason.STOP
        )
        providers.append(provider)
        return provider

    monkeypatch.setattr(LLMTransform, "_create_provider", create_provider)
    settings = _select_settings(
        tmp_path,
        rows=_TICKETS,
        inject_on_success="guided",
        transforms=[
            {
                "name": "draft_reply",
                "plugin": "llm",
                "input": "guided",
                "on_success": "out",
                "on_error": "quarantine",
                "options": {
                    "provider": "openrouter",
                    "api_key": "fake-key-not-checked",  # secret-scan: allow-this-line
                    "model": "openai/gpt-4o",
                    "prompt_template": "{{ row.guidance }}\n---\nTicket: {{ row.summary }}",
                    "response_field": "reply",
                    # No source declares `guidance`: only file_inject's own
                    # output guarantee can satisfy this at validation.
                    "required_input_fields": ["guidance", "summary"],
                    "schema": _OBSERVED,
                },
            }
        ],
    )

    assert _cli("validate", "-s", str(settings)).exit_code == 0
    result = _cli("run", "-s", str(settings), "--execute")

    assert result.exit_code == 0, result.output
    prompts = [
        "\n".join(message.content for message in call.args[0]) for provider in providers for call in provider.execute_query.call_args_list
    ]
    guidance_a = _guidance_a()
    (prompt_a,) = [prompt for prompt in prompts if "Parcel arrived damaged" in prompt]
    (prompt_b,) = [prompt for prompt in prompts if "Charged twice in March" in prompt]
    assert guidance_a.strip() in prompt_a
    assert "confirm the billing period first" not in prompt_a
    assert "confirm the billing period first" in prompt_b
    assert guidance_a.strip() not in prompt_b
    assert {row["ticket_id"]: row["reply"] for row in _read_jsonl(tmp_path / "out.jsonl")} == {
        "T-1": "Drafted reply.",
        "T-2": "Drafted reply.",
    }
