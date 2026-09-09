# tests/fixtures/nonconforming_transform.py
"""A transform-shaped fake that deliberately does NOT satisfy TransformProtocol.

elspeth-8783933d99: the engine dispatches nominally on GateSettings (negative
form) over containers that are closed by construction — protocol conformance
is deliberately NOT a dispatch control, because widening the runtime_checkable
protocol silently reclassifies every implementation tree-wide (ef5e6e593).

``NonConformingTransform`` models the full attribute surface the engine
actually reads on the transform path (the ``_UncalledBatchTransform`` shape
from test_follower_processor.py) while omitting ``preserves_input_values`` —
the exact member whose addition to TransformProtocol caused the original
mid-traversal ``TypeError``. Tests use it to pin that such an object still
dispatches as a transform. Do NOT "fix" the missing member: its absence is
the point.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

from elspeth.contracts.data import PluginSchema
from elspeth.contracts.plugin_capabilities import WebConfigAuthority
from elspeth.plugins.infrastructure.results import TransformResult


class _AnyRowSchema(PluginSchema):
    """Field-free schema: accepts any row (extra fields are ignored)."""


class NonConformingTransform:
    """Transform-shaped fake missing ``preserves_input_values``.

    ``isinstance(instance, TransformProtocol)`` is False by construction —
    tests assert that as a precondition so the fake cannot silently drift
    into conformance.
    """

    def __init__(
        self,
        *,
        node_id: str | None,
        name: str = "nonconforming-transform",
        is_batch_aware: bool = False,
        on_success: str | None = None,
        on_error: str = "discard",
    ) -> None:
        self.name = name
        self.input_schema = _AnyRowSchema
        self.output_schema = _AnyRowSchema
        self.node_id = node_id
        self.config: dict[str, Any] = {}
        self.determinism = None
        self.plugin_version = "1.0"
        self.source_file_hash = None
        self.usage_when_to_use = None
        self.usage_when_not_to_use = None
        self.example_use = None
        self.capability_tags: tuple[str, ...] = ()
        self.web_config_authority = WebConfigAuthority.USER_CONFIGURABLE
        self.policy_capabilities = frozenset()
        self.audit_characteristics = frozenset()
        self.discovery_secret_requirements = MappingProxyType({})
        self._on_start_called = True
        self._on_complete_called = False
        self.is_batch_aware = is_batch_aware
        self.supports_row_mode_when_batch_aware = False
        self.creates_tokens = False
        self.passes_through_input = False
        self.forwards_input_fields = False
        # DELIBERATELY ABSENT: preserves_input_values. The fake must fail
        # TransformProtocol conformance so tests can prove dispatch does not
        # depend on it.
        self.removed_input_fields: frozenset[str] = frozenset()
        self.can_drop_rows = False
        self.declared_output_fields: frozenset[str] = frozenset()
        self.declared_input_fields: frozenset[str] = frozenset()
        self.declared_string_input_fields: frozenset[str] = frozenset()
        self.requires_runtime_preflight = False
        self._output_schema_config = None
        self.on_error = on_error
        self.on_success = on_success
        self.process_called = False

    def effective_static_contract(self) -> frozenset[str]:
        return self.declared_output_fields

    def process(self, row: Any, ctx: Any) -> TransformResult:
        self.process_called = True
        return TransformResult.success(row, success_reason={"action": "passthrough"})

    def close(self) -> None:  # pragma: no cover - lifecycle surface only
        return None
