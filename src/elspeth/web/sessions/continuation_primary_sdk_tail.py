"""Original PRIMARY recipe interface, preserving canonical v1 tail bytes."""

from datetime import datetime

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity
from elspeth.web.sessions.continuation_sdk_tail import SdkTailBatch as PrimarySdkTailBatch
from elspeth.web.sessions.continuation_sdk_tail import SdkTailPlan as PrimarySdkTailPlan
from elspeth.web.sessions.continuation_sdk_tail import SdkTerminalFamily, build_sdk_tail_batch, prepare_sdk_tail_plan
from elspeth.web.sessions.continuation_sdk_terminal import SdkTerminalReceipt


def prepare_primary_sdk_tail_plan(
    *, identity: PaidOverflowIdentity, admitted: StoredContinuationSnapshot, creator_sequence: int
) -> PrimarySdkTailPlan:
    return prepare_sdk_tail_plan(identity=identity, admitted=admitted, creator_sequence=creator_sequence, family=SdkTerminalFamily.PRIMARY)


def build_primary_sdk_tail_batch(plan: PrimarySdkTailPlan, *, receipt: SdkTerminalReceipt, published_at: datetime) -> PrimarySdkTailBatch:
    if type(plan) is not PrimarySdkTailPlan or plan.family is not SdkTerminalFamily.PRIMARY:
        raise AuditIntegrityError("PRIMARY SDK batch requires its original nominal family")
    return build_sdk_tail_batch(plan, receipt=receipt, published_at=published_at)
