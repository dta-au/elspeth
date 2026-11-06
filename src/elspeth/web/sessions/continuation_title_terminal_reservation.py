"""TITLE SDK-capable H, preserving the exact original three-tail contract."""

from elspeth.web.sessions.continuation_sdk_reservation import extend_sdk_terminal_reservation
from elspeth.web.sessions.continuation_sdk_tail import SdkTerminalFamily, prepare_sdk_tail_plan
from elspeth.web.sessions.continuation_title_no_sdk_tail import TitleNoSdkTailPlan
from elspeth.web.sessions.continuation_title_paid_incomplete_tail import TitlePaidIncompleteTailPlan, title_combined_terminal_hold_contract
from elspeth.web.sessions.continuation_title_paid_overflow_tail import TitlePaidOverflowTailPlan


def title_sdk_terminal_reservation(
    paid: TitlePaidOverflowTailPlan,
    incomplete: TitlePaidIncompleteTailPlan,
    no_sdk: TitleNoSdkTailPlan,
    *,
    historical_contract_sha256: str | None = None,
) -> tuple[int, str]:
    reserved, contract = title_combined_terminal_hold_contract(paid, incomplete, no_sdk)
    plan = prepare_sdk_tail_plan(
        identity=paid.identity, admitted=paid.admitted, creator_sequence=paid.creator_sequence, family=SdkTerminalFamily.TITLE
    )
    return extend_sdk_terminal_reservation(
        legacy_reserved_bytes=reserved,
        legacy_contract_sha256=contract,
        plan=plan,
        version="composer.title-v4-combined-tail.v2",
        historical_contract_sha256=historical_contract_sha256,
    )
