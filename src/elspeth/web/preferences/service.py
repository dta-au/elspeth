"""Service layer for the user_preferences table.

Read path: returns the user's row; falls back to 'guided' when no row exists.
Crashes on Tier-1 read of a corrupt mode value (any stored value outside
{"guided", "freeform"} is a code bug, DB corruption, or tampering — never
a recoverable situation; the DB-level CHECK constraint on
``user_preferences_table.default_composer_mode`` is the first line of
defence, but the read guard here catches the case where the CHECK was
bypassed via direct SQL or schema-version drift).

Write path: upserts the row, touching only fields the caller actually set.
Returns the response model built from the values just written rather than
re-reading — this avoids the corrupt-mode PATCH lockout (Finding 7): a
pre-existing corrupt row would crash a re-read even when the PATCH
write succeeded and supplied a valid new mode.

SQLite and PostgreSQL both use ``ON CONFLICT ... DO UPDATE``, but SQLAlchemy's
upsert construct is dialect-specific. The write path selects the matching
insert builder from the active connection before constructing the statement.

Async/sync bridge: ``run_sync_in_worker`` (``elspeth.web.async_workers``, bounded shared
pool); see ``sessions/service.py`` for the canonical usage pattern.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from opentelemetry import metrics
from sqlalchemy import String, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.composer.tutorial_telemetry import record_tutorial_completed_path
from elspeth.web.preferences.models import (
    ComposerMode,
    ComposerPreferences,
    TutorialStage,
    UpdateComposerPreferencesRequest,
)
from elspeth.web.sessions.models import user_preferences_table


@dataclass(frozen=True, slots=True)
class PriorPreferencesSnapshot:
    """The ``user_preferences`` row as read just before an upsert.

    ``value`` is ``None`` when no row existed (never a synthesised
    default: that would fabricate state the system never wrote).
    ``serialised`` is derived from the engine dialect at call time and
    says whether that read was serialised against concurrent writers of
    the same row. SQLite: ``True``; ``create_session_engine`` opens every
    ``engine.begin()`` with ``BEGIN IMMEDIATE`` and the
    ``contract_invariants.session_engine_factory`` lint forbids any other
    sessions engine. PostgreSQL: ``False``; the transaction is a plain
    READ COMMITTED ``BEGIN`` with no advisory lock, so a concurrent PATCH
    for the same user can commit between this read and the upsert and
    the losing writer's ``value`` is a stale snapshot. The durable row is
    still correct on both dialects (the upsert itself is atomic). A
    snapshot with ``serialised=False`` MUST NOT be promoted into a
    Landscape or audit emit without a user-keyed lock;
    tests/unit/web/preferences/test_prior_snapshot_inventory.py pins that
    no consumer reads ``prior`` at all today.
    """

    value: ComposerPreferences | None
    serialised: bool


@dataclass(frozen=True, slots=True)
class ComposerPreferencesTransition:
    """Result of an account-level composer-preferences PATCH: ``current``
    is built from the values just written; ``prior`` is the pre-write read
    whose per-dialect guarantee ``PriorPreferencesSnapshot`` states.
    """

    prior: PriorPreferencesSnapshot
    current: ComposerPreferences


class CorruptPreferencesError(RuntimeError):
    """Raised when the sessions DB returns a preferences row that violates
    a closed-list invariant (Tier-1 read guard).

    Carrying a named type rather than bare ``RuntimeError`` lets the
    application's exception handlers (``app.py``) match this specific
    failure mode for incident response without string-grepping the
    message. Subclasses ``RuntimeError`` so existing
    ``except RuntimeError`` callers continue to catch it during the
    transition — there are no such callers today; this is forward-fit
    headroom for the explain/diagnose surface in mcp/.

    Attributes:
      user_id: the row's primary key, included so the operator can
        locate it.
      field_name: the closed/typed field that failed its Tier-1 guard.
      bad_value: the offending value, named exactly as stored, so the
        operator can confirm the corruption rather than re-derive it.
    """

    def __init__(self, user_id: str, bad_value: object, *, field_name: str = "default_composer_mode") -> None:
        super().__init__(f"user_preferences row for {user_id!r} has invalid {field_name}={bad_value!r}")
        self.user_id = user_id
        self.field_name = field_name
        self.bad_value = bad_value


# ── Telemetry (Panel S1): operational signal only. Preferences are user
# state, not a pipeline decision boundary, so there is NO Landscape emit;
# the counter is the explicit "nothing to send" acknowledgement. A future
# promotion needs a serialised ``prior`` (see ``PriorPreferencesSnapshot``).
_meter = metrics.get_meter(__name__)
_PREFERENCES_PATCH_COUNTER = _meter.create_counter(
    "composer.preferences.patch_total",
    description=(
        "Composer-preferences PATCH operations. Attributes: mode_changed (bool), "
        "banner_dismissed (bool), tutorial_changed (bool), "
        "tutorial_progress_changed (bool), wrote_row (bool)."
    ),
)

_DEFAULT_MODE: ComposerMode = "guided"
_VALID_MODES: frozenset[ComposerMode] = frozenset({"guided", "freeform"})
# Tier-1 read-guard set for ``tutorial_stage``; lockstep with the
# ``TutorialStage`` Literal (models.py) and the
# ``ck_user_preferences_tutorial_stage`` CHECK (sessions/models.py).
_VALID_TUTORIAL_STAGES: frozenset[TutorialStage] = frozenset({"guided", "run", "audit", "graduation"})


def _utcnow() -> datetime:
    return datetime.now(UTC)


def prior_read_is_serialised(dialect_name: str) -> bool:
    """Whether a pre-upsert read under ``engine.begin()`` is serialised on this dialect.

    SQLite: ``create_session_engine`` opens the transaction with ``BEGIN
    IMMEDIATE``, so no concurrent writer can interpose. Every other dialect
    (PostgreSQL in production) opens a plain READ COMMITTED ``BEGIN``.
    """
    return dialect_name == "sqlite"


def _select_preferences_for_user(user_id: str) -> Any:
    """Select preferences with tutorial timestamp kept as raw text.

    SQLAlchemy's SQLite DateTime result processor raises before the
    service can name the corrupt field if a direct SQL write stores a
    non-datetime string. The tutorial column is new and has an explicit
    Tier-1 guard, so select it as text and parse it in `_row_to_prefs`.
    """
    return select(
        user_preferences_table.c.default_composer_mode,
        user_preferences_table.c.banner_dismissed_at,
        user_preferences_table.c.freeform_intro_dismissed_at,
        sql_cast(user_preferences_table.c.tutorial_completed_at, String).label("tutorial_completed_at"),
        user_preferences_table.c.tutorial_stage,
        user_preferences_table.c.tutorial_session_id,
        user_preferences_table.c.tutorial_run_id,
        user_preferences_table.c.tutorial_source_data_hash,
        user_preferences_table.c.show_advanced,
        user_preferences_table.c.updated_at,
    ).where(user_preferences_table.c.user_id == user_id)


def _decode_tutorial_completed_at(user_id: str, raw_value: object) -> datetime | None:
    if raw_value is None:
        return None
    if type(raw_value) is datetime:
        return raw_value
    if type(raw_value) is str:
        try:
            return datetime.fromisoformat(raw_value.removesuffix("Z") + "+00:00" if raw_value.endswith("Z") else raw_value)
        except ValueError as exc:
            raise CorruptPreferencesError(
                user_id,
                {"tutorial_completed_at": raw_value},
                field_name="tutorial_completed_at",
            ) from exc
    raise CorruptPreferencesError(
        user_id,
        {"tutorial_completed_at": raw_value},
        field_name="tutorial_completed_at",
    )


class PreferencesService:
    """Reads and writes per-user composer preferences."""

    def __init__(self, engine: Engine, *, now: Callable[[], datetime] = _utcnow) -> None:
        self._engine = engine
        self._now = now

    async def get_composer_preferences(self, user_id: str) -> ComposerPreferences:
        """Return the user's preferences, falling back to 'guided' if no row exists.

        Default policy:
          - No row => 'guided' (new-user default; the existing-user
            session-count heuristic was retired under
            ``project_db_migration_policy`` — see plan 12 Task 5).
          - Row exists => use stored value; crash if stored value is
            corrupt.
        """

        def _sync() -> ComposerPreferences:
            with self._engine.connect() as conn:
                row = conn.execute(_select_preferences_for_user(user_id)).first()
                if row is not None:
                    return self._row_to_prefs(row, user_id)

            # No row: return the new-user guided default. We do not write
            # a row here (lazy — avoid write traffic for users who never
            # touch preferences). Panel U1: updated_at=None because no
            # write event exists to associate a timestamp with;
            # fabricating self._now() would put a value the system never
            # actually wrote into an audit-visible field.
            return ComposerPreferences(
                default_mode=_DEFAULT_MODE,
                banner_dismissed_at=None,
                freeform_intro_dismissed_at=None,
                tutorial_completed_at=None,
                tutorial_stage=None,
                tutorial_session_id=None,
                tutorial_run_id=None,
                tutorial_source_data_hash=None,
                show_advanced=False,
                updated_at=None,
            )

        return await run_sync_in_worker(_sync)

    def _row_to_prefs(self, row: Any, user_id: str) -> ComposerPreferences:
        """Convert a DB row to the response model with a Tier-1 read guard.

        A stored mode outside the validated set is a fault we caused
        (bug, tampering, or DB corruption). Crash with the offending
        value named so the operator can diagnose.

        ``row: Any`` matches the established sessions/service.py
        convention (see lines 326, 346, 1945, 2836) and avoids
        ``type: ignore[attr-defined]`` noise on every column access.
        SQLAlchemy ``Row`` objects don't have a useful static type for
        the column attributes the engine exposes via dot access.
        """
        mode = row.default_composer_mode
        if mode not in _VALID_MODES:
            raise CorruptPreferencesError(user_id, mode)
        tutorial_completed_at = _decode_tutorial_completed_at(user_id, row.tutorial_completed_at)
        stage = row.tutorial_stage
        if stage is not None and stage not in _VALID_TUTORIAL_STAGES:
            raise CorruptPreferencesError(user_id, stage, field_name="tutorial_stage")
        return ComposerPreferences(
            default_mode=mode,
            banner_dismissed_at=row.banner_dismissed_at,
            freeform_intro_dismissed_at=row.freeform_intro_dismissed_at,
            tutorial_completed_at=tutorial_completed_at,
            tutorial_stage=stage,
            tutorial_session_id=row.tutorial_session_id,
            tutorial_run_id=row.tutorial_run_id,
            tutorial_source_data_hash=row.tutorial_source_data_hash,
            show_advanced=bool(row.show_advanced),
            updated_at=row.updated_at,
        )

    async def update_composer_preferences(self, user_id: str, payload: UpdateComposerPreferencesRequest) -> ComposerPreferencesTransition:
        """Upsert the preferences row, touching only fields in ``payload``.

        Empty payloads are accepted as no-ops (the request succeeds; if a row already exists,
        only ``updated_at`` advances). An empty PATCH against a user with no row inserts
        nothing (Panel C2), so the GET side's lazy-write contract holds.

        Returns a ``ComposerPreferencesTransition``. ``current`` is built directly from the
        written values, never re-read: a pre-existing corrupt ``default_mode`` row would crash
        a re-read even when this PATCH just wrote a valid mode (Finding 7). The values passed
        the Tier-3 boundary on ``payload``, so the Tier-1 read guard is not re-run on ``current``.

        Concurrency guarantee, per dialect (elspeth-d336060892):

        - Durable row state is correct on BOTH dialects. The write is one
          ``INSERT ... ON CONFLICT (user_id) DO UPDATE`` whose update clause carries
          only the fields the caller set, so two concurrent PATCHes for one user
          never lose each other's fields and never raise a unique violation.
        - ``prior`` is read inside the same transaction as the upsert. On SQLite
          ``create_session_engine`` opens that transaction with ``BEGIN IMMEDIATE``,
          so the read is serialised against every other writer. On PostgreSQL the
          transaction is a plain READ COMMITTED ``BEGIN`` with no advisory lock and
          no ``SELECT ... FOR UPDATE``, so a concurrent PATCH can commit between the
          read and the upsert and the losing writer's ``prior`` is a stale snapshot
          of a row it did not conflict with. The empty-PATCH no-row guard can race
          the same way; both writers then skip, which is harmless because neither
          intended a write.
        - ``transition.prior.serialised`` carries that fact, derived from the engine
          dialect at call time. ``prior`` MUST NOT be promoted into a Landscape or
          audit emit while it can be unserialised; the route discards it and the
          inventory test pins that.

        telemetry: increments ``composer.preferences.patch_total`` with attributes naming which
        fields were touched and whether a row was written (the empty-PATCH-no-row guard reports
        ``wrote_row=False``). Operational signal only: no Landscape emit.
        """
        now = self._now()
        tutorial_in_payload = "tutorial_completed_at" in payload.model_fields_set
        banner_in_payload = "banner_dismissed_at" in payload.model_fields_set
        intro_in_payload = "freeform_intro_dismissed_at" in payload.model_fields_set
        advanced_in_payload = "show_advanced" in payload.model_fields_set
        # Tutorial resume fields (elspeth-918f4434b3) — each carries the same
        # absent-vs-explicit-null discrimination as the banner/tutorial
        # timestamps. See the completion-clears-progress rule below.
        progress_fields = (
            "tutorial_stage",
            "tutorial_session_id",
            "tutorial_run_id",
            "tutorial_source_data_hash",
        )
        progress_in_payload = {name: name in payload.model_fields_set for name in progress_fields}
        any_progress_in_payload = any(progress_in_payload.values())
        payload_is_empty = (
            payload.default_mode is None
            and not banner_in_payload
            and not intro_in_payload
            and not tutorial_in_payload
            and not any_progress_in_payload
            and not advanced_in_payload
        )

        def _sync() -> tuple[ComposerPreferences, bool, ComposerPreferences | None]:
            """Returns (current_prefs, wrote, prior_prefs)."""
            with self._engine.begin() as conn:
                # Load the prior row inside the same transaction as the
                # upsert. ``None`` when no row exists: synthesising a
                # default here would fabricate state the system never
                # wrote (see PriorPreferencesSnapshot). Whether this read
                # is serialised against a concurrent PATCH depends on the
                # dialect; the method docstring states the guarantee.
                prior_row = conn.execute(_select_preferences_for_user(user_id)).first()
                prior_prefs: ComposerPreferences | None
                if prior_row is None:
                    prior_prefs = None
                else:
                    prior_prefs = self._row_to_prefs(prior_row, user_id)

                # Panel C2 guard: empty PATCH against a no-row user is a
                # no-write no-op, so skip the INSERT entirely. On SQLite
                # the prior-load above ran under BEGIN IMMEDIATE and no
                # writer can interpose; on PostgreSQL (READ COMMITTED,
                # no lock) a concurrent PATCH can insert the row between
                # this check and commit. Both writers then skip, which
                # is harmless: neither intended a write.
                if payload_is_empty and prior_row is None:
                    return (
                        ComposerPreferences(
                            default_mode=_DEFAULT_MODE,
                            banner_dismissed_at=None,
                            freeform_intro_dismissed_at=None,
                            tutorial_completed_at=None,
                            tutorial_stage=None,
                            tutorial_session_id=None,
                            tutorial_run_id=None,
                            tutorial_source_data_hash=None,
                            show_advanced=False,
                            updated_at=None,
                        ),
                        False,
                        prior_prefs,
                    )

                # Determine the mode to insert (NOT NULL column). On
                # conflict, only fields the caller set are updated.
                insert_mode: ComposerMode
                if payload.default_mode is not None:
                    insert_mode = payload.default_mode
                elif prior_prefs is not None:
                    insert_mode = prior_prefs.default_mode
                else:
                    insert_mode = _DEFAULT_MODE

                # banner_dismissed_at uses `model_fields_set` to distinguish
                # "absent from JSON" (preserve existing) from "explicit null"
                # (clear the dismissal — re-show the banner on next session).
                # Symmetric with tutorial_completed_at; see models.py docstring.
                if banner_in_payload:
                    resolved_banner: datetime | None = payload.banner_dismissed_at
                elif prior_prefs is not None:
                    resolved_banner = prior_prefs.banner_dismissed_at
                else:
                    resolved_banner = None

                if intro_in_payload:
                    resolved_intro: datetime | None = payload.freeform_intro_dismissed_at
                elif prior_prefs is not None:
                    resolved_intro = prior_prefs.freeform_intro_dismissed_at
                else:
                    resolved_intro = None

                if advanced_in_payload:
                    resolved_advanced: bool = bool(payload.show_advanced)
                elif prior_prefs is not None:
                    resolved_advanced = prior_prefs.show_advanced
                else:
                    resolved_advanced = False

                if tutorial_in_payload:
                    resolved_tutorial: datetime | None = payload.tutorial_completed_at
                elif prior_prefs is not None:
                    resolved_tutorial = prior_prefs.tutorial_completed_at
                else:
                    resolved_tutorial = None

                # Tutorial resume fields. Completion-clears-progress rule:
                # a PATCH that sets OR clears ``tutorial_completed_at`` also
                # clears any resume field it does not itself supply, because
                # completing the tutorial (any door: finish, skip) and
                # resetting it for a retake (the e2e harness recipe —
                # ``PATCH {"tutorial_completed_at": null}``) both terminate
                # any in-progress tutorial. Without this rule a stale
                # ``tutorial_stage`` would resurrect a mid-tutorial resume
                # after a reset, breaking the restart-cleanly contract.
                def _resolve_progress(name: str) -> str | None:
                    if progress_in_payload[name]:
                        value: str | None = getattr(payload, name)
                        return value
                    if tutorial_in_payload:
                        return None
                    if prior_prefs is not None:
                        prior_value: str | None = getattr(prior_prefs, name)
                        return prior_value
                    return None

                resolved_progress = {name: _resolve_progress(name) for name in progress_fields}

                values: dict[str, object] = {
                    "user_id": user_id,
                    "default_composer_mode": insert_mode,
                    "banner_dismissed_at": resolved_banner,
                    "freeform_intro_dismissed_at": resolved_intro,
                    "tutorial_completed_at": resolved_tutorial,
                    "show_advanced": resolved_advanced,
                    "updated_at": now,
                    **resolved_progress,
                }
                dialect = conn.dialect.name
                stmt: Any
                if dialect == "sqlite":
                    stmt = sqlite_insert(user_preferences_table).values(**values)
                elif dialect == "postgresql":
                    stmt = postgresql_insert(user_preferences_table).values(**values)
                else:
                    raise NotImplementedError(
                        "PreferencesService requires an atomic upsert for session database "
                        f"dialect {dialect!r}; supported dialects: sqlite, postgresql"
                    )
                update_clause: dict[str, object] = {"updated_at": now}
                if payload.default_mode is not None:
                    update_clause["default_composer_mode"] = payload.default_mode
                if banner_in_payload:
                    update_clause["banner_dismissed_at"] = payload.banner_dismissed_at
                if intro_in_payload:
                    update_clause["freeform_intro_dismissed_at"] = payload.freeform_intro_dismissed_at
                if tutorial_in_payload:
                    update_clause["tutorial_completed_at"] = payload.tutorial_completed_at
                if advanced_in_payload:
                    update_clause["show_advanced"] = resolved_advanced
                for name in progress_fields:
                    # Written when the caller supplied the field OR the
                    # completion-clears-progress rule applies.
                    if progress_in_payload[name] or tutorial_in_payload:
                        update_clause[name] = resolved_progress[name]
                stmt = stmt.on_conflict_do_update(index_elements=["user_id"], set_=update_clause)
                row = conn.execute(
                    stmt.returning(
                        user_preferences_table.c.default_composer_mode,
                        user_preferences_table.c.banner_dismissed_at,
                        user_preferences_table.c.freeform_intro_dismissed_at,
                        sql_cast(user_preferences_table.c.tutorial_completed_at, String).label("tutorial_completed_at"),
                        user_preferences_table.c.tutorial_stage,
                        user_preferences_table.c.tutorial_session_id,
                        user_preferences_table.c.tutorial_run_id,
                        user_preferences_table.c.tutorial_source_data_hash,
                        user_preferences_table.c.show_advanced,
                        user_preferences_table.c.updated_at,
                    )
                ).one()

            returned = self._row_to_prefs(row, user_id)
            current = ComposerPreferences(
                default_mode=payload.default_mode if payload.default_mode is not None else returned.default_mode,
                banner_dismissed_at=payload.banner_dismissed_at if banner_in_payload else returned.banner_dismissed_at,
                freeform_intro_dismissed_at=(
                    payload.freeform_intro_dismissed_at if intro_in_payload else returned.freeform_intro_dismissed_at
                ),
                tutorial_completed_at=payload.tutorial_completed_at if tutorial_in_payload else returned.tutorial_completed_at,
                # The RETURNING row carries the post-write resolved values
                # (including the completion-clears-progress rule), so the
                # resume fields read straight off ``returned``.
                tutorial_stage=returned.tutorial_stage,
                tutorial_session_id=returned.tutorial_session_id,
                tutorial_run_id=returned.tutorial_run_id,
                tutorial_source_data_hash=returned.tutorial_source_data_hash,
                show_advanced=returned.show_advanced,
                updated_at=now,
            )
            return current, True, prior_prefs

        current, wrote, prior_prefs = await run_sync_in_worker(_sync)
        # Panel S1: operational telemetry only — no Landscape (user state,
        # not pipeline decision boundary). See module-level comment for
        # the no-Landscape rationale and the future-promote criterion.
        _PREFERENCES_PATCH_COUNTER.add(
            1,
            attributes={
                "mode_changed": payload.default_mode is not None,
                "banner_dismissed": payload.banner_dismissed_at is not None,
                "freeform_intro_dismissed": payload.freeform_intro_dismissed_at is not None,
                "tutorial_changed": tutorial_in_payload,
                "tutorial_progress_changed": any_progress_in_payload,
                "wrote_row": wrote,
            },
        )
        if tutorial_in_payload:
            prior_tutorial = prior_prefs.tutorial_completed_at if prior_prefs is not None else None
            addressed_mode = "default_mode" in payload.model_fields_set
            # The explicit discriminator outranks the payload-shape inference
            # below: an exit-to-freeform opt-out (elspeth-61591e64bb) is a
            # one-key completion write that shape-reads as "skip" (or, with a
            # mode change riding along, "first_time").
            if payload.tutorial_completed_at is not None and payload.tutorial_completed_via == "exit":
                record_tutorial_completed_path("exit")
            elif prior_tutorial is None and payload.tutorial_completed_at is not None and addressed_mode:
                record_tutorial_completed_path("first_time")
            elif prior_tutorial is None and payload.tutorial_completed_at is not None and not addressed_mode:
                record_tutorial_completed_path("skip")
            elif prior_tutorial is not None and payload.tutorial_completed_at is None:
                record_tutorial_completed_path("retake")
            elif prior_tutorial is not None and payload.tutorial_completed_at is not None:
                record_tutorial_completed_path("repeat")
        # Derived from the engine dialect at call time, outside ``_sync`` so
        # the writer's transaction body stays exactly what the session-DB
        # mutation-authority manifest fingerprints.
        prior = PriorPreferencesSnapshot(
            value=prior_prefs,
            serialised=prior_read_is_serialised(self._engine.dialect.name),
        )
        return ComposerPreferencesTransition(prior=prior, current=current)
