"""Unit tests for resume audit snapshot boundary immutability."""

from __future__ import annotations

from collections.abc import MutableMapping
from types import MappingProxyType
from typing import cast
from unittest.mock import Mock

import pytest

from elspeth.contracts.types import NodeID
from elspeth.core.checkpoint.recovery import RecoveryManager
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.engine.orchestrator.resume import _ResumeAuditSnapshot


def test_resume_audit_snapshot_freezes_container_fields() -> None:
    """Caller-owned audit maps must not remain mutable through the snapshot.

    The snapshot carries only topology-stable reconstruction (the per-source
    name/lifecycle maps); the run's scheduler work is race-sensitive and is
    read by the processor under leadership. So this pins the freeze contract
    for exactly the fields the read-only snapshot owns.
    """
    source_id = NodeID("source-1")
    source_id_after_creation = NodeID("source-2")

    source_names_by_source = {source_id: "source"}
    source_lifecycle_by_source = {source_id: "exhausted"}

    snapshot = _ResumeAuditSnapshot(
        factory=Mock(spec=RecorderFactory),
        recovery=Mock(spec=RecoveryManager),
        run_id="run-1",
        worker_id="worker-1",
        source_names_by_source=source_names_by_source,
        source_lifecycle_by_source=source_lifecycle_by_source,
    )

    # Mutating the caller-owned dicts after construction must not leak in.
    source_names_by_source[source_id_after_creation] = "source-2"
    source_lifecycle_by_source[source_id_after_creation] = "loaded"

    for frozen_map in (
        snapshot.source_names_by_source,
        snapshot.source_lifecycle_by_source,
    ):
        assert isinstance(frozen_map, MappingProxyType)
        assert source_id_after_creation not in frozen_map

    with pytest.raises(TypeError):
        cast(MutableMapping[NodeID, str], snapshot.source_names_by_source)[source_id_after_creation] = "nope"
