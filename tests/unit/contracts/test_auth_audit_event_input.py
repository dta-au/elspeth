"""Auth audit batch inputs retain an immutable snapshot of event metadata."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from elspeth.contracts.auth import AuthAuditEventInput


def test_auth_audit_event_input_detaches_and_deep_freezes_metadata() -> None:
    metadata = {"binding": {"checks": [{"result": "approved"}]}}
    event = AuthAuditEventInput(
        event_type="approval_decided",
        outcome="success",
        provider="local",
        user_id=None,
        username=None,
        failure_category=None,
        request_id=None,
        client_host=None,
        user_agent=None,
        metadata=metadata,
    )

    metadata["binding"]["checks"][0]["result"] = "rejected"
    metadata["binding"]["checks"].append({"result": "changed"})
    assert isinstance(event.metadata, MappingProxyType)
    assert isinstance(event.metadata["binding"], MappingProxyType)
    assert event.metadata["binding"]["checks"] == (MappingProxyType({"result": "approved"}),)
    with pytest.raises(TypeError):
        event.metadata["binding"]["checks"][0]["result"] = "changed"
