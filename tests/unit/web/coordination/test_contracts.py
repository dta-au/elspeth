"""Closed coordination value-object and protocol contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from typing import cast

import pytest

import elspeth.web.coordination as coordination
import elspeth.web.coordination.contracts as coordination_contracts
from elspeth.web.coordination.contracts import (
    PROTOCOL_BUMP_NOT_REQUIRED_CHANGES,
    PROTOCOL_BUMP_REQUIRED_CHANGES,
    WEB_COORDINATION_PROTOCOL_VERSION,
    ArchiveDeleteReconciliation,
    ArchiveManifestRelation,
    CancellationSource,
    CompatibilityKey,
    FenceLossReason,
    InstanceState,
    RecoveryRequiredReason,
    RunOwnershipFence,
    RunSagaState,
    SessionOperationFence,
    SessionOperationFenceLost,
    SessionOperationKind,
    SessionOperationLeaseDisposition,
    SessionOperationTerminalOutcomeUnknown,
    StartPermitState,
)


def test_authority_value_objects_are_frozen_with_exact_fields() -> None:
    compatibility = CompatibilityKey(session_epoch=37, landscape_epoch=29, coordination_protocol=1)
    operation = SessionOperationFence(
        session_id="session-1",
        operation_id="operation-1",
        lease_token="random-authority-token",
        operation_epoch=2,
    )
    ownership = RunOwnershipFence(run_id="run-1", owner_instance_id="instance-1", owner_epoch=1)

    assert tuple(field.name for field in fields(CompatibilityKey)) == (
        "session_epoch",
        "landscape_epoch",
        "coordination_protocol",
    )
    assert tuple(field.name for field in fields(SessionOperationFence)) == (
        "session_id",
        "operation_id",
        "lease_token",
        "operation_epoch",
    )
    assert tuple(field.name for field in fields(RunOwnershipFence)) == (
        "run_id",
        "owner_instance_id",
        "owner_epoch",
    )
    with pytest.raises(FrozenInstanceError):
        compatibility.session_epoch = 38  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        operation.lease_token = "replacement"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        ownership.owner_epoch = 2  # type: ignore[misc]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: CompatibilityKey(session_epoch=0, landscape_epoch=29, coordination_protocol=1),
        lambda: CompatibilityKey(session_epoch=37, landscape_epoch=True, coordination_protocol=1),
        lambda: SessionOperationFence(session_id=" ", operation_id="op", lease_token="token", operation_epoch=1),
        lambda: SessionOperationFence(session_id="s", operation_id="op", lease_token=" ", operation_epoch=1),
        lambda: RunOwnershipFence(run_id="run", owner_instance_id="", owner_epoch=1),
        lambda: RunOwnershipFence(run_id="run", owner_instance_id="owner", owner_epoch=0),
    ],
)
def test_authority_value_objects_reject_blank_ids_and_nonpositive_exact_epochs(factory) -> None:
    with pytest.raises(ValueError):
        factory()


def test_protocol_version_and_bump_rules_are_explicit_and_closed() -> None:
    assert WEB_COORDINATION_PROTOCOL_VERSION == 1
    assert (
        frozenset(
            {
                "membership",
                "session_operation_fence",
                "run_ownership_fence",
                "typed_run_start_permit",
                "run_start_saga",
                "atomic_baseline",
                "cancellation",
                "recovery",
                "cleanup_claim",
                "execution_authority",
            }
        )
        == PROTOCOL_BUMP_REQUIRED_CHANGES
    )
    assert (
        frozenset(
            {
                "compatible_telemetry",
                "provider_only",
                "documentation_only",
            }
        )
        == PROTOCOL_BUMP_NOT_REQUIRED_CHANGES
    )


def test_coordination_state_and_reason_enums_are_bounded() -> None:
    assert {item.value for item in InstanceState} == {"active", "draining", "stopped"}
    assert {item.value for item in SessionOperationKind} == {
        "create",
        "compose",
        "proposal",
        "execute",
        "archive",
        "progress",
        "blob_read",
        "session_fork",
    }
    assert {item.value for item in StartPermitState} == {
        "pending",
        "start_permitted",
        "cancelled_before_permit",
    }
    assert {item.value for item in RunSagaState} == {
        "draft",
        "start_intent",
        "start_permit_issued",
        "baseline_checkpointed",
        "running",
        "recovery_required",
        "cancel_pending",
        "terminal",
        "terminal_cancelled",
    }
    assert {item.value for item in CancellationSource} == {"user", "operator", "shutdown", "reconciler"}
    assert "unknown" in {item.value for item in RecoveryRequiredReason}
    assert {item.value for item in ArchiveDeleteReconciliation} == {"current", "consumed"}
    assert {item.value for item in ArchiveManifestRelation} == {
        "current_operation",
        "stale_operation",
    }
    assert {item.value for item in SessionOperationLeaseDisposition} == {
        "active",
        "released",
        "consumed",
        "lost",
        "unknown",
    }


def test_session_operation_context_is_final_frozen_slotted_and_retains_exact_fence_identity() -> None:
    fence = SessionOperationFence(
        session_id="session-identifier",
        operation_id="operation-identifier",
        lease_token="lease-identifier",
        operation_epoch=3,
    )

    context = coordination_contracts.SessionOperationContext(
        fence=fence,
        operation_kind=SessionOperationKind.BLOB_READ,
    )

    assert tuple(field.name for field in fields(coordination_contracts.SessionOperationContext)) == (
        "fence",
        "operation_kind",
    )
    assert context.fence is fence
    assert context.operation_kind is SessionOperationKind.BLOB_READ
    assert context.__class__.__final__ is True
    assert not hasattr(context, "__dict__")
    assert coordination.SessionOperationContext is coordination_contracts.SessionOperationContext
    with pytest.raises(FrozenInstanceError):
        context.operation_kind = SessionOperationKind.COMPOSE  # type: ignore[misc]


def test_session_operation_context_rejects_non_exact_field_types_without_identifier_leakage() -> None:
    class DerivedFence(SessionOperationFence):
        pass

    derived_fence = DerivedFence(
        session_id="secret-session-identifier",
        operation_id="secret-operation-identifier",
        lease_token="secret-lease-identifier",
        operation_epoch=3,
    )
    with pytest.raises(TypeError) as fence_error:
        coordination_contracts.SessionOperationContext(
            fence=derived_fence,
            operation_kind=SessionOperationKind.BLOB_READ,
        )

    exact_fence = SessionOperationFence(
        session_id="other-secret-session",
        operation_id="other-secret-operation",
        lease_token="other-secret-lease",
        operation_epoch=4,
    )
    with pytest.raises(TypeError) as kind_error:
        coordination_contracts.SessionOperationContext(
            fence=exact_fence,
            operation_kind=cast("SessionOperationKind", "blob_read"),
        )

    rendered = f"{fence_error.value!s} {fence_error.value!r} {kind_error.value!s} {kind_error.value!r}"
    for identifier in (
        "secret-session-identifier",
        "secret-operation-identifier",
        "secret-lease-identifier",
        "other-secret-session",
        "other-secret-operation",
        "other-secret-lease",
    ):
        assert identifier not in rendered


def test_fence_loss_errors_are_low_cardinality_and_leak_safe() -> None:
    error = SessionOperationFenceLost(FenceLossReason.TOKEN_MISMATCH)

    rendered = f"{error!s} {error!r}"
    assert "token_mismatch" in rendered
    assert "session-" not in rendered
    assert "operation-" not in rendered
    assert "lease_token" not in rendered


def test_terminal_outcome_unknown_is_identifier_free_and_accepts_no_context() -> None:
    error = SessionOperationTerminalOutcomeUnknown()

    assert str(error) == "session operation terminal outcome is unknown"
    with pytest.raises(TypeError):
        SessionOperationTerminalOutcomeUnknown("secret-session")  # type: ignore[call-arg]
    rendered = f"{error!s} {error!r}"
    assert "session-" not in rendered
    assert "operation-" not in rendered
    assert "lease_token" not in rendered
