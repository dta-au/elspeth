"""Closed nominal family dispatch; normalized DATA is never a writer input."""

from typing import Any

from sqlalchemy import Connection, Row

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.composer.provider_sdk_observation import V4SdkFailureObservation, require_original_v4_sdk_failure
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity
from elspeth.web.sessions.continuation_retained_capacity import ProviderTerminalHoldSource
from elspeth.web.sessions.continuation_sdk_source import SdkTerminalLiveSource, SdkTerminalSource
from elspeth.web.sessions.continuation_sdk_tail import SdkTerminalFamily


def sdk_terminal_family_for_custody(custody: ProviderCallCustody) -> SdkTerminalFamily:
    from elspeth.web.sessions.continuation_checkpoint_paid import CheckpointPreparedSource, V4CheckpointProviderAdmission
    from elspeth.web.sessions.continuation_classifier_primary import V4ClassifierProviderAdmission
    from elspeth.web.sessions.continuation_primary import V4PrimaryProviderAdmission
    from elspeth.web.sessions.continuation_semantic_advisor import V4SemanticProviderAdmission
    from elspeth.web.sessions.continuation_title_provider import V4TitleProviderAdmission

    if type(custody) is not ProviderCallCustody:
        raise TypeError("SDK family requires original nominal custody")
    admissions = tuple(
        item
        for item in (
            custody.continuation_primary_v4_admission,
            custody.continuation_classifier_v4_admission,
            custody.continuation_semantic_v4_admission,
            custody.continuation_title_v4_admission,
            custody.continuation_checkpoint_v4_admission,
        )
        if item is not None
    )
    if len(admissions) != 1:
        raise AuditIntegrityError("SDK family lost its sole original admission")
    admission = admissions[0]
    if type(admission) is V4CheckpointProviderAdmission:
        checkpoint = admission.original_source
        if type(checkpoint) is not CheckpointPreparedSource:
            raise AuditIntegrityError("SDK checkpoint family lost its nominal original source")
        if checkpoint.phase == "end" and checkpoint.early_input_ref is None:
            return SdkTerminalFamily.CHECKPOINT_END
        if (
            checkpoint.phase == "early"
            and checkpoint.kind == "iteration_early"
            and checkpoint.early_input_ref is not None
            and checkpoint.end_input_ref is None
        ):
            return SdkTerminalFamily.CHECKPOINT_EARLY
        raise AuditIntegrityError("SDK checkpoint family has no installed original phase/input")
    if type(admission) is V4PrimaryProviderAdmission:
        return SdkTerminalFamily.PRIMARY
    if type(admission) is V4ClassifierProviderAdmission:
        return SdkTerminalFamily.CLASSIFIER
    if type(admission) is V4SemanticProviderAdmission:
        return SdkTerminalFamily.SEMANTIC
    if type(admission) is V4TitleProviderAdmission:
        return SdkTerminalFamily.TITLE
    raise AuditIntegrityError("SDK family has no installed nominal admission")


def require_sdk_live_source_on_connection(
    conn: Connection, *, custody: ProviderCallCustody, observation: V4SdkFailureObservation
) -> SdkTerminalLiveSource:
    from elspeth.web.sessions.continuation_checkpoint_sdk_source import require_checkpoint_sdk_source_on_connection
    from elspeth.web.sessions.continuation_classifier_sdk_source import require_classifier_sdk_source_on_connection
    from elspeth.web.sessions.continuation_primary_sdk_source import require_primary_sdk_source_on_connection
    from elspeth.web.sessions.continuation_semantic_sdk_source import require_semantic_sdk_source_on_connection
    from elspeth.web.sessions.continuation_title_sdk_source import require_title_sdk_source_on_connection

    require_original_v4_sdk_failure(observation, custody=custody)
    if custody.continuation_checkpoint_v4 is not None:
        family = sdk_terminal_family_for_custody(custody)
        verified = require_checkpoint_sdk_source_on_connection(conn, custody=custody, observation=observation)
        if verified.source.family is not family:
            raise AuditIntegrityError("SDK checkpoint live source changed its original admission family")
        return verified
    if custody.continuation_primary_v4 is not None:
        return require_primary_sdk_source_on_connection(conn, custody=custody, observation=observation)
    if custody.continuation_semantic_v4 is not None:
        return require_semantic_sdk_source_on_connection(conn, custody=custody, observation=observation)
    if custody.continuation_title is not None:
        return require_title_sdk_source_on_connection(conn, custody=custody, observation=observation)
    if custody.continuation_classifier_v4 is not None:
        return require_classifier_sdk_source_on_connection(conn, custody=custody, observation=observation)
    raise AuditIntegrityError("SDK terminal family has no installed original source factory")


def require_sdk_cold_source_on_connection(
    conn: Connection,
    *,
    family: SdkTerminalFamily,
    source: ProviderTerminalHoldSource,
    admitted: StoredContinuationSnapshot,
    identity: PaidOverflowIdentity,
    effect: Row[Any],
    physical_row: Row[Any],
    parent: Row[Any],
    names: frozenset[str],
) -> SdkTerminalSource:
    from elspeth.web.sessions.continuation_checkpoint_sdk_source import require_checkpoint_sdk_cold_source_on_connection
    from elspeth.web.sessions.continuation_classifier_sdk_source import require_classifier_sdk_cold_source_on_connection
    from elspeth.web.sessions.continuation_primary_sdk_source import require_primary_sdk_cold_source_on_connection
    from elspeth.web.sessions.continuation_semantic_sdk_source import require_semantic_sdk_cold_source_on_connection
    from elspeth.web.sessions.continuation_title_sdk_source import require_title_sdk_cold_source_on_connection

    if type(family) is not SdkTerminalFamily:
        raise TypeError("SDK cold source requires a nominal family selector")
    if family in (SdkTerminalFamily.CHECKPOINT_END, SdkTerminalFamily.CHECKPOINT_EARLY):
        verified = require_checkpoint_sdk_cold_source_on_connection(
            conn, source=source, admitted=admitted, identity=identity, effect=effect, physical_row=physical_row, parent=parent, names=names
        )
        if verified.family is not family:
            raise AuditIntegrityError("SDK checkpoint cold source changed its requested family")
        return verified
    if family is SdkTerminalFamily.PRIMARY:
        return require_primary_sdk_cold_source_on_connection(
            conn, source=source, admitted=admitted, identity=identity, effect=effect, physical_row=physical_row, parent=parent, names=names
        )
    if family is SdkTerminalFamily.SEMANTIC:
        return require_semantic_sdk_cold_source_on_connection(
            conn,
            source=source,
            admitted=admitted,
            identity=identity,
            effect=effect,
            physical_row=physical_row,
            parent=parent,
            names=names,
        )
    if family is SdkTerminalFamily.TITLE:
        return require_title_sdk_cold_source_on_connection(
            conn,
            source=source,
            admitted=admitted,
            identity=identity,
            effect=effect,
            physical_row=physical_row,
            parent=parent,
            names=names,
        )
    if family is SdkTerminalFamily.CLASSIFIER:
        return require_classifier_sdk_cold_source_on_connection(
            conn,
            source=source,
            admitted=admitted,
            identity=identity,
            effect=effect,
            physical_row=physical_row,
            parent=parent,
            names=names,
        )
    raise AuditIntegrityError("SDK terminal family has no installed original cold source factory")
