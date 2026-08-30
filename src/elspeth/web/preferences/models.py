"""Pydantic models for the composer-preferences API.

``ComposerPreferences`` is the full response payload for GET; it is also the
response for PATCH (the service returns the post-write state). The PATCH
request body is ``UpdateComposerPreferencesRequest``, which is a partial
form where each field is independently optional.

``ComposerPreferences`` (response, server-built) uses
``ConfigDict(strict=True, extra="forbid")``. ``strict=True`` is safe on
the server side because the datetime fields are always real ``datetime``
instances at construction.

``UpdateComposerPreferencesRequest`` (request, JSON-bound) uses
``ConfigDict(extra="forbid")`` only — same pattern as
``blobs/schemas.py::CreateInlineBlobRequest``. ``strict=True`` on a
request body with a ``datetime`` field would reject the standard JSON
ISO-8601 string representation (Pydantic v2 strict mode rejects
string→datetime coercion entirely), which is too aggressive for a
Tier-3 boundary whose contract is "validate, coerce where the standard
wire format permits, never fabricate".

The Literal ``ComposerMode`` still rejects ``"kiosk"`` and any other
out-of-set value on both models, and ``extra="forbid"`` rejects typos.

The Literal ``ComposerMode`` is the single source of truth for the
permitted-values set. It is paired with:
  - the DB-level CHECK constraint on ``user_preferences_table``
  - the Tier-1 read guard in ``PreferencesService._row_to_prefs``
  - the ``MODES`` Record in the frontend's
    ``api/preferencesDecoder.ts``, which is keyed by the ``ComposerMode``
    TS union so a frontend-side addition is a compile error there

Extending the set requires updating all four call sites in lockstep —
the Literal here, the CHECK in ``sessions/models.py``, the service read
guard, and the decoder's Record. Only the first three are Python; the
decoder is the cross-language one, and adding a value here without it
makes the decoder reject a now-valid payload.

Separately from the permitted-VALUES covenant above, the FIELD SET of
``ComposerPreferences`` is itself a closed cross-language contract. The
decoder's ``KEYS`` tuple rejects a missing key and an unexpected key
alike, so adding or removing a field here without editing ``KEYS``
breaks every preferences GET at runtime while CI stays green. That axis
is gated executably by
``tests/unit/web/composer/test_preferences_decoder_parity.py`` rather
than by this comment, so a field change fails pytest instead of failing
at the user.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

ComposerMode = Literal["guided", "freeform"]

# First-run tutorial resume stage (elspeth-918f4434b3). Mirrors the frontend
# ``TutorialStep`` union (tutorialMachine.ts) minus ``"welcome"`` — the
# Welcome bookend is never persisted (nothing has started; ``None`` is the
# no-in-progress-tutorial state). Extending this set requires updating the
# Literal here, the ``ck_user_preferences_tutorial_stage`` CHECK in
# ``sessions/models.py``, the Tier-1 read guard in
# ``PreferencesService._row_to_prefs``, and the ``STAGES`` Record in the
# frontend's ``api/preferencesDecoder.ts`` in lockstep — same rule, and the
# same four sites, as ``ComposerMode`` above. The decoder fails closed on an
# unlisted stage, so adding a value here without it makes the frontend reject
# a payload the server considers valid.
TutorialStage = Literal["guided", "run", "audit", "graduation"]


class ComposerPreferences(BaseModel):
    """The full preferences payload returned by GET and PATCH.

    ``updated_at`` is nullable (Panel U1): when no DB row exists for the
    user, the response payload represents the in-server *default* — there
    has been no write event to associate a timestamp with, and fabricating
    ``self._now()`` here would put a value in the audit-visible field that
    the system never actually wrote (CLAUDE.md fabrication test). The
    no-row GET path and the empty-PATCH-on-no-row path both return
    ``updated_at=None``; every other response returns the real write time.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    default_mode: ComposerMode
    banner_dismissed_at: datetime | None
    freeform_intro_dismissed_at: datetime | None
    tutorial_completed_at: datetime | None
    # In-progress tutorial resume state. All four are NULL when no tutorial
    # is in progress. ``tutorial_run_id`` / ``tutorial_source_data_hash``
    # are recorded once the tutorial run completes so the audit step can
    # resume without re-executing the pipeline.
    tutorial_stage: TutorialStage | None
    tutorial_session_id: str | None
    tutorial_run_id: str | None
    tutorial_source_data_hash: str | None
    # Detail level (elspeth-9c11df65f8): True shows engineer/auditor detail
    # the standard view keeps behind summaries.
    show_advanced: bool
    updated_at: datetime | None


class UpdateComposerPreferencesRequest(BaseModel):
    """Partial-update payload for PATCH.

    Every field is independently optional; the service writes only the
    fields the caller actually set. An empty PATCH is a no-op (the
    request succeeds; ``updated_at`` is bumped if any row already
    exists; if no row exists, none is created — see PreferencesService
    Panel C2 guard for the no-insert contract).

    ``banner_dismissed_at`` semantics:

      - Field absent from JSON → unchanged.
      - JSON ``null`` → clear the banner dismissal (the banner re-shows
        on next session — there is no separate "un-dismiss" RPC).
      - ISO-8601 datetime string → set to that value (records the
        dismissal time).

    This field uses ``model_fields_set`` in the service so the
    re-show affordance can distinguish "not mentioned" from "clear it".

    ``tutorial_completed_at`` semantics:

      - Field absent from JSON → unchanged.
      - JSON ``null`` → clear/reset the tutorial completion gate.
      - ISO-8601 datetime string → set to that value.

    This field uses ``model_fields_set`` in the service so the reset
    affordance can distinguish "not mentioned" from "clear it".

    Tutorial resume fields (``tutorial_stage`` / ``tutorial_session_id`` /
    ``tutorial_run_id`` / ``tutorial_source_data_hash``) follow the same
    absent-vs-explicit-null discrimination via ``model_fields_set``. They
    interact with ``tutorial_completed_at`` through the service's
    completion-clears-progress rule: a PATCH that sets OR clears
    ``tutorial_completed_at`` also clears any resume fields it does not
    itself supply, because completing (or resetting for a retake — the e2e
    harness recipe) terminates any in-progress tutorial. See
    ``PreferencesService.update_composer_preferences``.
    """

    model_config = ConfigDict(extra="forbid")

    default_mode: ComposerMode | None = None
    banner_dismissed_at: datetime | None = None
    freeform_intro_dismissed_at: datetime | None = None
    tutorial_completed_at: datetime | None = None
    tutorial_stage: TutorialStage | None = None
    tutorial_session_id: str | None = None
    tutorial_run_id: str | None = None
    tutorial_source_data_hash: str | None = None
    show_advanced: bool | None = None
    # Request-only telemetry discriminator (never persisted, not in the GET
    # payload): qualifies a completion write as an explicit tutorial exit
    # (the in-tutorial "Exit tutorial" / exit-to-freeform opt-out,
    # elspeth-61591e64bb). Without it the server's payload-shape inference
    # would bucket an exit as "skip". Only meaningful alongside a non-null
    # ``tutorial_completed_at`` in the same PATCH — enforced below.
    tutorial_completed_via: Literal["exit"] | None = None

    @model_validator(mode="after")
    def _via_requires_completion_write(self) -> "UpdateComposerPreferencesRequest":
        if self.tutorial_completed_via is not None and self.tutorial_completed_at is None:
            raise ValueError("tutorial_completed_via requires a non-null tutorial_completed_at in the same PATCH")
        return self
