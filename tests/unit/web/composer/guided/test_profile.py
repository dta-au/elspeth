"""Tests for WorkflowProfile - frozen value type + closed-enum discriminator."""

from __future__ import annotations

import dataclasses

import pytest

from elspeth.web.composer.guided.errors import InvariantError
from elspeth.web.composer.guided.profile import (
    EMPTY_PROFILE,
    TUTORIAL_PROFILE,
    WorkflowProfile,
    WorkflowProfileKind,
    kind_for_profile,
    profile_for_kind,
)


class TestKindForProfile:
    """The inverse the guided root-custody helper is load-bearing on.

    A completed ``guided_start``'s request DTO is not persisted, so the only
    way to recover the ``profile`` discriminator it was hashed under is the
    profile constant its result checkpoint carries. If this inverse drifts, a
    rooted session stops verifying and the planner refuses to run on it.
    """

    @pytest.mark.parametrize("kind", list(WorkflowProfileKind))
    def test_round_trips_every_closed_kind(self, kind: WorkflowProfileKind) -> None:
        assert kind_for_profile(profile_for_kind(kind)) is kind

    def test_refuses_a_profile_no_server_preset_mints(self) -> None:
        # ``WorkflowProfile`` is a plain pair of booleans, so a corrupt
        # checkpoint can decode to a combination no preset produces. Guessing
        # a kind for it would forge start-operation authority.
        with pytest.raises(InvariantError, match=r"kind_for_profile"):
            kind_for_profile(WorkflowProfile(coaching=True, bookends=False))


class TestWorkflowProfileShape:
    def test_is_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            EMPTY_PROFILE.coaching = True  # type: ignore[misc]

    def test_empty_profile_is_live_guided_default(self) -> None:
        assert EMPTY_PROFILE.coaching is False
        assert EMPTY_PROFILE.bookends is False

    def test_tutorial_profile_enables_coaching_bookends(self) -> None:
        assert TUTORIAL_PROFILE.coaching is True
        assert TUTORIAL_PROFILE.bookends is True


class TestWorkflowProfileKind:
    def test_kind_values_are_closed(self) -> None:
        assert WorkflowProfileKind.LIVE.value == "live"
        assert WorkflowProfileKind.TUTORIAL.value == "tutorial"
        assert {k.value for k in WorkflowProfileKind} == {"live", "tutorial"}

    def test_profile_for_kind_maps_live_to_empty(self) -> None:
        assert profile_for_kind(WorkflowProfileKind.LIVE) is EMPTY_PROFILE

    def test_profile_for_kind_maps_tutorial(self) -> None:
        assert profile_for_kind(WorkflowProfileKind.TUTORIAL) is TUTORIAL_PROFILE

    def test_unknown_kind_string_rejected_by_enum(self) -> None:
        with pytest.raises(ValueError):
            WorkflowProfileKind("bespoke")


class TestWorkflowProfileSerialisation:
    def test_empty_profile_round_trips(self) -> None:
        assert WorkflowProfile.from_dict(EMPTY_PROFILE.to_dict()) == EMPTY_PROFILE

    def test_tutorial_profile_round_trips(self) -> None:
        assert WorkflowProfile.from_dict(TUTORIAL_PROFILE.to_dict()) == TUTORIAL_PROFILE

    def test_to_dict_emits_the_two_active_profile_keys(self) -> None:
        assert set(EMPTY_PROFILE.to_dict()) == {
            "coaching",
            "bookends",
        }

    def test_from_dict_uses_direct_key_not_get_default(self) -> None:
        # An empty dict must crash, never silently fabricate a profile.
        with pytest.raises(InvariantError, match=r"WorkflowProfile\.from_dict"):
            WorkflowProfile.from_dict({})

    def test_from_dict_rejects_unknown_key(self) -> None:
        # A forked/tampered blob with an injected field must be rejected, not
        # silently ignored - the closed schema is the tamper boundary.
        d = {**TUTORIAL_PROFILE.to_dict(), "stages": ["smuggled"]}
        with pytest.raises(InvariantError, match=r"unexpected keys"):
            WorkflowProfile.from_dict(d)

    def test_from_dict_rejects_removed_advisor_checkpoints_key(self) -> None:
        d = {**TUTORIAL_PROFILE.to_dict(), "advisor_checkpoints": False}
        with pytest.raises(InvariantError, match=r"unexpected keys"):
            WorkflowProfile.from_dict(d)
