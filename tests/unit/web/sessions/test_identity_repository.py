"""The identity substrate's login path against a real engine.

``ensure_identity`` / ``read_identity`` / ``retire_identity`` are methods of
``RepositoryIdentityAuthority`` (P4-D6): the engine-taking module functions
they replaced are deleted, and ``identity_repository`` keeps only the record
types and row parsers. These tests pin the login-path CONTRACT those methods
carried over unchanged -- first sight, repeat logins, the administrator's
decision outranking the caller's default, the admission audit inside the
transaction, the loser path of the first-login race, and retirement -- so a
regression in the authority is caught here by name, not only by the
authority's own adversarial suite.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from elspeth.web.auth.models import IdentityClaims
from elspeth.web.coordination.identity_authority import IdentityRetired, RepositoryIdentityAuthority
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.identity_repository import IdentityRowCorruptionError
from elspeth.web.sessions.models import identities_table, quota_policies_table
from elspeth.web.sessions.schema import initialize_session_schema

_TOKENS = 50_000
_STORAGE = 1_073_741_824


@pytest.fixture
def engine():
    eng = create_session_engine("sqlite:///:memory:")
    initialize_session_schema(eng)
    return eng


@pytest.fixture
def authority(engine) -> RepositoryIdentityAuthority:
    return RepositoryIdentityAuthority(engine)


def _noop(_identity_id: str, _username: str, _quota_written: bool) -> None:
    return None


def _record_no_retirement(_outcome: IdentityRetired) -> None:
    return None


def _claims(**overrides) -> IdentityClaims:
    fields = {
        "provider": "local",
        "subject": "ada",
        "username": "ada",
        "display_name": "Ada Lovelace",
        "email": "ada@example.com",
        "organisation_id": None,
    }
    fields.update(overrides)
    return IdentityClaims(**fields)


def _ensure(authority: RepositoryIdentityAuthority, *, activate: bool, **claim_overrides):
    return authority.ensure_identity(
        claims=_claims(**claim_overrides),
        activate=activate,
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=_STORAGE,
        record_admission=_noop,
    )


def _retire(authority: RepositoryIdentityAuthority, subject: str = "ada", *, reason: str = "local credential deleted"):
    return authority.retire_identity(provider="local", subject=subject, reason=reason, record=_record_no_retirement)


# --------------------------------------------------------------------------
# First sight.
# --------------------------------------------------------------------------


def test_a_first_login_creates_one_identity(authority) -> None:
    outcome = _ensure(authority, activate=False)

    assert outcome.created is True
    assert outcome.record.subject == "ada"
    assert outcome.record.provider == "local"


def test_a_first_login_lands_pending_by_default(authority) -> None:
    """D12: no access until an administrator approves."""
    outcome = _ensure(authority, activate=False)

    assert outcome.record.access_state == "pending"
    assert outcome.record.is_active is False
    assert outcome.activated_now is False


def test_an_activating_deployment_admits_on_first_sight(authority) -> None:
    outcome = _ensure(authority, activate=True)

    assert outcome.record.access_state == "active"
    assert outcome.record.is_active is True
    assert outcome.activated_now is True


def test_a_pending_identity_gets_no_quota_row(engine, authority) -> None:
    """Quota is granted at admission; a pending identity has not been admitted."""
    outcome = _ensure(authority, activate=False)

    with engine.connect() as conn:
        rows = conn.execute(
            select(quota_policies_table.c.policy_id).where(quota_policies_table.c.identity_id == outcome.record.identity_id)
        ).all()
    assert rows == []


def test_activation_writes_the_default_quota_row(engine, authority) -> None:
    """D31: activation without this leaves the first run to refuse on nothing."""
    outcome = _ensure(authority, activate=True)

    with engine.connect() as conn:
        row = conn.execute(
            select(
                quota_policies_table.c.tokens_per_day,
                quota_policies_table.c.storage_bytes,
                quota_policies_table.c.set_by_actor,
                quota_policies_table.c.set_by_identity_id,
                quota_policies_table.c.revoked_at,
            ).where(quota_policies_table.c.identity_id == outcome.record.identity_id)
        ).one()

    assert row.tokens_per_day == _TOKENS
    assert row.storage_bytes == _STORAGE
    assert row.set_by_actor == "operator"
    # No administrator acted, so none is named. Inventing one would put a
    # fabricated actor in the table R5 counts.
    assert row.set_by_identity_id is None
    assert row.revoked_at is None


def test_first_seen_email_is_recorded_for_rebound_detection(engine, authority) -> None:
    """D10 needs the email as it was the first time this subject appeared."""
    outcome = _ensure(authority, activate=False, email="ada@example.com")

    with engine.connect() as conn:
        stored = conn.execute(
            select(identities_table.c.subject_email_at_first_seen).where(identities_table.c.identity_id == outcome.record.identity_id)
        ).scalar_one()
    assert stored == "ada@example.com"


def test_no_profile_claims_are_stored_at_first_sight(engine, authority) -> None:
    """A container must not accumulate the claims of everyone who merely tried."""
    outcome = _ensure(authority, activate=True)

    with engine.connect() as conn:
        raw = conn.execute(
            select(identities_table.c.raw_claims_json).where(identities_table.c.identity_id == outcome.record.identity_id)
        ).scalar_one()
    assert raw is None


# --------------------------------------------------------------------------
# Repeat logins.
# --------------------------------------------------------------------------


def test_a_second_login_finds_the_same_identity(authority) -> None:
    """``sub`` must be stable, or one person owns rows under two ids."""
    first = _ensure(authority, activate=True)
    second = _ensure(authority, activate=True)

    assert second.created is False
    assert second.record.identity_id == first.record.identity_id


def test_a_returning_active_user_does_not_re_activate(authority) -> None:
    """Otherwise every visit manufactures an ``identity_activated`` event."""
    _ensure(authority, activate=True)
    second = _ensure(authority, activate=True)

    assert second.activated_now is False


def test_a_returning_user_does_not_get_a_second_quota_row(engine, authority) -> None:
    """The active-per-identity partial unique would refuse it; do not try."""
    outcome = _ensure(authority, activate=True)
    _ensure(authority, activate=True)

    with engine.connect() as conn:
        count = len(
            conn.execute(
                select(quota_policies_table.c.policy_id).where(quota_policies_table.c.identity_id == outcome.record.identity_id)
            ).all()
        )
    assert count == 1


def test_a_login_refreshes_the_display_username(authority) -> None:
    """The admin queue must show what the IdP says today, not at first sight."""
    first = _ensure(authority, activate=True, username="ada")
    second = _ensure(authority, activate=True, username="Ada L.")

    assert second.record.identity_id == first.record.identity_id
    assert second.record.username == "Ada L."


def test_a_login_stamps_last_login_at(engine, authority) -> None:
    """R9's dormancy window measures from this column."""
    outcome = _ensure(authority, activate=False)

    with engine.connect() as conn:
        stamped = conn.execute(
            select(identities_table.c.last_login_at).where(identities_table.c.identity_id == outcome.record.identity_id)
        ).scalar_one()
    assert stamped is not None


# --------------------------------------------------------------------------
# The decision an administrator already made outranks the caller's default.
# --------------------------------------------------------------------------


def test_a_pending_identity_is_not_activated_by_a_later_login(authority) -> None:
    """Waiting at the D12 wall must not be escapable by logging in again."""
    first = _ensure(authority, activate=False)
    second = _ensure(authority, activate=True)

    assert second.record.access_state == "pending"
    assert second.activated_now is False
    # Re-read the STORED row. The returned record is computed before the
    # UPDATE runs, so asserting on it alone leaves an upgrading UPDATE
    # (.values(access_state=...)) entirely invisible.
    assert authority.read_identity(identity_id=first.record.identity_id).access_state == "pending"


def test_an_active_identity_is_not_downgraded_by_a_closed_deployment(authority) -> None:
    """A pre-provisioned cohort keeps its admission when registration closes."""
    first = _ensure(authority, activate=True)
    second = _ensure(authority, activate=False)

    assert second.record.access_state == "active"
    assert authority.read_identity(identity_id=first.record.identity_id).access_state == "active"


def test_a_disabled_identity_stays_disabled_through_a_login(engine, authority) -> None:
    """The disable must survive; re-authenticating is not an appeal."""
    outcome = _ensure(authority, activate=True)
    with engine.begin() as conn:
        conn.execute(
            identities_table.update().where(identities_table.c.identity_id == outcome.record.identity_id).values(access_state="disabled")
        )

    again = _ensure(authority, activate=True)
    assert again.record.access_state == "disabled"
    assert again.record.is_active is False
    assert authority.read_identity(identity_id=outcome.record.identity_id).access_state == "disabled"


# --------------------------------------------------------------------------
# Provider separation.
# --------------------------------------------------------------------------


def test_the_same_subject_under_two_providers_is_two_people(authority) -> None:
    """Subjects are per-IdP namespaces; collapsing them merges strangers."""
    local = _ensure(authority, activate=True, provider="local", subject="ada")
    entra = _ensure(authority, activate=True, provider="entra", subject="ada")

    assert local.record.identity_id != entra.record.identity_id


# --------------------------------------------------------------------------
# Reads.
# --------------------------------------------------------------------------


def test_read_identity_returns_the_stored_state(authority) -> None:
    outcome = _ensure(authority, activate=True)
    record = authority.read_identity(identity_id=outcome.record.identity_id)

    assert record is not None
    assert record.identity_id == outcome.record.identity_id
    assert record.is_active is True


def test_read_identity_returns_none_for_an_unknown_id(authority) -> None:
    """An absent row is never an implicit grant — the caller must refuse."""
    assert authority.read_identity(identity_id="no-such-identity") is None


def test_a_row_whose_state_left_the_vocabulary_is_refused_not_coerced(engine, authority) -> None:
    """Reaching this means the CHECK was bypassed; guessing would be worse.

    The CHECK constraint has to be genuinely suspended to set this up
    (``ignore_check_constraints``), which is itself the evidence that the
    schema defends this column today. What is under test is the READER's
    behaviour in the world where that defence has already failed — a dropped
    constraint, a row written by something that bypassed it, or a store that
    is not the store we think it is. In every one of those, refusing is
    correct and coercing to a plausible state would hide it.
    """
    outcome = _ensure(authority, activate=True)
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
        conn.exec_driver_sql(
            "UPDATE identities SET access_state = 'superuser' WHERE identity_id = ?",
            (outcome.record.identity_id,),
        )
        conn.exec_driver_sql("PRAGMA ignore_check_constraints = OFF")

    with pytest.raises(IdentityRowCorruptionError, match="access_state"):
        authority.read_identity(identity_id=outcome.record.identity_id)


# --------------------------------------------------------------------------
# The admission audit is part of the activation, not a follow-up.
# --------------------------------------------------------------------------


def test_the_admission_audit_runs_before_the_activation_commits(authority) -> None:
    """It must see the row it is auditing, inside the same transaction."""
    seen: list[tuple[str, str]] = []

    outcome = authority.ensure_identity(
        claims=_claims(),
        activate=True,
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=_STORAGE,
        record_admission=lambda identity_id, username, _quota: seen.append((identity_id, username)),
    )

    assert seen == [(outcome.record.identity_id, "ada")]


def test_a_failed_admission_audit_rolls_the_whole_activation_back(engine, authority) -> None:
    """The defect this ordering exists to prevent.

    Audited AFTER the commit, a Landscape outage would leave the identity
    already ``active`` while the retry — finding it active — would never
    attempt the audit again. That is an authority change no trail records and
    no path repairs. Rolling back instead means the next login retries both.
    """

    def _audit_fails(_identity_id: str, _username: str, _quota_written: bool) -> None:
        raise RuntimeError("landscape unavailable")

    with pytest.raises(RuntimeError, match="landscape unavailable"):
        authority.ensure_identity(
            claims=_claims(),
            activate=True,
            quota_tokens_per_day=_TOKENS,
            quota_storage_bytes=_STORAGE,
            record_admission=_audit_fails,
        )

    # Nothing survives: no identity, and therefore no quota row either.
    with engine.connect() as conn:
        identities = conn.execute(select(identities_table.c.identity_id)).all()
        quotas = conn.execute(select(quota_policies_table.c.policy_id)).all()
    assert identities == []
    assert quotas == []


def test_a_retry_after_a_failed_audit_admits_and_audits(authority) -> None:
    """Recovery must be automatic — the person just logs in again."""
    attempts: list[str] = []

    def _fails_once(identity_id: str, _username: str, _quota_written: bool) -> None:
        attempts.append(identity_id)
        if len(attempts) == 1:
            raise RuntimeError("landscape unavailable")

    with pytest.raises(RuntimeError):
        authority.ensure_identity(
            claims=_claims(),
            activate=True,
            quota_tokens_per_day=_TOKENS,
            quota_storage_bytes=_STORAGE,
            record_admission=_fails_once,
        )

    outcome = authority.ensure_identity(
        claims=_claims(),
        activate=True,
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=_STORAGE,
        record_admission=_fails_once,
    )

    assert outcome.record.is_active is True
    assert len(attempts) == 2


def test_a_pending_admission_writes_no_audit(authority) -> None:
    """Nothing was granted, so there is no authority change to record."""
    seen: list[str] = []

    authority.ensure_identity(
        claims=_claims(),
        activate=False,
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=_STORAGE,
        record_admission=lambda identity_id, _username, _quota: seen.append(identity_id),
    )

    assert seen == []


def test_a_returning_active_user_writes_no_second_audit(authority) -> None:
    """Otherwise every visit would claim an administrator acted."""
    seen: list[str] = []

    def recorder(identity_id: str, _username: str, _quota: bool) -> None:
        seen.append(identity_id)

    for _ in range(2):
        authority.ensure_identity(
            claims=_claims(),
            activate=True,
            quota_tokens_per_day=_TOKENS,
            quota_storage_bytes=_STORAGE,
            record_admission=recorder,
        )

    assert len(seen) == 1


# --------------------------------------------------------------------------
# Losing the insert race.
# --------------------------------------------------------------------------


def _winner_lands_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the next attempt lose the first-login race to a winner that commits first.

    The single attempt runs twice: the first run is the winner's whole
    transaction, the second is ours, which hits ``uq_identities_provider_subject``
    and takes the documented loser path. The ``IntegrityError`` is real, raised
    by the unique constraint, not simulated.
    """
    original_once = RepositoryIdentityAuthority._ensure_identity_once

    def race_then_run(self: RepositoryIdentityAuthority, **kwargs: Any) -> Any:
        original_once(self, **kwargs)
        return original_once(self, **kwargs)

    monkeypatch.setattr(RepositoryIdentityAuthority, "_ensure_identity_once", race_then_run)


def test_the_loser_of_a_first_login_race_binds_to_the_winners_identity(engine, authority, monkeypatch) -> None:
    """Two concurrent first logins must not become two people, or a 500.

    Without the handler the loser's IntegrityError is not an
    AuthenticationError, so the login route does not catch it — the request
    becomes a 500 with NO login row and no failure row, and the attempt
    vanishes from the audit trail entirely.
    """
    _winner_lands_first(monkeypatch)

    loser = _ensure(authority, activate=True)

    assert loser.created is False
    with engine.connect() as conn:
        rows = conn.execute(select(identities_table.c.identity_id).where(identities_table.c.subject == "ada")).all()
    assert [row.identity_id for row in rows] == [loser.record.identity_id]


def test_the_loser_does_not_write_a_second_activation_audit(authority, monkeypatch) -> None:
    """The winner already wrote the pair; a second would claim two admissions."""
    seen: list[str] = []
    _winner_lands_first(monkeypatch)

    outcome = authority.ensure_identity(
        claims=_claims(),
        activate=True,
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=_STORAGE,
        record_admission=lambda identity_id, _username, _quota: seen.append(identity_id),
    )

    assert outcome.activated_now is False
    # Exactly one admission was audited: the winner's, inside its own attempt.
    assert seen == [outcome.record.identity_id]


def test_an_integrity_error_that_is_not_the_race_still_raises(authority, monkeypatch) -> None:
    """Swallowing every IntegrityError would hide real defects.

    A violation with no row for the natural key afterwards did not come from
    losing the race, and turning it into a successful admission would convert
    a schema or foreign-key defect into a confusing second error later.
    """

    def _explode(self: RepositoryIdentityAuthority, **_kwargs: Any) -> Any:
        raise IntegrityError("simulated", None, Exception("not the natural-key race"))

    monkeypatch.setattr(RepositoryIdentityAuthority, "_ensure_identity_once", _explode)

    with pytest.raises(IntegrityError):
        _ensure(authority, activate=True)
    assert authority.read_identity_by_natural_key(provider="local", subject="ada") is None


# --------------------------------------------------------------------------
# The quota event may not outrun the quota row.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tokens", "storage"),
    [(None, None), (_TOKENS, None), (None, _STORAGE)],
)
def test_no_quota_is_claimed_when_no_quota_row_is_written(engine, authority, tokens, storage) -> None:
    """The two container defaults are INDEPENDENTLY optional.

    Configure one and not the other and no policy row is written — so the
    admission audit must not carry a cap either, or the trail asserts an
    allowance that no row records and points a later refusal at corruption
    rather than at the missing configuration.
    """
    outcome = authority.ensure_identity(
        claims=_claims(),
        activate=True,
        quota_tokens_per_day=tokens,
        quota_storage_bytes=storage,
        record_admission=lambda _i, _u, _q: None,
    )

    assert outcome.quota_written is False
    with engine.connect() as conn:
        assert conn.execute(select(quota_policies_table.c.policy_id)).all() == []


def test_a_written_quota_row_is_reported_to_the_caller(authority) -> None:
    """The positive control for the parametrised refusals above."""
    seen: list[bool] = []
    outcome = authority.ensure_identity(
        claims=_claims(),
        activate=True,
        quota_tokens_per_day=_TOKENS,
        quota_storage_bytes=_STORAGE,
        record_admission=lambda _i, _u, quota_written: seen.append(quota_written),
    )

    assert outcome.quota_written is True
    assert seen == [True]


# --------------------------------------------------------------------------
# Retiring an identity when its credential is deleted.
# --------------------------------------------------------------------------


def test_retiring_frees_the_username_for_a_fresh_identity(authority) -> None:
    """The whole point: a deleted account must not hand its admission on.

    Without retirement the next holder of the username binds to the old row
    and inherits its access_state, its quota row, and every row FK'd to that
    identity_id.
    """
    original = _ensure(authority, activate=True)
    _retire(authority)

    successor = _ensure(authority, activate=True)

    assert successor.created is True
    assert successor.record.identity_id != original.record.identity_id


def test_a_retired_identity_keeps_its_row_and_its_history(authority) -> None:
    """It is disabled, not deleted — the FKs and the audit anchor must survive."""
    original = _ensure(authority, activate=True)

    retired = _retire(authority)

    assert retired is not None
    stored = authority.read_identity(identity_id=original.record.identity_id)
    assert stored is not None
    assert stored.access_state == "disabled"
    assert stored.identity_id == original.record.identity_id


def test_a_retired_binding_cannot_be_reached_by_a_login(authority) -> None:
    """The natural key is retired, so the old row is unreachable by subject."""
    _ensure(authority, activate=True)
    _retire(authority)

    assert authority.read_identity_by_natural_key(provider="local", subject="ada") is None


def test_retiring_the_same_username_twice_does_not_collide(engine, authority) -> None:
    """Each retired subject carries its identity_id, so the unique holds."""
    first = _ensure(authority, activate=True)
    _retire(authority, reason="first")
    second = _ensure(authority, activate=True)
    _retire(authority, reason="second")

    assert first.record.identity_id != second.record.identity_id
    with engine.connect() as conn:
        retired = conn.execute(select(identities_table.c.identity_id).where(identities_table.c.access_state == "disabled")).all()
    assert len(retired) == 2


def test_retiring_an_identity_that_never_existed_is_not_an_error(authority) -> None:
    """An account deleted before its first login has no identity row."""
    assert _retire(authority, "nobody", reason="x") is None
