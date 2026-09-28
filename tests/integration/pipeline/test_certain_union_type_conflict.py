"""A union coalesce whose every row would fail the runtime merge is refused at build (ADR-050 D8).

Two branches that each guarantee a field and certainly type it differently —
a derived-``str`` literal or an untyped field_mapper dotted extraction
against a carried concrete type — fail EVERY row with ``contract_type_conflict``
at the runtime merge. Both types are fixed before row 1 (the stamp table on
one side, a source declaration on the other), so the conflict is certain from
config and is refused at construction (S3), in every schema mode — the
all-observed case included, where ``merge_union_fields`` returns early and the
build used to be blind to it.

Soundness is pinned from the other side: with the refusal disabled, every
newly refused shape really does fail every row at runtime (no false refusal).
The residual — a type knowable only at row 1 (an observed upstream) — keeps
routing per row (``test_coalesce_group_failure_accounting``).

Expression typing makes ``row['price'] + 1`` an ``int`` rewrite that delivers;
the ``literal`` and ``dotted`` shapes remain certain conflicts (``str`` vs
``int`` and ``any`` vs ``int``, respectively).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.core.dag import builder as dag_builder

_CERTAIN_CONFLICT_MARKER = "Every row would fail this merge at runtime"


def _fork_gate() -> dict[str, Any]:
    return {
        "name": "fork_gate",
        "input": "raw",
        "condition": "True",
        "routes": {"true": "fork", "false": "out"},
        "fork_to": ["path_a", "path_b"],
    }


def _transform(name: str, plugin: str, branch: str, out: str, options: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "plugin": plugin, "input": branch, "on_success": out, "on_error": "discard", "options": options}


def _observed(**options: Any) -> dict[str, Any]:
    return {"schema": {"mode": "observed"}, **options}


def _coalesce(policy: str = "require_all") -> dict[str, Any]:
    return {
        "name": "merge_results",
        "branches": {"path_a": "out_a", "path_b": "out_b"},
        "policy": policy,
        "merge": "union",
        "on_success": "out",
        **({"timeout_seconds": 5} if policy == "best_effort" else {}),
    }


def _csv_source(tmp_path: Path, fields: list[str]) -> dict[str, Any]:
    path = tmp_path / "in.csv"
    path.write_text("id,price\n1,10\n2,20\n")
    return {
        "plugin": "csv",
        "on_success": "raw",
        "options": {"path": str(path), "on_validation_failure": "discard", "schema": {"mode": "fixed", "fields": fields}},
    }


def _json_source(tmp_path: Path) -> dict[str, Any]:
    path = tmp_path / "in.jsonl"
    path.write_text('{"id": 1, "r": 5, "meta": {"p": 7}}\n{"id": 2, "r": 6, "meta": {"p": 8}}\n')
    return {
        "plugin": "json",
        "on_success": "raw",
        "options": {
            "path": str(path),
            "format": "jsonl",
            "on_validation_failure": "discard",
            "schema": {"mode": "fixed", "fields": ["id: int", "r: int", "meta: any"]},
        },
    }


def _settings(tmp_path: Path, *, source: dict[str, Any], transforms: list[dict[str, Any]], policy: str = "require_all") -> dict[str, Any]:
    return {
        "landscape": {"url": f"sqlite:///{tmp_path / 'audit.db'}"},
        "payload_store": {"backend": "filesystem", "base_path": str(tmp_path / "payloads")},
        "sources": {"src": source},
        "gates": [_fork_gate()],
        "transforms": transforms,
        "coalesce": [_coalesce(policy)],
        "sinks": {
            "out": {
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {"path": str(tmp_path / "out.jsonl"), "format": "jsonl", "schema": {"mode": "observed"}},
            }
        },
    }


def _rewrite_vs_carried(tmp_path: Path, *, expression: str, policy: str = "require_all") -> dict[str, Any]:
    """co2/co6: a value_transform rewrite of ``price`` on path_a, a passthrough of the fixed source's ``price: int`` on path_b."""
    return _settings(
        tmp_path,
        source=_csv_source(tmp_path, ["id: int", "price: int"]),
        transforms=[
            _transform("vt_a", "value_transform", "path_a", "out_a", _observed(operations=[{"target": "price", "expression": expression}])),
            _transform("pt_b", "passthrough", "path_b", "out_b", _observed()),
        ],
        policy=policy,
    )


def _dotted_vs_carried_rename(tmp_path: Path) -> dict[str, Any]:
    """c10: field_mapper ``meta.p -> q`` (created ``any``) on path_a, field_mapper ``r -> q`` (carries ``r: int``) on path_b."""
    return _settings(
        tmp_path,
        source=_json_source(tmp_path),
        transforms=[
            _transform("fm_a", "field_mapper", "path_a", "out_a", _observed(mapping={"meta.p": "q"})),
            _transform("fm_b", "field_mapper", "path_b", "out_b", _observed(mapping={"r": "q"})),
        ],
    )


_CERTAIN_SHAPES = {
    "literal": (lambda tmp_path: _rewrite_vs_carried(tmp_path, expression="'x'"), "price", "transform 'vt_a' (value_transform)"),
    "dotted": (_dotted_vs_carried_rename, "q", "transform 'fm_a' (field_mapper)"),
}
_CARRIED_DECLARERS = {"literal": "source 'src' (csv)", "dotted": "source 'src' (json)"}
_CONFLICTING_TYPES = {"literal": ("str", "int"), "dotted": ("any", "int")}


def _write(tmp_path: Path, settings: dict[str, Any]) -> Path:
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(yaml.safe_dump(settings, sort_keys=False))
    return settings_path


_PANEL_CHARACTERS = str.maketrans("", "", "│╭╮╰╯─")


def _cli(monkeypatch: pytest.MonkeyPatch, *args: str) -> tuple[int, str]:
    """Run the CLI; the output is returned with the error panel's box drawing removed and whitespace collapsed."""
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "certain-union-type-conflict")
    result = CliRunner().invoke(app, ["--no-dotenv", *args])
    return result.exit_code, " ".join(result.output.translate(_PANEL_CHARACTERS).split())


def _terminal_summary(db_path: Path) -> tuple[int, dict[str, int], set[str]]:
    """(non-terminal token count, completed outcome counts, failure reasons recorded at the coalesce)."""
    with sqlite3.connect(db_path) as conn:
        (non_terminal,) = conn.execute(
            "SELECT count(*) FROM tokens t "
            "WHERE NOT EXISTS (SELECT 1 FROM token_outcomes o WHERE o.token_id = t.token_id AND o.completed = 1)"
        ).fetchone()
        outcomes = dict(conn.execute("SELECT outcome, count(*) FROM token_outcomes WHERE completed = 1 GROUP BY outcome").fetchall())
        rows = conn.execute(
            "SELECT ns.error_json FROM node_states ns JOIN nodes n ON n.node_id = ns.node_id AND n.run_id = ns.run_id "
            "WHERE n.node_type = 'coalesce' AND ns.status = 'failed'"
        ).fetchall()
    return int(non_terminal), outcomes, {json.loads(error_json)["failure_reason"] for (error_json,) in rows}


@pytest.mark.parametrize("shape", sorted(_CERTAIN_SHAPES))
def test_certain_conflict_is_refused_at_build_naming_both_declarers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str) -> None:
    make_settings, field_name, rewriting_declarer = _CERTAIN_SHAPES[shape]
    settings_path = _write(tmp_path, make_settings(tmp_path))

    exit_code, flat = _cli(monkeypatch, "validate", "--settings", str(settings_path))
    assert exit_code == 1, flat
    assert f"receives incompatible types for field '{field_name}' in union merge" in flat
    assert _CERTAIN_CONFLICT_MARKER in flat
    assert rewriting_declarer in flat
    assert _CARRIED_DECLARERS[shape] in flat
    assert "on the output schema of every branch's last node" in flat

    run_exit, run_output = _cli(monkeypatch, "run", "--settings", str(settings_path), "--execute")
    assert run_exit == 1, run_output
    assert _CERTAIN_CONFLICT_MARKER in run_output


@pytest.mark.parametrize("shape", sorted(_CERTAIN_SHAPES))
def test_every_refused_shape_fails_every_row_with_the_refusal_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str) -> None:
    """T4 soundness: the refusal never rejects a pipeline that could deliver a row."""
    make_settings, field_name, _declarer = _CERTAIN_SHAPES[shape]
    monkeypatch.setattr(dag_builder, "_refuse_certain_union_type_conflict", lambda *_args, **_kwargs: None)
    settings_path = _write(tmp_path, make_settings(tmp_path))

    exit_code, output = _cli(monkeypatch, "run", "--settings", str(settings_path), "--execute")

    assert exit_code == 2, output  # Run FAILED: nothing succeeded
    non_terminal, outcomes, reasons = _terminal_summary(tmp_path / "audit.db")
    assert non_terminal == 0
    assert "success" not in outcomes
    assert outcomes["failure"] == 4  # 2 rows x 2 branch tokens
    first_type, second_type = _CONFLICTING_TYPES[shape]
    assert reasons == {
        f"contract_type_conflict: Cannot merge contracts: field '{field_name}' has conflicting types '{first_type}' and '{second_type}'"
    }, reasons


def test_derived_int_rewrite_builds_and_delivers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """co2: the derived int rewrite is compatible with the carried int branch."""
    settings = _rewrite_vs_carried(tmp_path, expression="row['price'] + 1")
    settings_path = _write(tmp_path, settings)
    validate_exit, validate_output = _cli(monkeypatch, "validate", "--settings", str(settings_path))
    assert validate_exit == 0, validate_output
    run_exit, run_output = _cli(monkeypatch, "run", "--settings", str(settings_path), "--execute")
    assert run_exit == 0, run_output
    assert (tmp_path / "out.jsonl").read_bytes() == b'{"id": 1, "price": 10}\n{"id": 2, "price": 20}\n'


@pytest.mark.parametrize(
    "transforms",
    [
        pytest.param(
            [
                _transform("vt_a", "value_transform", "path_a", "out_a", _observed(operations=[{"target": "bonus", "expression": "2"}])),
                _transform("vt_b", "value_transform", "path_b", "out_b", _observed(operations=[{"target": "bonus", "expression": "3"}])),
            ],
            id="co3-both-branches-create-any",
        ),
        pytest.param(
            [
                _transform(
                    "vt_a",
                    "value_transform",
                    "path_a",
                    "out_a",
                    _observed(operations=[{"target": "bonus", "expression": "row['price'] + 2"}]),
                ),
                _transform("pt_b", "passthrough", "path_b", "out_b", _observed()),
            ],
            id="co7-new-field-on-one-branch",
        ),
        pytest.param(
            [
                _transform(
                    "vt_a",
                    "value_transform",
                    "path_a",
                    "out_a",
                    {
                        "schema": {"mode": "flexible", "fields": ["price: int"]},
                        "operations": [{"target": "price", "expression": "row['price'] + 1"}],
                    },
                ),
                _transform("pt_b", "passthrough", "path_b", "out_b", {"schema": {"mode": "flexible", "fields": ["price: int"]}}),
            ],
            id="co8-remedy-type-declared-on-every-branch",
        ),
    ],
)
def test_controls_build_and_deliver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transforms: list[dict[str, Any]]) -> None:
    settings = _settings(tmp_path, source=_csv_source(tmp_path, ["id: int", "price: int"]), transforms=transforms)
    exit_code, output = _cli(monkeypatch, "run", "--settings", str(_write(tmp_path, settings)), "--execute")
    assert exit_code == 0, output
    assert len((tmp_path / "out.jsonl").read_text().splitlines()) == 2


@pytest.mark.parametrize("policy", ["best_effort", "first"])
def test_a_policy_that_can_merge_without_the_conflicting_sibling_is_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    """Certainty needs every branch in every merge; ``first`` merges one arrival and ``best_effort`` merges what arrived."""
    settings_path = _write(tmp_path, _rewrite_vs_carried(tmp_path, expression="'x'", policy=policy))
    exit_code, output = _cli(monkeypatch, "validate", "--settings", str(settings_path))
    assert exit_code == 0, output


def test_a_field_one_branch_does_not_guarantee_is_not_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Presence control: ``price`` is optional at the source, so path_b guarantees nothing about it."""
    settings = _settings(
        tmp_path,
        source=_csv_source(tmp_path, ["id: int", "price: int?"]),
        transforms=[
            _transform("vt_a", "value_transform", "path_a", "out_a", _observed(operations=[{"target": "price", "expression": "'x'"}])),
            _transform("pt_b", "passthrough", "path_b", "out_b", _observed()),
        ],
    )
    exit_code, output = _cli(monkeypatch, "validate", "--settings", str(_write(tmp_path, settings)))
    assert exit_code == 0, output
