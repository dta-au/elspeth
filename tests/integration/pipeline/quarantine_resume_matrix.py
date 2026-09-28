"""Shared scenarios: a source-quarantined row survives a real crash and resume.

Every scenario is a REAL ``elspeth run --execute`` through the CLI, crashed at
a named window, then a real ``elspeth resume --execute``. No token, outcome or
work item is inserted by hand. The crash is injected at the sink-effect
coordinator's in-repo ``SinkEffectExecutionSeam`` fault point (the
``_fault`` test hook: the constructor's ``fault_hook`` is not threaded through
the orchestrator, so a CLI run has no other seam), either as a raise (the
failure ceremony runs) or as process death (``os._exit`` in a spawned child,
no ceremony). The mid-ingest window (W0) instead kills the SOURCE after it has
yielded the quarantined row: the plugin's own iterator dies, which is the
crash the window names.

The seam hook carries no sink identity, so a window is a (seam, stage) pair:
the hook fires at the first hit of its seam at which the quarantined row's
handoff is at that stage in the store (parked in no effect, or a member of a
sink effect). Every scenario then asserts the crash IMAGE names the
quarantine sink (W2: its effect in flight, unpublished; W3: in flight and
published; W4: finalized with its PENDING_SINK item still open; W1: no effect
exists), so a mis-aimed seam fails as a harness error instead of passing on
the wrong sink.

Windows (DESIGN-QR §5):
- W1: the first sink flush dies before any reservation; every token is parked
  PENDING_SINK (the lane's 3pt shape: another sink fails first).
- W2: the quarantine sink's effect is reserved, the write has not published.
- W3: the quarantine sink has published, the effect is not finalized.
- W4: the effect is finalized, the scheduler item is not yet terminal.
- W0: the source dies mid-read after the quarantined row was ingested.

Module-level names only: the process-death child is spawned and re-imports
this module.
"""

from __future__ import annotations

import json
import multiprocessing
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import select
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.contracts import RunStatus, TerminalOutcome, TerminalPath
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import (
    runs_table,
    sink_effect_members_table,
    sink_effects_table,
    token_outcomes_table,
    token_work_items_table,
    tokens_table,
)
from elspeth.engine.executors.sink_effects import SinkEffectCoordinator, SinkEffectExecutionSeam, SinkEffectInjectedFault
from tests.fixtures.landscape import expire_leader_seat, expire_sink_effect_lease

_GOOD = ({"id": 1, "line": "a"}, {"id": 2, "line": "b"}, {"id": 3, "line": "c"})
_FIXED = {"mode": "fixed", "fields": ["id: int", "line: str"]}
_OBSERVED = {"mode": "observed"}


def _jsonl(*lines: object) -> str:
    return "".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines)


@dataclass(frozen=True, slots=True)
class QuarantineKind:
    """One source plugin x quarantine error kind: three valid rows around one rejected row."""

    data_name: str
    data: str
    plugin: str
    options: dict[str, Any]
    good_ids: tuple[object, ...] = (1, 2, 3)


KINDS: dict[str, QuarantineKind] = {
    "json_parse": QuarantineKind("d.jsonl", _jsonl(*_GOOD[:2], "{not json", _GOOD[2]), "json", {"format": "jsonl", "schema": _FIXED}),
    "json_type": QuarantineKind(
        "d.jsonl", _jsonl(*_GOOD[:2], {"id": "x", "line": "b2"}, _GOOD[2]), "json", {"format": "jsonl", "schema": _FIXED}
    ),
    "json_nonobject": QuarantineKind("d.jsonl", _jsonl(*_GOOD[:2], "[1, 2]", _GOOD[2]), "json", {"format": "jsonl", "schema": _FIXED}),
    "json_nan": QuarantineKind(
        "d.jsonl", _jsonl(*_GOOD[:2], '{"id": NaN, "line": "n"}', _GOOD[2]), "json", {"format": "jsonl", "schema": _FIXED}
    ),
    "json_fieldcap": QuarantineKind(
        "d.jsonl", _jsonl(*_GOOD[:2], {f"c{i}": i for i in range(1025)}, _GOOD[2]), "json", {"format": "jsonl", "schema": _OBSERVED}
    ),
    "json_drift": QuarantineKind(
        "d.jsonl", _jsonl(*_GOOD[:2], {"id": "x", "line": "b2"}, _GOOD[2]), "json", {"format": "jsonl", "schema": _OBSERVED}
    ),
    "jsonarr_type": QuarantineKind(
        "d.json", json.dumps([*_GOOD[:2], {"id": "x", "line": "b2"}, _GOOD[2]]), "json", {"format": "json", "schema": _FIXED}
    ),
    "csv_parse": QuarantineKind("d.csv", "id,line\n1,a\n2,b\n9,z,extra\n3,c\n", "csv", {"schema": _FIXED}),
    "csv_type": QuarantineKind("d.csv", "id,line\n1,a\n2,b\nx,b2\n3,c\n", "csv", {"schema": _FIXED}),
    # CSV under an observed schema keeps every value a string, so no value can
    # drift; its observed-schema rejection is a structural one.
    "csv_observed_parse": QuarantineKind(
        "d.csv", "id,line\n1,a\n2,b\n9,z,extra\n3,c\n", "csv", {"schema": _OBSERVED}, good_ids=("1", "2", "3")
    ),
    "text_type": QuarantineKind(
        "d.txt", "1\n2\nnotint\n3\n", "text", {"column": "line", "schema": {"mode": "fixed", "fields": ["line: int"]}}, good_ids=()
    ),
}


@dataclass(frozen=True, slots=True)
class Window:
    """A crash window: the seam, the quarantine handoff's stage at which it fires, and the image it must leave."""

    seam: SinkEffectExecutionSeam
    stage: str  # "parked" (ingested, in no effect yet) | "in_effect" (a member of a sink effect)
    image: str  # "none" | "unpublished" | "published" | "finalized"


# The seam fires at its first hit whose quarantine handoff is at ``stage`` in
# the store, never at a hit count: sinks can flush mid-source (measured on
# PostgreSQL: a success-sink flush after row 1 took the second hit before the
# quarantined row was read). Sinks are declared "out" then "bad" (written
# unsorted below), so W1 kills the success sink's end-of-source flush while
# the quarantine handoff is still parked (the lane's 3pt shape).
WINDOWS: dict[str, Window] = {
    "W1": Window(SinkEffectExecutionSeam.BEFORE_RESERVATION, "parked", "none"),
    "W2": Window(SinkEffectExecutionSeam.BEFORE_EFFECT, "in_effect", "unpublished"),
    "W3": Window(SinkEffectExecutionSeam.AFTER_RETURN_BEFORE_FINALIZE, "in_effect", "published"),
    "W4": Window(SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE, "in_effect", "finalized"),
}


def write_settings(tmp_path: Path, kind: QuarantineKind, db_url: str) -> Path:
    data = tmp_path / kind.data_name
    data.write_text(kind.data, encoding="utf-8")
    for name in ("out", "bad"):
        (tmp_path / name).mkdir()
    (tmp_path / "payloads").mkdir(mode=0o700)
    config: dict[str, Any] = {
        "sources": {
            "src": {
                "plugin": kind.plugin,
                "on_success": "out",
                "options": {"path": str(data), **kind.options, "on_validation_failure": "bad"},
            }
        },
        "sinks": {
            name: {
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {"path": str(tmp_path / name / f"{name}.jsonl"), "format": "jsonl", "schema": {"mode": "observed"}},
            }
            for name in ("out", "bad")
        },
        "landscape": {"url": db_url, "backend": "postgresql" if db_url.startswith("postgresql") else "sqlite"},
        "payload_store": {"backend": "filesystem", "base_path": str(tmp_path / "payloads")},
    }
    settings = tmp_path / "settings.yaml"
    settings.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return settings


def _quarantine_stage(db: LandscapeDB) -> str:
    """Where the quarantined row's handoff is: "not_ingested", "parked" (in no effect) or "in_effect"."""
    quarantine_tokens = select(token_work_items_table.c.token_id).where(
        token_work_items_table.c.pending_path == TerminalPath.QUARANTINED_AT_SOURCE.value
    )
    with db.connection() as conn:
        if conn.execute(quarantine_tokens).first() is None:
            return "not_ingested"
        in_effect = conn.execute(
            select(sink_effect_members_table.c.effect_id).where(sink_effect_members_table.c.token_id.in_(quarantine_tokens))
        ).first()
    return "parked" if in_effect is None else "in_effect"


class _WindowFault:
    """The coordinator's ``_fault`` hook for one window; reads the store (opened at the first seam hit, once the run has created it)."""

    def __init__(self, window: Window, db_url: str, *, die: bool) -> None:
        self._window = window
        self._db_url = db_url
        self._die = die
        self._db: LandscapeDB | None = None

    def hook(self) -> Any:
        def fault(coordinator: SinkEffectCoordinator, seam: SinkEffectExecutionSeam) -> None:
            if seam is not self._window.seam:
                return
            if self._db is None:
                self._db = LandscapeDB.from_url(self._db_url, create_tables=False)
            if _quarantine_stage(self._db) == self._window.stage:
                if self._die:
                    os._exit(137)
                raise SinkEffectInjectedFault(seam)

        return fault

    def close(self) -> None:
        if self._db is not None:
            self._db.close()


def _die_at_window_child(settings: str, db_url: str, seam: str, stage: str, image: str) -> None:
    """Spawned child: a real run whose process dies (no ceremony) at the window."""
    fault = _WindowFault(Window(SinkEffectExecutionSeam(seam), stage, image), db_url, die=True)
    pytest.MonkeyPatch().setattr(SinkEffectCoordinator, "_fault", fault.hook())
    app(["run", "-s", settings, "--execute"], standalone_mode=False)
    os._exit(0)


def crash_run(settings: Path, db_url: str, window: Window, *, process_death: bool, monkeypatch: Any) -> None:
    if process_death:
        child = multiprocessing.get_context("spawn").Process(
            target=_die_at_window_child, args=(str(settings), db_url, window.seam.value, window.stage, window.image)
        )
        child.start()
        child.join(timeout=300)
        assert child.exitcode == 137, f"the run did not die at {window}: exit {child.exitcode}"
        return
    fault = _WindowFault(window, db_url, die=False)
    try:
        with monkeypatch.context() as patched:
            patched.setattr(SinkEffectCoordinator, "_fault", fault.hook())
            result = CliRunner().invoke(app, ["run", "-s", str(settings), "--execute"])
    finally:
        fault.close()
    assert result.exit_code != 0, result.output
    assert f"injected sink-effect fault at {window.seam.value}" in result.output, result.output


def run_id_of(db: LandscapeDB) -> str:
    with db.connection() as conn:
        return str(conn.execute(select(runs_table.c.run_id)).scalar_one())


@dataclass(frozen=True, slots=True)
class CrashImage:
    run_id: str
    quarantine_token: str
    ingest_error_hash: str
    quarantine_effect_id: str | None
    quarantine_payload_hashes: tuple[str, ...]


def _quarantine_items(db: LandscapeDB, run_id: str) -> list[Any]:
    with db.connection() as conn:
        return list(
            conn.execute(
                select(token_work_items_table)
                .where(token_work_items_table.c.run_id == run_id)
                .where(token_work_items_table.c.pending_path == TerminalPath.QUARANTINED_AT_SOURCE.value)
            ).all()
        )


def _effects_with_member(db: LandscapeDB, run_id: str, token_id: str) -> list[Any]:
    with db.connection() as conn:
        effect_ids = (
            select(sink_effect_members_table.c.effect_id)
            .where(sink_effect_members_table.c.run_id == run_id)
            .where(sink_effect_members_table.c.token_id == token_id)
        )
        return list(conn.execute(select(sink_effects_table).where(sink_effects_table.c.effect_id.in_(effect_ids))).all())


def _member_payload_hashes(db: LandscapeDB, effect_id: str) -> tuple[str, ...]:
    with db.connection() as conn:
        return tuple(
            str(h)
            for h in conn.execute(
                select(sink_effect_members_table.c.payload_hash)
                .where(sink_effect_members_table.c.effect_id == effect_id)
                .order_by(sink_effect_members_table.c.ordinal)
            ).scalars()
        )


def _lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def assert_crash_image(tmp_path: Path, db: LandscapeDB, window: Window) -> CrashImage:
    """The crash left the window's image, on the QUARANTINE sink: a harness check before resume."""
    run_id = run_id_of(db)
    items = _quarantine_items(db, run_id)
    assert len(items) == 1, f"exactly one parked quarantine handoff expected, got {len(items)}"
    item = items[0]
    assert item.node_id is None
    assert item.pending_sink_name == "bad"
    assert item.pending_error_hash, "the ingest recorded no authoritative error hash"
    assert item.status == TokenWorkStatus.PENDING_SINK.value, item.status
    effects = _effects_with_member(db, run_id, str(item.token_id))
    bad_rows = _lines(tmp_path / "bad" / "bad.jsonl")
    if window.image == "none":
        with db.connection() as conn:
            assert conn.execute(select(sink_effects_table.c.effect_id).where(sink_effects_table.c.run_id == run_id)).first() is None
        assert bad_rows == []
        return CrashImage(run_id, str(item.token_id), str(item.pending_error_hash), None, ())
    assert len(effects) == 1, f"the quarantine sink's effect was not the one the window hit: {effects}"
    effect = effects[0]
    # At BEFORE_EFFECT the effect is already claimed in flight; the sink file
    # (asserted below) is what tells unpublished from published.
    expected_states = {"unpublished": {"in_flight"}, "published": {"in_flight"}, "finalized": {"finalized"}}[window.image]
    assert effect.state in expected_states, (window, effect.state)
    assert len(bad_rows) == (0 if window.image == "unpublished" else 1), bad_rows
    return CrashImage(
        run_id,
        str(item.token_id),
        str(item.pending_error_hash),
        str(effect.effect_id),
        _member_payload_hashes(db, str(effect.effect_id)),
    )


def _lapse_crashed_leases(db: LandscapeDB, run_id: str, *, seat: bool) -> None:
    """Write the crashed run's still-held windows into the database's past.

    A crashed run keeps its unfinalized sink effect's lease (5 min default)
    whether it raised or died, and a killed leader also keeps its seat
    (ADR-030 §B.4); resume waits both out. This produces the post-window image
    deterministically instead of sleeping (ADR-047 database clock); no token,
    outcome or work item is touched.
    """
    if seat:
        expire_leader_seat(db, run_id)
    with db.connection() as conn:
        leased = conn.execute(
            select(sink_effects_table.c.effect_id)
            .where(sink_effects_table.c.run_id == run_id)
            .where(sink_effects_table.c.state != "finalized")
            .where(sink_effects_table.c.lease_owner.is_not(None))
        ).scalars()
        effect_ids = [str(effect_id) for effect_id in leased]
    for effect_id in effect_ids:
        expire_sink_effect_lease(db.engine, effect_id)


def resume(settings: Path, run_id: str) -> Any:
    return CliRunner().invoke(app, ["resume", run_id, "-s", str(settings), "--execute"])


def assert_resumed_correctly(tmp_path: Path, db: LandscapeDB, kind: QuarantineKind, image: CrashImage, result: Any) -> None:
    """DESIGN-QR §5 assertions: one outcome per token, the original error hash, one publication, nothing left open."""
    assert "Traceback" not in result.output, result.output
    run_id = image.run_id
    with db.connection() as conn:
        status = conn.execute(select(runs_table.c.status).where(runs_table.c.run_id == run_id)).scalar_one()
        token_ids = set(conn.execute(select(tokens_table.c.token_id).where(tokens_table.c.run_id == run_id)).scalars())
        outcomes = conn.execute(
            select(token_outcomes_table).where(token_outcomes_table.c.run_id == run_id).where(token_outcomes_table.c.completed == 1)
        ).all()
        open_items = conn.execute(
            select(token_work_items_table.c.status)
            .where(token_work_items_table.c.run_id == run_id)
            .where(token_work_items_table.c.status != TokenWorkStatus.TERMINAL.value)
        ).all()
        open_effects = conn.execute(
            select(sink_effects_table.c.effect_id, sink_effects_table.c.state)
            .where(sink_effects_table.c.run_id == run_id)
            .where(sink_effects_table.c.state != "finalized")
        ).all()
    assert status == RunStatus.COMPLETED_WITH_FAILURES.value, (status, result.output)
    assert sorted(str(o.token_id) for o in outcomes) == sorted(token_ids), "a token lacks, or repeats, a completed outcome"
    by_token = {str(o.token_id): o for o in outcomes}
    quarantine = by_token.pop(image.quarantine_token)
    assert (quarantine.outcome, quarantine.path, quarantine.sink_name) == (
        TerminalOutcome.FAILURE.value,
        TerminalPath.QUARANTINED_AT_SOURCE.value,
        "bad",
    )
    assert quarantine.error_hash == image.ingest_error_hash, "the quarantine outcome did not carry the ingest-time error hash"
    assert len(by_token) == 3, "every kind surrounds its rejected row with three valid rows"
    assert {(o.outcome, o.path, o.sink_name) for o in by_token.values()} == {
        (TerminalOutcome.SUCCESS.value, TerminalPath.DEFAULT_FLOW.value, "out")
    }
    assert open_items == [], open_items
    assert open_effects == [], open_effects
    bad_rows = _lines(tmp_path / "bad" / "bad.jsonl")
    out_rows = _lines(tmp_path / "out" / "out.jsonl")
    assert len(bad_rows) == 1, f"the quarantined row was published {len(bad_rows)} times"
    assert len(out_rows) == len(by_token), out_rows
    if kind.good_ids:
        assert sorted(row["id"] for row in out_rows) == sorted(kind.good_ids), "a rejected row reached the success sink"
    if image.quarantine_effect_id is not None:
        effects = _effects_with_member(db, run_id, image.quarantine_token)
        assert [str(e.effect_id) for e in effects] == [image.quarantine_effect_id], "the quarantine effect was re-reserved"
        assert _member_payload_hashes(db, image.quarantine_effect_id) == image.quarantine_payload_hashes


def scenario_crash_then_resume(
    tmp_path: Path, monkeypatch: Any, *, kind_name: str, window_name: str, process_death: bool, db_url: str
) -> None:
    kind = KINDS[kind_name]
    window = WINDOWS[window_name]
    settings = write_settings(tmp_path, kind, db_url)
    crash_run(settings, db_url, window, process_death=process_death, monkeypatch=monkeypatch)
    db = LandscapeDB.from_url(db_url, create_tables=False)
    try:
        image = assert_crash_image(tmp_path, db, window)
        _lapse_crashed_leases(db, image.run_id, seat=process_death)
        result = resume(settings, image.run_id)
        assert_resumed_correctly(tmp_path, db, kind, image, result)
    finally:
        db.close()


def _source_dying_after_quarantine(original: Any, *, die: bool) -> Any:
    """Wrap a source's ``load``: yield the first three rows (the third is the rejected one), then die mid-read."""

    def load(self: Any, ctx: Any) -> Any:
        for index, item in enumerate(original(self, ctx)):
            yield item
            if index == 2:
                if die:
                    os._exit(137)
                raise RuntimeError("source died mid-read")

    return load


def _die_mid_ingest_child(settings: str) -> None:
    """Spawned child: a real run whose process dies inside the source, after the quarantined row was ingested."""
    from elspeth.plugins.sources.json_source import JSONSource

    pytest.MonkeyPatch().setattr(JSONSource, "load", _source_dying_after_quarantine(JSONSource.load, die=True))
    app(["run", "-s", settings, "--execute"], standalone_mode=False)
    os._exit(0)


def scenario_mid_ingest_death_is_refused_then_abandoned(
    tmp_path: Path, monkeypatch: Any, *, kind_name: str, process_death: bool, db_url: str
) -> None:
    """W0: the source dies after the quarantined row's fenced ingest; resume refuses, abandon settles every token.

    The quarantined row's ingest is one transaction, so its token carries the
    FAILED source state and the PENDING_SINK handoff even though the run never
    reached a flush; the ADR-038 abandon records it ABANDONED like every other
    token of the leaderless run.
    """
    from elspeth.plugins.sources.json_source import JSONSource

    kind = KINDS[kind_name]
    assert kind.plugin == "json"
    settings = write_settings(tmp_path, kind, db_url)
    if process_death:
        child = multiprocessing.get_context("spawn").Process(target=_die_mid_ingest_child, args=(str(settings),))
        child.start()
        child.join(timeout=300)
        assert child.exitcode == 137, f"the run did not die mid-ingest: exit {child.exitcode}"
    else:
        with monkeypatch.context() as patched:
            patched.setattr(JSONSource, "load", _source_dying_after_quarantine(JSONSource.load, die=False))
            crashed = CliRunner().invoke(app, ["run", "-s", str(settings), "--execute"])
        assert crashed.exit_code != 0, crashed.output
    db = LandscapeDB.from_url(db_url, create_tables=False)
    try:
        run_id = run_id_of(db)
        items = _quarantine_items(db, run_id)
        assert len(items) == 1 and items[0].status == TokenWorkStatus.PENDING_SINK.value, "the ingest did not park the handoff"
        quarantine_token = str(items[0].token_id)
        if process_death:
            expire_leader_seat(db, run_id)
        refused = resume(settings, run_id)
        assert refused.exit_code != 0, refused.output
        assert "source lifecycle is incomplete" in refused.output, refused.output
        abandoned = CliRunner().invoke(app, ["abandon", run_id, "-s", str(settings), "--execute"])
        with db.connection() as conn:
            token_ids = set(conn.execute(select(tokens_table.c.token_id).where(tokens_table.c.run_id == run_id)).scalars())
            outcomes = conn.execute(select(token_outcomes_table).where(token_outcomes_table.c.run_id == run_id)).all()
            status = conn.execute(select(runs_table.c.status).where(runs_table.c.run_id == run_id)).scalar_one()
        if process_death:
            # A killed leader ran no ceremony: abandon settles the leaderless run.
            assert abandoned.exit_code == 0, abandoned.output
            assert status == RunStatus.INTERRUPTED.value
        else:
            # The raised failure's ceremony already settled every token and stamped FAILED.
            assert abandoned.exit_code != 0 and "already terminal" in abandoned.output, abandoned.output
            assert status == RunStatus.FAILED.value
        # Rows 1-2 and the rejected row were read before the source died; row 3 never was.
        assert len(token_ids) == 3 and quarantine_token in token_ids
        assert sorted(str(o.token_id) for o in outcomes) == sorted(token_ids), "a token lacks, or repeats, an outcome record"
        assert {(o.outcome, o.path, o.completed) for o in outcomes} == {(None, TerminalPath.ABANDONED.value, 0)}
    finally:
        db.close()


def scenario_quarantine_storm_resumes_every_parked_handoff(tmp_path: Path, monkeypatch: Any, *, rows: int, db_url: str) -> None:
    """Systems C2: every row rejected, the run dies before its first reservation, resume publishes each once.

    Every quarantined row is a parked PENDING_SINK handoff, so the resume's
    pending-sink recovery drain takes one iteration per row plus the final
    empty claim: the ``MAX_WORK_QUEUE_ITERATIONS`` cap binds at that many
    parked handoffs (measured in the QR evidence).
    """
    data = "".join(json.dumps({"id": f"x{i}", "line": "z"}) + "\n" for i in range(rows))
    kind = QuarantineKind("d.jsonl", data, "json", {"format": "jsonl", "schema": _FIXED}, good_ids=())
    settings = write_settings(tmp_path, kind, db_url)
    crash_run(settings, db_url, WINDOWS["W1"], process_death=False, monkeypatch=monkeypatch)
    db = LandscapeDB.from_url(db_url, create_tables=False)
    try:
        run_id = run_id_of(db)
        with db.connection() as conn:
            parked = conn.execute(
                select(token_work_items_table.c.status)
                .where(token_work_items_table.c.run_id == run_id)
                .where(token_work_items_table.c.pending_path == TerminalPath.QUARANTINED_AT_SOURCE.value)
            ).scalars()
            assert list(parked) == [TokenWorkStatus.PENDING_SINK.value] * rows
        result = resume(settings, run_id)
        assert "Traceback" not in result.output, result.output
        with db.connection() as conn:
            status = conn.execute(select(runs_table.c.status).where(runs_table.c.run_id == run_id)).scalar_one()
            outcomes = conn.execute(
                select(token_outcomes_table.c.token_id, token_outcomes_table.c.path)
                .where(token_outcomes_table.c.run_id == run_id)
                .where(token_outcomes_table.c.completed == 1)
            ).all()
            open_items = conn.execute(
                select(token_work_items_table.c.work_item_id)
                .where(token_work_items_table.c.run_id == run_id)
                .where(token_work_items_table.c.status != TokenWorkStatus.TERMINAL.value)
            ).all()
        assert status == RunStatus.COMPLETED_WITH_FAILURES.value, (status, result.output)
        assert len({row.token_id for row in outcomes}) == len(outcomes) == rows
        assert {row.path for row in outcomes} == {TerminalPath.QUARANTINED_AT_SOURCE.value}
        assert open_items == []
        assert len(_lines(tmp_path / "bad" / "bad.jsonl")) == rows
        assert _lines(tmp_path / "out" / "out.jsonl") == []
    finally:
        db.close()
