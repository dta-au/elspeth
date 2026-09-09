"""Contract ownership and legacy import compatibility for sink-effect carriers."""

import elspeth.contracts as contracts
from elspeth.contracts.audit_export import (
    AuditExportSnapshotCandidate,
    AuditExportSnapshotReadLimits,
    AuditExportSnapshotRegistryKey,
    AuditExportSnapshotWinner,
    AuditExportTerminalWitness,
)
from elspeth.contracts.sink_effects import (
    SinkEffectAttemptRequest,
    SinkEffectAttemptResult,
    SinkEffectFinalizationMember,
    SinkEffectFinalizationResult,
    SinkEffectFinalizeRequest,
    SinkEffectLease,
    SinkEffectReservationRequest,
)
from elspeth.core.landscape.execution.audit_export_snapshots import (
    AuditExportSnapshotCandidate as LegacyAuditExportSnapshotCandidate,
)
from elspeth.core.landscape.execution.audit_export_snapshots import (
    AuditExportSnapshotReadLimits as LegacyAuditExportSnapshotReadLimits,
)
from elspeth.core.landscape.execution.audit_export_snapshots import (
    AuditExportSnapshotRegistryKey as LegacyAuditExportSnapshotRegistryKey,
)
from elspeth.core.landscape.execution.audit_export_snapshots import (
    AuditExportSnapshotWinner as LegacyAuditExportSnapshotWinner,
)
from elspeth.core.landscape.execution.sink_effect_finalization import (
    SinkEffectFinalizationMember as LegacySinkEffectFinalizationMember,
)
from elspeth.core.landscape.execution.sink_effect_finalization import (
    SinkEffectFinalizationResult as LegacySinkEffectFinalizationResult,
)
from elspeth.core.landscape.execution.sink_effect_finalization import (
    SinkEffectFinalizeRequest as LegacySinkEffectFinalizeRequest,
)
from elspeth.core.landscape.execution.sink_effect_lifecycle import (
    SinkEffectAttemptRequest as LegacySinkEffectAttemptRequest,
)
from elspeth.core.landscape.execution.sink_effect_lifecycle import (
    SinkEffectAttemptResult as LegacySinkEffectAttemptResult,
)
from elspeth.core.landscape.execution.sink_effect_lifecycle import (
    SinkEffectLease as LegacySinkEffectLease,
)
from elspeth.core.landscape.execution.sink_effect_reservation import (
    SinkEffectReservationRequest as LegacySinkEffectReservationRequest,
)
from elspeth.core.landscape.export_read_model import AuditExportTerminalWitness as LegacyAuditExportTerminalWitness

CONTRACT_CARRIERS = (
    AuditExportSnapshotCandidate,
    AuditExportSnapshotReadLimits,
    AuditExportSnapshotRegistryKey,
    AuditExportSnapshotWinner,
    AuditExportTerminalWitness,
    SinkEffectAttemptRequest,
    SinkEffectAttemptResult,
    SinkEffectFinalizationMember,
    SinkEffectFinalizationResult,
    SinkEffectFinalizeRequest,
    SinkEffectLease,
    SinkEffectReservationRequest,
)


# The package-level re-export of each carrier, paired with the carrier itself.
# Naming both objects is the point of the test, so they are written out rather
# than the package attribute being resolved from ``carrier.__name__`` (ADR-032):
# a carrier that loses its ``elspeth.contracts`` export now fails at import.
CONTRACT_CARRIER_EXPORTS: tuple[tuple[type, object], ...] = (
    (AuditExportSnapshotCandidate, contracts.AuditExportSnapshotCandidate),
    (AuditExportSnapshotReadLimits, contracts.AuditExportSnapshotReadLimits),
    (AuditExportSnapshotRegistryKey, contracts.AuditExportSnapshotRegistryKey),
    (AuditExportSnapshotWinner, contracts.AuditExportSnapshotWinner),
    (AuditExportTerminalWitness, contracts.AuditExportTerminalWitness),
    (SinkEffectAttemptRequest, contracts.SinkEffectAttemptRequest),
    (SinkEffectAttemptResult, contracts.SinkEffectAttemptResult),
    (SinkEffectFinalizationMember, contracts.SinkEffectFinalizationMember),
    (SinkEffectFinalizationResult, contracts.SinkEffectFinalizationResult),
    (SinkEffectFinalizeRequest, contracts.SinkEffectFinalizeRequest),
    (SinkEffectLease, contracts.SinkEffectLease),
    (SinkEffectReservationRequest, contracts.SinkEffectReservationRequest),
)


def test_sink_effect_carriers_are_contract_owned_and_public() -> None:
    assert tuple(carrier for carrier, _exported in CONTRACT_CARRIER_EXPORTS) == CONTRACT_CARRIERS, (
        "CONTRACT_CARRIER_EXPORTS has drifted from CONTRACT_CARRIERS; every carrier must be checked."
    )
    for carrier, exported in CONTRACT_CARRIER_EXPORTS:
        assert carrier.__module__.startswith("elspeth.contracts.")
        assert exported is carrier
        assert carrier.__name__ in contracts.__all__


def test_core_carrier_imports_remain_identity_preserving_re_exports() -> None:
    assert LegacyAuditExportSnapshotCandidate is AuditExportSnapshotCandidate
    assert LegacyAuditExportSnapshotReadLimits is AuditExportSnapshotReadLimits
    assert LegacyAuditExportSnapshotRegistryKey is AuditExportSnapshotRegistryKey
    assert LegacyAuditExportSnapshotWinner is AuditExportSnapshotWinner
    assert LegacyAuditExportTerminalWitness is AuditExportTerminalWitness
    assert LegacySinkEffectAttemptRequest is SinkEffectAttemptRequest
    assert LegacySinkEffectAttemptResult is SinkEffectAttemptResult
    assert LegacySinkEffectFinalizationMember is SinkEffectFinalizationMember
    assert LegacySinkEffectFinalizationResult is SinkEffectFinalizationResult
    assert LegacySinkEffectFinalizeRequest is SinkEffectFinalizeRequest
    assert LegacySinkEffectLease is SinkEffectLease
    assert LegacySinkEffectReservationRequest is SinkEffectReservationRequest
