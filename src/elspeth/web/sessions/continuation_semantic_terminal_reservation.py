"""Semantic Advisor SDK-capable H with exact historical three-tail reads."""

from elspeth.web.sessions.continuation_sdk_reservation import extend_sdk_terminal_reservation
from elspeth.web.sessions.continuation_sdk_tail import SdkTerminalFamily, prepare_sdk_tail_plan
from elspeth.web.sessions.continuation_semantic_paid_incomplete_tail import (
    SemanticPaidIncompleteTailPlan,
    semantic_combined_terminal_hold_contract,
)
from elspeth.web.sessions.continuation_semantic_paid_overflow_no_sdk import SemanticNoSdkTailPlan
from elspeth.web.sessions.continuation_semantic_paid_overflow_tail import SemanticPaidOverflowTailPlan


def semantic_sdk_terminal_reservation(
    paid: SemanticPaidOverflowTailPlan,
    incomplete: SemanticPaidIncompleteTailPlan,
    no_sdk: SemanticNoSdkTailPlan,
    *,
    historical_contract_sha256: str | None = None,
) -> tuple[int, str]:
    reserved, contract = semantic_combined_terminal_hold_contract(paid, incomplete, no_sdk)
    plan = prepare_sdk_tail_plan(
        identity=paid.identity, admitted=paid.admitted, creator_sequence=paid.creator_sequence, family=SdkTerminalFamily.SEMANTIC
    )
    return extend_sdk_terminal_reservation(
        legacy_reserved_bytes=reserved,
        legacy_contract_sha256=contract,
        plan=plan,
        version="composer.semantic-v4-combined-tail.v3",
        historical_contract_sha256=historical_contract_sha256,
    )
