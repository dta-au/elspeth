"""Collector parity for the Composer secret-wiring authorization boundary."""

from __future__ import annotations

from typing import Any, cast

from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.secrets import ResolvedSecret, SecretInventoryItem
from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata
from elspeth.web.composer.tools._common import ToolContext
from elspeth.web.composer.tools.secrets import _execute_wire_secret_ref
from elspeth.web.secrets.wiring_policy import SecretWiringPolicy, SecretWiringRule


class _AvailableSecretService:
    def list_refs(self, user_id: str) -> list[SecretInventoryItem]:
        del user_id
        return []

    def has_ref(self, user_id: str, name: str) -> bool:
        del user_id, name
        return True

    def resolve(self, user_id: str, name: str) -> ResolvedSecret | None:
        del user_id, name
        return None


def _collector_state() -> CompositionState:
    collector = NodeSpec(
        id="collect_pages",
        node_type="collector",
        plugin="batch_stats",
        input="pages",
        on_success="main",
        on_error=None,
        options={"schema": {"mode": "observed"}, "value_field": "amount"},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
        scope_name="document_pages",
        scope_opener="explode_pages",
        scope_policy="require_all",
    )
    return CompositionState(
        source=None,
        nodes=(collector,),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def _context(policy: SecretWiringPolicy | None) -> ToolContext:
    return ToolContext(
        catalog=cast(Any, None),
        plugin_snapshot=cast(Any, None),
        secret_service=_AvailableSecretService(),
        secret_wiring_policy=policy,
        user_id="test-user",
    )


def test_wire_collector_target_authorized_by_transform_rule() -> None:
    """Collectors reuse the transform-plugin secret policy vocabulary."""
    state = _collector_state()
    policy = SecretWiringPolicy(
        rules=(
            SecretWiringRule(
                secret="COLLECTOR_API_KEY",
                component_type="transform",
                plugin="batch_stats",
                option_key="api_key",
            ),
        )
    )

    result = _execute_wire_secret_ref(
        {"name": "COLLECTOR_API_KEY", "target": "node", "target_id": "collect_pages", "option_key": "api_key"},
        state,
        _context(policy),
    )

    assert result.success is True
    collector = result.updated_state.nodes[0]
    assert deep_thaw(collector.options)["api_key"] == {"secret_ref": "COLLECTOR_API_KEY"}


def test_wire_collector_target_without_policy_reaches_authorization_denial() -> None:
    """An unallowlisted collector is denied by policy without mutation."""
    state = _collector_state()

    result = _execute_wire_secret_ref(
        {"name": "COLLECTOR_API_KEY", "target": "node", "target_id": "collect_pages", "option_key": "api_key"},
        state,
        _context(None),
    )

    assert result.success is False
    assert result.updated_state is state
    assert "secret_wiring_allowlist" in result.data["error"]
    assert "api_key" not in deep_thaw(result.updated_state.nodes[0].options)
