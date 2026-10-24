"""Real-store, synthetic-only controls for secret inventory interpretation."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
import sqlalchemy as sa

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.discovery_response import admit_discovery_result, serialize_admitted_discovery_result
from elspeth.web.composer.pipeline_planner import planner_tool_definitions
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.provider_gateway import composer_loop_tool_definitions
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools._dispatch import execute_tool
from elspeth.web.composer.tools.wire_projection import decode_wire_arguments
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.secrets.server_store import ServerSecretStore
from elspeth.web.secrets.service import ScopedSecretResolver, WebSecretService
from elspeth.web.secrets.user_store import UserSecretStore
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import user_secrets_table
from elspeth.web.sessions.schema import initialize_session_schema
from tests.fixtures.identities import ensure_test_identity


@pytest.fixture
def inventory_engine(monkeypatch: pytest.MonkeyPatch) -> Iterator[sa.Engine]:
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "synthetic-list-contract-fingerprint")
    monkeypatch.setenv("SYNTHETIC_LIST_SHARED", "synthetic-server-canary")
    engine = create_session_engine("sqlite:///:memory:")
    initialize_session_schema(engine)
    with engine.begin() as connection:
        ensure_test_identity(connection, identity_id="inventory-owner")
        ensure_test_identity(connection, identity_id="other-owner")
    yield engine
    engine.dispose()


def _call(resolver: ScopedSecretResolver, tool: str = "list_secret_refs", arguments: dict[str, object] | None = None):
    full = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(full)
    catalog = PolicyCatalogView.for_trained_operator(full, snapshot)
    state = CompositionState(sources={}, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(name="Inventory"), version=7)
    before = state.to_dict()
    result = execute_tool(
        tool,
        {} if arguments is None else arguments,
        state,
        catalog,
        plugin_snapshot=snapshot,
        secret_service=resolver,
        user_id="inventory-owner",
        validate_arguments=True,
        raise_schema_argument_errors=True,
    )
    assert result.updated_state is state
    assert state.to_dict() == before
    assert result.updated_state.version == 7
    assert result.affected_nodes == ()
    admitted = admit_discovery_result(tool, result)
    wire = json.loads(serialize_admitted_discovery_result(admitted))
    assert wire["success"] is True
    assert wire["version"] == 7
    assert wire["affected_nodes"] == []
    # Discovery success does not assert that this intentionally empty graph is valid.
    assert wire["validation"]["is_valid"] is False
    assert "synthetic-user-canary" not in json.dumps(wire)
    assert "synthetic-server-canary" not in json.dumps(wire)
    return wire["data"]


@pytest.mark.parametrize("row_state", ["intact", "corrupt", "old-key"])
def test_metadata_readiness_is_not_live_resolution(inventory_engine: sa.Engine, row_state: str) -> None:
    store = UserSecretStore(engine=inventory_engine, master_key="synthetic-original-master-key")
    store.set_secret("SYNTHETIC_LIST_SHARED", value="synthetic-user-canary", user_id="inventory-owner", auth_provider_type="local")
    if row_state == "corrupt":
        with inventory_engine.begin() as connection:
            connection.execute(sa.update(user_secrets_table).values(encrypted_value=b"synthetic-invalid-ciphertext"))
    elif row_state == "old-key":
        store = UserSecretStore(engine=inventory_engine, master_key="synthetic-replacement-master-key")
    server = ServerSecretStore(allowlist=("SYNTHETIC_LIST_SHARED",))
    assert server.has_secret("SYNTHETIC_LIST_SHARED") is True
    resolver = ScopedSecretResolver(WebSecretService(store, server), "local")
    listed = _call(resolver)
    assert listed == [{"name": "SYNTHETIC_LIST_SHARED", "scope": "user", "available": True, "source_kind": "user_store", "reason": None}]
    checked = _call(resolver, "validate_secret_ref", {"name": "SYNTHETIC_LIST_SHARED"})
    assert checked == {
        **listed[0],
        "available": row_state == "intact",
        "reason": None if row_state == "intact" else "value_decryption_failed",
    }


@pytest.mark.parametrize("boundary", ["other-user", "other-provider", "disabled-user", "missing-fingerprint", "empty-server"])
def test_scoping_and_unavailability_boundaries(inventory_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch, boundary: str) -> None:
    store = UserSecretStore(engine=inventory_engine, master_key="synthetic-original-master-key")
    store.set_secret(
        "SYNTHETIC_LIST_SHARED",
        value="synthetic-user-canary",
        user_id="other-owner" if boundary == "other-user" else "inventory-owner",
        auth_provider_type="oidc" if boundary == "other-provider" else "local",
    )
    if boundary == "missing-fingerprint":
        monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY")
    if boundary == "empty-server":
        monkeypatch.setenv("SYNTHETIC_LIST_SHARED", "")
    service = WebSecretService(store, ServerSecretStore(("SYNTHETIC_LIST_SHARED",)), user_secrets_enabled=boundary != "disabled-user")
    listed = _call(ScopedSecretResolver(service, "local"))
    if boundary in {"other-user", "other-provider", "disabled-user"}:
        assert listed == [{"name": "SYNTHETIC_LIST_SHARED", "scope": "server", "available": True, "source_kind": "env", "reason": None}]
    elif boundary == "missing-fingerprint":
        assert listed == [
            {
                "name": "SYNTHETIC_LIST_SHARED",
                "scope": "user",
                "available": False,
                "source_kind": "user_store",
                "reason": "fingerprint_resolver_not_configured",
            }
        ]
    else:
        # An intact user row still wins over an empty server variable.
        assert listed == [
            {"name": "SYNTHETIC_LIST_SHARED", "scope": "user", "available": True, "source_kind": "user_store", "reason": None}
        ]


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"_elspeth_no_arguments": False},
        {"_elspeth_no_arguments": 1},
        {"_elspeth_no_arguments": None},
        {"_elspeth_no_arguments": True, "extra": "x"},
    ],
)
def test_strict_empty_marker_rejects_noncanonical_calls(raw: dict[str, object]) -> None:
    with pytest.raises(ToolArgumentError):
        decode_wire_arguments("list_secret_refs", ToolContractDialect.OPENAI_STRICT, raw)


@pytest.mark.parametrize(
    "dialect,raw", [(ToolContractDialect.NONE, {}), (ToolContractDialect.OPENAI_STRICT, {"_elspeth_no_arguments": True})]
)
def test_dialect_decode_enters_real_dispatch(inventory_engine: sa.Engine, dialect: ToolContractDialect, raw: dict[str, object]) -> None:
    resolver = ScopedSecretResolver(
        WebSecretService(UserSecretStore(engine=inventory_engine, master_key="synthetic-master"), ServerSecretStore(())), "local"
    )
    decoded = decode_wire_arguments("list_secret_refs", dialect, raw)
    assert decoded.wire_conformant is True
    semantic = deep_thaw(decoded.semantic)
    assert semantic == {}
    assert _call(resolver, arguments=semantic) == []


@pytest.mark.parametrize("dialect", [ToolContractDialect.NONE, ToolContractDialect.OPENAI_STRICT])
def test_offered_description_distinguishes_metadata_from_resolution(dialect: ToolContractDialect) -> None:
    loop = next(tool for tool in composer_loop_tool_definitions(dialect) if tool["function"]["name"] == "list_secret_refs")
    planner = next(tool for tool in planner_tool_definitions(dialect=dialect) if tool["function"]["name"] == "list_secret_refs")
    assert loop == planner
    description = loop["function"]["description"]
    assert "listing does not check decryption" in description
    assert "Call validate_secret_ref" in description
    assert "true when it resolves for you" not in description
