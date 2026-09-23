"""Control checksums bind provenance even when origins share a provider role."""

import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.control_messages import (
    advisor_signoff_withheld_control_envelope,
    anti_anchor_control_envelope,
    replay_composer_control_message,
)
from elspeth.web.sessions.routes._helpers import _composer_chat_history
from tests.fixtures.identities import ensure_test_identity

from .conftest import build_test_sessions_service


@pytest.fixture(params=[anti_anchor_control_envelope, advisor_signoff_withheld_control_envelope])
def envelope_factory(request: pytest.FixtureRequest) -> Callable[[str], dict[str, str]]:
    return request.param


def test_registered_origins_have_distinct_checksums_for_identical_content() -> None:
    content = "The same provider-visible control text."
    anti_anchor = anti_anchor_control_envelope(content)
    advisor = advisor_signoff_withheld_control_envelope(content)

    assert anti_anchor["provider_role"] == advisor["provider_role"] == "user"
    assert anti_anchor["content_hash"] != advisor["content_hash"]


@pytest.mark.parametrize("schema", ["composer.control-message.v1", "composer.control-message.v2"])
def test_content_only_checksum_cannot_be_replayed_or_relabelled_as_v2(
    envelope_factory: Callable[[str], dict[str, str]], schema: str
) -> None:
    content = "Old content-only control."
    envelope = envelope_factory(content)
    envelope["schema"] = schema
    envelope["content_hash"] = hashlib.sha256(content.encode("utf-8")).hexdigest()

    with pytest.raises(AuditIntegrityError):
        replay_composer_control_message(stored_role="audit", writer_principal="compose_loop", content=content, tool_calls=[envelope])


@pytest.mark.parametrize("swap_origin", [False, True], ids=["intact", "registered-origin-swap"])
def test_control_replay_binds_registered_origin(envelope_factory: Callable[[str], dict[str, str]], swap_origin: bool) -> None:
    content = "A control message with Unicode: café\nCATEGORY: quoted text"
    envelope = envelope_factory(content)
    if swap_origin:
        envelope["origin"] = "advisor_signoff_withheld" if envelope["origin"] == "anti_anchor" else "anti_anchor"
        with pytest.raises(AuditIntegrityError):
            replay_composer_control_message(stored_role="audit", writer_principal="compose_loop", content=content, tool_calls=[envelope])
    else:
        assert replay_composer_control_message(
            stored_role="audit", writer_principal="compose_loop", content=content, tool_calls=[envelope]
        ) == {"role": "user", "content": content}


@pytest.mark.asyncio
@pytest.mark.parametrize("swap_origin", [False, True], ids=["intact", "registered-origin-swap"])
async def test_persisted_control_origin_is_checked_on_history_reconstruction(
    tmp_path: Path, envelope_factory: Callable[[str], dict[str, str]], swap_origin: bool
) -> None:
    sessions = build_test_sessions_service(data_dir=tmp_path)
    with sessions._engine.begin() as conn:
        ensure_test_identity(conn, identity_id="control-origin-user")
    session = await sessions.create_session("control-origin-user", "Control custody", "local")
    content = "Backend control text."
    envelope = envelope_factory(content)
    if swap_origin:
        envelope["origin"] = "advisor_signoff_withheld" if envelope["origin"] == "anti_anchor" else "anti_anchor"
    await sessions.add_message(session.id, "audit", content, tool_calls=[envelope], writer_principal="compose_loop")
    messages = await sessions.get_messages(session.id, limit=None)

    if swap_origin:
        with pytest.raises(AuditIntegrityError):
            _composer_chat_history(messages)
    else:
        assert _composer_chat_history(messages) == [{"role": "user", "content": content}]
