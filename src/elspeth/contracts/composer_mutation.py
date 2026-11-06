"""Request-bound ceilings for automatic Composer mutations."""

from dataclasses import dataclass
from typing import Literal

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind

type MutationTrustMode = Literal["auto_commit", "explicit_approve"]


def _valid_trust_mode(value: object) -> bool:
    return value in ("auto_commit", "explicit_approve")


@dataclass(frozen=True, slots=True)
class ComposerMutationCeiling:
    session_id: str
    request_id: str
    initial_trust_mode: MutationTrustMode

    def __post_init__(self) -> None:
        if type(self.session_id) is not str or not self.session_id:
            raise ValueError("Mutation ceiling requires its session identity")
        if type(self.request_id) is not str or not self.request_id:
            raise ValueError("Mutation ceiling requires its logical request identity")
        if not _valid_trust_mode(self.initial_trust_mode):
            raise ValueError("Mutation ceiling has an invalid initial trust mode")

    def validate_context(self, context: SessionOperationContext) -> None:
        if (
            type(context) is not SessionOperationContext
            or context.operation_kind is not SessionOperationKind.COMPOSE
            or context.fence.session_id != self.session_id
        ):
            raise AuditIntegrityError("Mutation ceiling differs from current COMPOSE custody")

    def permits_auto(self, current: MutationTrustMode) -> bool:
        if not _valid_trust_mode(current):
            raise AuditIntegrityError("Session has an invalid trust mode")
        return self.initial_trust_mode == "auto_commit" and current == "auto_commit"


@dataclass(frozen=True, slots=True)
class ComposerMutationRevocation:
    ceiling: ComposerMutationCeiling
    current_trust_mode: MutationTrustMode

    def __post_init__(self) -> None:
        if type(self.ceiling) is not ComposerMutationCeiling:
            raise TypeError("Mutation revocation requires an owned ceiling")
        if self.ceiling.permits_auto(self.current_trust_mode):
            raise ValueError("Allowed mutation cannot be reported as revoked")


class ComposerAutoMutationRefused(RuntimeError):
    def __init__(self, revocation: ComposerMutationRevocation) -> None:
        if type(revocation) is not ComposerMutationRevocation:
            raise TypeError("Mutation refusal requires an owned revocation")
        self.revocation = revocation
        super().__init__("Automatic mutation authority is no longer available for this request.")
