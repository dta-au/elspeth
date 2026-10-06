"""Required emulator lanes must not pass by silently dropping blob coverage."""

import pytest
from tests.fixtures.azurite import _azurite_unavailable, azurite_blob_container


@pytest.mark.parametrize("reason", ["Azurite CLI not found.", "Azurite failed to start."])
def test_required_azurite_fails_instead_of_skipping(reason: str) -> None:
    with pytest.raises(pytest.fail.Exception, match=reason):
        _azurite_unavailable(reason, required=True)


def test_optional_developer_azurite_retains_skip() -> None:
    with pytest.raises(pytest.skip.Exception, match="Azurite CLI not found"):
        _azurite_unavailable("Azurite CLI not found.", required=False)


@pytest.mark.parametrize("required", [True, False])
def test_container_connection_failure_respects_required_mode(
    required: bool, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("azure.storage.blob")
    monkeypatch.setattr(request.config.option, "require_azurite", required)
    outcome = pytest.fail.Exception if required else pytest.skip.Exception
    with pytest.raises(outcome, match="Azurite connection failed"):
        next(azurite_blob_container.__wrapped__({"connection_string": "invalid"}, request))
