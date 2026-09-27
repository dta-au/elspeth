"""A test-only batch plugin whose flush emits exactly one row per buffered row.

``output_mode: passthrough`` admits only a batch-aware plugin that declares
``flush_emits_one_row_per_buffered_row``. The one shipped plugin that does,
``batch_rank``, adds four annotation fields to every row; engine tests of the
passthrough path use this identity plugin instead, registered through a test
plugin manager, so what they assert is the engine's carriage of each buffered
row and not a plugin's annotations. ``batch_rank`` itself is exercised under
passthrough by its own tests.
"""

from __future__ import annotations

import contextlib
import copy
import functools
from collections.abc import Iterator

from elspeth.contracts import Determinism
from elspeth.contracts.contexts import TransformContext
from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract
from elspeth.plugins.infrastructure.discovery import create_dynamic_hookimpl
from elspeth.plugins.infrastructure.manager import PluginManager, scoped_plugin_manager
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.transforms.passthrough import PassThrough

PASSTHROUGH_IDENTITY_BATCH = "test_passthrough_identity_batch"


class PassthroughIdentityBatch(PassThrough):
    """Emit a deep copy of every buffered row, in buffered order, as one success_multi."""

    name = PASSTHROUGH_IDENTITY_BATCH
    determinism = Determinism.DETERMINISTIC
    # Not the shipped passthrough's provenance: this test plugin behaves differently.
    source_file_hash: str | None = None
    is_batch_aware = True
    flush_emits_one_row_per_buffered_row = True

    def process(self, row: PipelineRow | list[PipelineRow], ctx: TransformContext) -> TransformResult:
        del ctx
        if not isinstance(row, list):
            raise TypeError(f"{self.name} is batch-aware: the engine hands it the buffered rows as a list")
        rows = row
        merged_fields: dict[str, FieldContract] = {}
        for member in rows:
            for field_contract in member.contract.fields:
                merged_fields.setdefault(field_contract.normalized_name, field_contract)
        output_contract = self._align_output_contract(
            self._apply_declared_output_field_contracts(
                SchemaContract(mode=rows[0].contract.mode, fields=tuple(merged_fields.values()), locked=True)
            )
        )
        return TransformResult.success_multi(
            [PipelineRow(copy.deepcopy(member.to_dict()), output_contract) for member in rows],
            success_reason={"action": "passthrough"},
        )


@functools.cache
def passthrough_batch_plugin_manager() -> PluginManager:
    """The built-in plugins plus ``PassthroughIdentityBatch`` (built once; never mutated after)."""
    manager = PluginManager()
    manager.register_builtin_plugins()
    manager.register(create_dynamic_hookimpl([PassthroughIdentityBatch], "elspeth_get_transforms"))
    return manager


@contextlib.contextmanager
def passthrough_batch_plugins() -> Iterator[None]:
    """Resolve plugin names against ``passthrough_batch_plugin_manager()`` in this context."""
    with scoped_plugin_manager(passthrough_batch_plugin_manager()):
        yield
