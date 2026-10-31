"""Public attachment production, persistence and private reuse on PostgreSQL.

Metadata freshness/isolation checks do not attest to attachment-byte integrity.
Public grammar/512-width controls remain in the received unit cohort.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import select, update
from tests.fixtures.identities import ensure_test_identity
from tests.testcontainer.web.test_reviewed_plural_authority_postgres import authority_harness as _authority_harness
from tests.testcontainer.web.test_reviewed_plural_authority_postgres import operation_postgres_target as _operation_postgres_target
from tests.testcontainer.web.test_reviewed_plural_authority_postgres import operation_store as _operation_store
from tests.unit.web.composer import test_attachment_authoring_projection as projection
from tests.unit.web.composer import test_reviewed_plural_authority as custody
from tests.unit.web.composer import test_set_pipeline_attachment_roots as public

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.state import CompositionState
from elspeth.web.sessions.models import blobs_table, composition_states_table

pytestmark = pytest.mark.testcontainer
# These are the received serial target and fresh per-case database fixtures.
# Their original owner alone disposes/drops that UUID-named database.
operation_postgres_target = _operation_postgres_target
operation_store = _operation_store
authority_harness = _authority_harness


@dataclass(frozen=True)
class _PublicCase:
    seed: custody._PersistedPlural
    arguments: dict[str, Any]
    candidate: CompositionState
    source_name: str


def _state_rows(harness: custody._AuthorityHarness) -> list[dict[str, Any]]:
    with harness.engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                select(composition_states_table)
                .where(composition_states_table.c.session_id == str(harness.session_id))
                .order_by(composition_states_table.c.version)
            ).mappings()
        ]


def _private_accepts(harness: custody._AuthorityHarness, seed: custody._PersistedPlural) -> None:
    authority = custody._resolve(harness, seed.state)
    reused = custody._private_reuse(harness, seed.state, authority)
    assert reused.success and reused.validation.is_valid, reused.to_dict()
    assert reused.updated_state.version == seed.state.version + 1
    assert reused.updated_state.sources == seed.state.sources
    assert (reused.updated_state.nodes, reused.updated_state.edges, reused.updated_state.outputs, reused.updated_state.metadata) == (
        seed.state.nodes,
        seed.state.edges,
        seed.state.outputs,
        seed.state.metadata,
    )
    custody._assert_checkpoint_unchanged(harness, seed)


def _save_public_candidate(
    harness: custody._AuthorityHarness, candidate: CompositionState, *, previous: custody._PersistedPlural
) -> custody._PersistedPlural:
    before = _state_rows(harness)
    state, record = custody._persist(harness, candidate)
    after = _state_rows(harness)
    assert len(after) == len(before) + 1 and after[:-1] == before
    assert record.id != previous.record.id and record.version == previous.record.version + 1
    assert [row["version"] for row in after] == list(range(1, record.version + 1))
    saved = custody._PersistedPlural(state, record, previous.blob_ids)
    _private_accepts(harness, saved)
    return saved


def _seed_public(harness: custody._AuthorityHarness, *, form: str = "named") -> _PublicCase:
    assert harness.engine.dialect.name == "postgresql" and _state_rows(harness) == []
    first, second = public._images(harness)
    source_name = "source" if form == "default" else "scans"
    roots = {source_name: public._root("blob_rows", blob_ids=[first, second])}
    csv = None
    if form == "named":
        csv = public._csv_blob(harness)
        roots = {"table": public._root("text", blob_id=csv), **roots}
    else:
        assert form == "default"
    arguments = public._arguments(roots, default=form == "default")
    original_arguments = deepcopy(arguments)
    draft = custody._draft()
    original_draft = draft.to_dict()

    # The strict public dispatcher is the producer. No reviewed authority or
    # private source options are supplied to either public call.
    produced = public._public_call(harness, "set_pipeline", arguments, draft)
    assert produced.success and produced.validation.is_valid, produced.to_dict()
    assert produced.updated_state.version == draft.version + 1
    assert deep_thaw(produced.updated_state.sources[source_name].options["blobs"]) == public._expected_entries(harness, [first, second])
    if form == "named":
        assert produced.updated_state.sources["table"].plugin == "text"
        assert produced.updated_state.sources["table"].options["column"] == "provider_text"
    assert arguments == original_arguments and draft.to_dict() == original_draft and _state_rows(harness) == []
    state, record = custody._persist(harness, produced.updated_state)
    assert record.version == 1 and len(_state_rows(harness)) == 1
    seed = custody._PersistedPlural(state, record, (first, second))
    _private_accepts(harness, seed)

    # A genuine successful replacement precedes every later opposition. Its
    # earlier named root changes plugin, so a later-member refusal must not
    # leave that otherwise-valid first replacement partially applied.
    replacement_roots = {source_name: public._root("blob_rows", blob_ids=[second, first])}
    if form == "named":
        table = public._root("csv", blob_id=csv)
        table["options"].update(delimiter=",", encoding="utf-8")
        replacement_roots = {"table": table, **replacement_roots}
    replacement = public._arguments(replacement_roots, default=form == "default")
    replacement["metadata"]["name"] = "PostgreSQL public full replacement"
    before = deepcopy(replacement)
    result = public._public_call(harness, "set_pipeline", replacement, seed.state)
    assert result.success and result.validation.is_valid, result.to_dict()
    assert result.updated_state.version == seed.state.version + 1
    assert deep_thaw(result.updated_state.sources[source_name].options["blobs"]) == public._expected_entries(harness, [second, first])
    if form == "named":
        assert result.updated_state.sources["table"].plugin == "csv" and seed.state.sources["table"].plugin == "text"
    assert replacement == before
    custody._assert_checkpoint_unchanged(harness, seed)
    return _PublicCase(seed, replacement, result.updated_state, source_name)


def _assert_unchanged(harness: custody._AuthorityHarness, case: _PublicCase, rows: list[dict[str, Any]]) -> None:
    custody._assert_checkpoint_unchanged(harness, case.seed)
    assert _state_rows(harness) == rows


def _healthy_followup(harness: custody._AuthorityHarness, case: _PublicCase) -> None:
    before = deepcopy(case.arguments)
    result = public._public_call(harness, "set_pipeline", case.arguments, case.seed.state)
    assert result.success and result.validation.is_valid, result.to_dict()
    assert result.updated_state == case.candidate and result.updated_state.version == case.seed.state.version + 1
    assert case.arguments == before
    _private_accepts(harness, case.seed)


def _last_public_member(case: _PublicCase) -> str:
    root = case.arguments["source"] if case.source_name == "source" else case.arguments["sources"][case.source_name]
    return root["blob_ids"][-1]


@pytest.mark.parametrize("form", ("default", "named"))
def test_public_ordered_roots_persist_once_and_authorize_private_reuse_postgres(
    authority_harness: custody._AuthorityHarness, form: str
) -> None:
    harness = authority_harness
    case = _seed_public(harness, form=form)

    saved = _save_public_candidate(harness, case.candidate, previous=case.seed)

    assert saved.record.version == 2
    assert deep_thaw(saved.state.sources[case.source_name].options["blobs"]) == public._expected_entries(
        harness, list(reversed(case.seed.blob_ids))
    )
    wire = projection._projection(harness, saved.state)
    projection._assert_exact_projection(saved.state, wire)
    projection._strict_round_trip(wire)
    again = public._public_call(harness, "set_pipeline", wire, saved.state)
    assert again.success and again.validation.is_valid, again.to_dict()
    assert again.updated_state.sources == saved.state.sources and again.updated_state.version == saved.state.version + 1
    custody._assert_checkpoint_unchanged(harness, saved)
    assert len(_state_rows(harness)) == 2


@pytest.mark.parametrize("perturbation", ("not_ready_pending", "foreign_session_same_user", "llm_generated"))
def test_current_record_refusal_does_not_partially_replace_public_candidate_postgres(
    authority_harness: custody._AuthorityHarness, perturbation: str
) -> None:
    harness = authority_harness
    case = _seed_public(harness)
    rows = _state_rows(harness)
    arguments = deepcopy(case.arguments)
    later = _last_public_member(case)
    original = custody._blob_row(harness, later)
    assert original is not None
    try:
        custody._mutate_later_record(harness, later, perturbation)

        refused = public._public_call(harness, "set_pipeline", case.arguments, case.seed.state)

        public._assert_candidate_refusal(
            refused,
            case.seed.state,
            "source_blob_review_required" if perturbation == "llm_generated" else "source_blob_unavailable",
            case.source_name,
        )
        assert refused.updated_state.sources["table"].plugin == "text"
        assert case.arguments == arguments
        with pytest.raises(AuditIntegrityError, match="reviewed plural"):
            custody._resolve(harness, case.seed.state)
        _assert_unchanged(harness, case, rows)
    finally:
        projection._restore_blob(harness, later, original)
    _healthy_followup(harness, case)
    _assert_unchanged(harness, case, rows)


@pytest.mark.parametrize("perturbation", ("filename", "payload_ref"))
def test_public_reapply_refreshes_current_metadata_before_private_reuse_postgres(
    authority_harness: custody._AuthorityHarness, perturbation: str
) -> None:
    harness = authority_harness
    case = _seed_public(harness)
    rows = _state_rows(harness)
    arguments = deepcopy(case.arguments)
    later = _last_public_member(case)
    original = custody._blob_row(harness, later)
    assert original is not None
    saved = None
    try:
        custody._mutate_later_record(harness, later, perturbation)
        with pytest.raises(AuditIntegrityError, match="reviewed plural"):
            custody._resolve(harness, case.seed.state)

        fresh = public._public_call(harness, "set_pipeline", case.arguments, case.seed.state)

        assert fresh.success and fresh.validation.is_valid, fresh.to_dict()
        assert fresh.updated_state.version == case.seed.state.version + 1
        entries = deep_thaw(fresh.updated_state.sources[case.source_name].options["blobs"])
        assert entries == public._expected_entries(harness, list(reversed(case.seed.blob_ids)))
        historical = {entry["blob_id"]: entry for entry in case.seed.state.sources[case.source_name].options["blobs"]}
        changed = next(entry for entry in entries if entry["blob_id"] == later)
        assert {key for key in changed if changed[key] != historical[later][key]} == {perturbation}
        assert case.arguments == arguments
        _assert_unchanged(harness, case, rows)
        saved = _save_public_candidate(harness, fresh.updated_state, previous=case.seed)
    finally:
        projection._restore_blob(harness, later, original)
    assert saved is not None
    # Restoring current metadata does not rewrite either saved historical row.
    # Re-enter the public producer before granting current private authority.
    restored = public._public_call(harness, "set_pipeline", case.arguments, saved.state)
    assert restored.success and restored.validation.is_valid, restored.to_dict()
    assert restored.updated_state.version == saved.state.version + 1
    final = _save_public_candidate(harness, restored.updated_state, previous=saved)
    assert final.record.version == 3 and len(_state_rows(harness)) == 3


def test_publicly_persisted_roots_do_not_grant_another_user_private_authority_postgres(
    authority_harness: custody._AuthorityHarness,
) -> None:
    harness = authority_harness
    case = _seed_public(harness)
    rows = _state_rows(harness)

    with harness.engine.begin() as conn:
        ensure_test_identity(conn, identity_id="mallory")
    with pytest.raises(AuditIntegrityError, match="current user session"):
        custody._resolve(harness, case.seed.state, user_id="mallory")

    _assert_unchanged(harness, case, rows)
    _healthy_followup(harness, case)


def test_public_and_private_readers_observe_committed_ready_state_postgres(authority_harness: custody._AuthorityHarness) -> None:
    harness = authority_harness
    case = _seed_public(harness)
    rows = _state_rows(harness)
    later = _last_public_member(case)
    original = custody._blob_row(harness, later)
    assert original is not None and original["status"] == "ready"
    try:
        with harness.engine.connect() as writer, writer.begin():
            assert writer.get_isolation_level() == "READ COMMITTED"
            assert writer.execute(update(blobs_table).where(blobs_table.c.id == later).values(status="pending")).rowcount == 1
            assert writer.execute(select(blobs_table.c.status).where(blobs_table.c.id == later)).scalar_one() == "pending"
            # These genuine public/private calls use other actual connections.
            # PostgreSQL must expose the preceding committed READY row, not
            # this writer's uncommitted replacement.
            visible = custody._blob_row(harness, later)
            assert visible is not None and visible["status"] == "ready"
            _healthy_followup(harness, case)
            _assert_unchanged(harness, case, rows)

        committed = custody._blob_row(harness, later)
        assert committed is not None and committed["status"] == "pending"
        refused = public._public_call(harness, "set_pipeline", case.arguments, case.seed.state)
        public._assert_candidate_refusal(refused, case.seed.state, "source_blob_unavailable", case.source_name)
        with pytest.raises(AuditIntegrityError, match="reviewed plural"):
            custody._resolve(harness, case.seed.state)
        _assert_unchanged(harness, case, rows)
    finally:
        projection._restore_blob(harness, later, original)
    _healthy_followup(harness, case)
    _assert_unchanged(harness, case, rows)
