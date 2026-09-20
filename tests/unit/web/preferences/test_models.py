"""Tests for the ComposerPreferences Pydantic models."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from elspeth.web.preferences.models import (
    ComposerPreferences,
    UpdateComposerPreferencesRequest,
)


@pytest.mark.parametrize("via", ["complete", "skip", "exit"])
def test_completion_requires_explicit_intent_and_freeform(via: str) -> None:
    payload = {
        "tutorial_completed_at": "2026-09-20T00:00:00Z",
        "tutorial_completed_via": via,
        "default_mode": "freeform",
    }
    assert UpdateComposerPreferencesRequest.model_validate(payload).tutorial_completed_via == via
    for missing in ("tutorial_completed_via", "default_mode"):
        with pytest.raises(ValidationError):
            UpdateComposerPreferencesRequest.model_validate({key: value for key, value in payload.items() if key != missing})
    with pytest.raises(ValidationError):
        UpdateComposerPreferencesRequest.model_validate({**payload, "default_mode": "guided"})


@pytest.mark.parametrize("completed_at", [None, "2026-09-20T00:00:00Z"])
@pytest.mark.parametrize(
    ("field", "value"),
    [("tutorial_stage", "guided"), ("tutorial_session_id", "session"), ("tutorial_run_id", "run"), ("tutorial_source_data_hash", "hash")],
)
def test_completion_and_reset_reject_populated_progress(completed_at: str | None, field: str, value: str) -> None:
    payload: dict[str, object] = {"tutorial_completed_at": completed_at, field: value}
    if completed_at is not None:
        payload.update(default_mode="freeform", tutorial_completed_via="complete")
    with pytest.raises(ValidationError, match="progress"):
        UpdateComposerPreferencesRequest.model_validate(payload)


def test_composer_preferences_valid() -> None:
    """A well-formed payload constructs cleanly."""
    payload = ComposerPreferences(
        default_mode="guided",
        freeform_intro_dismissed_at=None,
        tutorial_completed_at=None,
        tutorial_stage=None,
        tutorial_session_id=None,
        tutorial_run_id=None,
        tutorial_source_data_hash=None,
        show_advanced=False,
        updated_at=datetime.now(UTC),
    )
    assert payload.default_mode == "guided"
    assert payload.tutorial_completed_at is None
    assert payload.tutorial_stage is None


def test_composer_preferences_rejects_invalid_mode() -> None:
    """Tier-3 boundary: only 'guided' or 'freeform' are accepted."""
    with pytest.raises(ValidationError):
        ComposerPreferences(
            default_mode="kiosk",  # type: ignore[arg-type]
            tutorial_completed_at=None,
            tutorial_stage=None,
            tutorial_session_id=None,
            tutorial_run_id=None,
            tutorial_source_data_hash=None,
            updated_at=datetime.now(UTC),
        )


def test_update_request_accepts_full_payload() -> None:
    payload = UpdateComposerPreferencesRequest(default_mode="freeform")
    assert payload.default_mode == "freeform"
    assert payload.freeform_intro_dismissed_at is None
    assert payload.tutorial_completed_at is None


def test_update_request_accepts_only_intro_field() -> None:
    """Partial PATCH: caller sets only freeform_intro_dismissed_at."""
    stamp = datetime.now(UTC)
    payload = UpdateComposerPreferencesRequest(freeform_intro_dismissed_at=stamp)
    assert payload.default_mode is None
    assert payload.freeform_intro_dismissed_at == stamp


def test_update_request_accepts_freeform_intro_dismissal() -> None:
    stamp = datetime(2026, 7, 12, 5, 0, tzinfo=UTC)
    payload = UpdateComposerPreferencesRequest(
        freeform_intro_dismissed_at=stamp,
    )

    assert payload.freeform_intro_dismissed_at == stamp
    assert "freeform_intro_dismissed_at" in payload.model_fields_set


def test_update_request_rejects_invalid_mode() -> None:
    with pytest.raises(ValidationError):
        UpdateComposerPreferencesRequest(default_mode="kiosk")  # type: ignore[arg-type]


def test_update_request_accepts_empty_payload_as_noop() -> None:
    """An empty PATCH payload is a no-op; the request succeeds without changes."""
    payload = UpdateComposerPreferencesRequest()
    assert payload.default_mode is None
    assert payload.freeform_intro_dismissed_at is None
    assert payload.tutorial_completed_at is None


def test_composer_preferences_accepts_tutorial_completed_at() -> None:
    stamp = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    payload = ComposerPreferences(
        default_mode="guided",
        freeform_intro_dismissed_at=None,
        tutorial_completed_at=stamp,
        tutorial_stage=None,
        tutorial_session_id=None,
        tutorial_run_id=None,
        tutorial_source_data_hash=None,
        show_advanced=False,
        updated_at=datetime.now(UTC),
    )
    assert payload.tutorial_completed_at == stamp


def test_update_request_accepts_tutorial_completed_at() -> None:
    stamp = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    payload = UpdateComposerPreferencesRequest(default_mode="freeform", tutorial_completed_at=stamp, tutorial_completed_via="complete")
    assert payload.default_mode == "freeform"
    assert payload.freeform_intro_dismissed_at is None
    assert payload.tutorial_completed_at == stamp
    assert "tutorial_completed_at" in payload.model_fields_set


def test_update_request_distinguishes_absent_from_explicit_null_tutorial() -> None:
    absent = UpdateComposerPreferencesRequest()
    explicit_null = UpdateComposerPreferencesRequest(tutorial_completed_at=None)

    assert "tutorial_completed_at" not in absent.model_fields_set
    assert "tutorial_completed_at" in explicit_null.model_fields_set


def test_update_request_rejects_unknown_field() -> None:
    """extra='forbid': a typo in the field name surfaces as ValidationError, not silent no-op."""
    with pytest.raises(ValidationError):
        UpdateComposerPreferencesRequest(default_modd="freeform")  # type: ignore[call-arg]


def test_composer_preferences_rejects_unknown_field() -> None:
    """extra='forbid' on the response model too (codebase convention)."""
    with pytest.raises(ValidationError):
        ComposerPreferences(
            default_mode="guided",
            tutorial_completed_at=None,
            tutorial_stage=None,
            tutorial_session_id=None,
            tutorial_run_id=None,
            tutorial_source_data_hash=None,
            updated_at=datetime.now(UTC),
            extra_key="boom",  # type: ignore[call-arg]
        )


def test_composer_preferences_rejects_invalid_tutorial_stage() -> None:
    """Tier-3 boundary: only the closed TutorialStage set (or None) is accepted;
    'welcome' is deliberately outside the set — it is never persisted."""
    with pytest.raises(ValidationError):
        ComposerPreferences(
            default_mode="guided",
            tutorial_completed_at=None,
            tutorial_stage="welcome",  # type: ignore[arg-type]
            tutorial_session_id="sess-1",
            tutorial_run_id=None,
            tutorial_source_data_hash=None,
            updated_at=datetime.now(UTC),
        )


def test_update_request_accepts_tutorial_progress_fields() -> None:
    payload = UpdateComposerPreferencesRequest(
        tutorial_stage="guided",
        tutorial_session_id="sess-1",
    )
    assert payload.tutorial_stage == "guided"
    assert payload.tutorial_session_id == "sess-1"
    assert "tutorial_stage" in payload.model_fields_set
    assert "tutorial_run_id" not in payload.model_fields_set


def test_update_request_rejects_invalid_tutorial_stage() -> None:
    with pytest.raises(ValidationError):
        UpdateComposerPreferencesRequest(tutorial_stage="welcome")  # type: ignore[arg-type]


def test_update_request_distinguishes_absent_from_explicit_null_stage() -> None:
    absent = UpdateComposerPreferencesRequest()
    explicit_null = UpdateComposerPreferencesRequest(tutorial_stage=None)

    assert "tutorial_stage" not in absent.model_fields_set
    assert "tutorial_stage" in explicit_null.model_fields_set


def test_update_request_accepts_tutorial_completed_via_exit() -> None:
    stamp = datetime(2026, 7, 9, 10, 0, tzinfo=UTC)
    payload = UpdateComposerPreferencesRequest(
        default_mode="freeform",
        tutorial_completed_at=stamp,
        tutorial_completed_via="exit",
    )
    assert payload.tutorial_completed_via == "exit"


def test_update_request_rejects_via_without_completed_at() -> None:
    """The discriminator only qualifies a completion write in the same PATCH."""
    with pytest.raises(ValidationError):
        UpdateComposerPreferencesRequest(tutorial_completed_via="exit")


def test_update_request_rejects_via_with_null_completed_at() -> None:
    """Clearing the gate (retake) is not an exit; the combination is a caller bug."""
    with pytest.raises(ValidationError):
        UpdateComposerPreferencesRequest(
            tutorial_completed_at=None,
            tutorial_completed_via="exit",
        )


def test_update_request_rejects_unknown_via_value() -> None:
    with pytest.raises(ValidationError):
        UpdateComposerPreferencesRequest(
            tutorial_completed_at=datetime(2026, 7, 9, 10, 0, tzinfo=UTC),
            tutorial_completed_via="quit",  # type: ignore[arg-type]
        )


def test_composer_preferences_carries_show_advanced() -> None:
    prefs = ComposerPreferences(
        default_mode="guided",
        freeform_intro_dismissed_at=None,
        tutorial_completed_at=None,
        tutorial_stage=None,
        tutorial_session_id=None,
        tutorial_run_id=None,
        tutorial_source_data_hash=None,
        show_advanced=True,
        updated_at=None,
    )
    assert prefs.show_advanced is True


def test_update_request_accepts_only_show_advanced() -> None:
    req = UpdateComposerPreferencesRequest.model_validate({"show_advanced": True})
    assert req.show_advanced is True
    assert req.model_fields_set == {"show_advanced"}


def test_update_request_rejects_non_bool_show_advanced() -> None:
    # The request model is deliberately NOT strict (ISO datetimes must coerce,
    # models.py:12-19), so pydantic's lax bool accepts "yes"/"1"/"true". A list
    # is the value shape lax mode still rejects.
    with pytest.raises(ValidationError):
        UpdateComposerPreferencesRequest.model_validate({"show_advanced": [1, 2]})
