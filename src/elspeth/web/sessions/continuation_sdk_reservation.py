"""Explicit SDK-capable hold recipes with exact historical recipe preservation."""

from hashlib import sha256

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, is_lower_sha256_hex
from elspeth.web.sessions.continuation_sdk_tail import SdkTailPlan, prepare_sdk_tail_plan


def extend_sdk_terminal_reservation(
    *,
    legacy_reserved_bytes: int,
    legacy_contract_sha256: str,
    plan: SdkTailPlan,
    version: str,
    historical_contract_sha256: str | None = None,
) -> tuple[int, str]:
    if (
        type(legacy_reserved_bytes) is not int
        or legacy_reserved_bytes <= 0
        or not is_lower_sha256_hex(legacy_contract_sha256)
        or type(plan) is not SdkTailPlan
        or plan
        != prepare_sdk_tail_plan(identity=plan.identity, admitted=plan.admitted, creator_sequence=plan.creator_sequence, family=plan.family)
    ):
        raise AuditIntegrityError("SDK reservation changed an original closed tail")
    if historical_contract_sha256 == legacy_contract_sha256:
        return legacy_reserved_bytes, legacy_contract_sha256
    reserved = max(legacy_reserved_bytes, plan.reserved_bytes)
    manifest = canonical_json(
        {
            "version": version,
            "previous_contract": legacy_contract_sha256,
            "sdk_terminal": plan.contract_sha256,
            "reserved_bytes": reserved,
        }
    )
    digest = sha256(manifest.encode("utf-8")).hexdigest()
    if historical_contract_sha256 is not None and historical_contract_sha256 != digest:
        raise AuditIntegrityError("SDK reservation refuses an unknown historical hold recipe")
    return reserved, digest
