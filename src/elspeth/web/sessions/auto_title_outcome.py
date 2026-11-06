"""One actual live title invocation's observations, never durable phase authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.prompt_guard import PromptGuardMode
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.composer.prompt_guard import PromptGuardMount
from elspeth.web.composer.provider_quota import ProviderCallCustody
from elspeth.web.coordination.quota_authority import ProviderAttempt
from elspeth.web.sessions.protocol import SessionRecord, SessionServiceProtocol

if TYPE_CHECKING:
    from elspeth.web.sessions._auto_title import _AdmittedAutoTitleCompletion

type TitleDisposition = Literal[
    "incomplete", "empty_request", "admission_refused", "provider_failure", "empty_response", "rejected", "updated"
]


@dataclass(frozen=True, slots=True)
class AutoTitleOutcome:
    disposition: TitleDisposition
    response: object | None
    admitted: _AdmittedAutoTitleCompletion | None
    audit_call: ComposerLLMCall | None
    attempt: ProviderAttempt | None
    candidate: str | None
    rejection_class: str | None
    handled_error: BaseException | None
    title_record: SessionRecord | None


@dataclass(frozen=True, slots=True)
class AutoTitleReturned:
    outcome: AutoTitleOutcome


@dataclass(frozen=True, slots=True)
class AutoTitleRaised:
    outcome: AutoTitleOutcome
    error: BaseException


@dataclass(slots=True)
class _AutoTitleCapture:
    claimed: bool = False
    disposition: TitleDisposition = "incomplete"
    response: object | None = None
    admitted: _AdmittedAutoTitleCompletion | None = None
    audit_call: ComposerLLMCall | None = None
    attempt: ProviderAttempt | None = None
    candidate: str | None = None
    rejection_class: str | None = None
    handled_error: BaseException | None = None
    title_record: SessionRecord | None = None

    def snapshot(self) -> AutoTitleOutcome:
        # Only the original producer sites can supply invocation observations.
        # An invalid or unrelated supplied custody must not populate this result.
        return AutoTitleOutcome(
            self.disposition,
            self.response,
            self.admitted,
            self.audit_call,
            self.attempt,
            self.candidate,
            self.rejection_class,
            self.handled_error,
            self.title_record,
        )


async def observe_auto_title_session(
    *,
    service: SessionServiceProtocol,
    session_id: UUID,
    user_message: str,
    model: str,
    temperature: float | None,
    seed: int | None,
    session_operation_context: SessionOperationContext,
    prompt_guard_mount: PromptGuardMount | None,
    prompt_guard_mode: PromptGuardMode,
    api_base: str | None = None,
    api_key: str | None = None,
    provider_custody: ProviderCallCustody | None = None,
) -> AutoTitleReturned | AutoTitleRaised:
    """Observe the same actual helper; return original raised values explicitly.

    Legacy callers still use its None-returning/raising entry unchanged. This
    private live result is not serializable recovery, a retry or a write grant.
    """
    from elspeth.web.sessions._auto_title import maybe_auto_title_session

    capture = _AutoTitleCapture()
    try:
        await maybe_auto_title_session(
            service=service,
            session_id=session_id,
            user_message=user_message,
            model=model,
            temperature=temperature,
            seed=seed,
            session_operation_context=session_operation_context,
            prompt_guard_mount=prompt_guard_mount,
            prompt_guard_mode=prompt_guard_mode,
            api_base=api_base,
            api_key=api_key,
            provider_custody=provider_custody,
            _outcome=capture,
        )
    except BaseException as error:
        return AutoTitleRaised(capture.snapshot(), error)
    return AutoTitleReturned(capture.snapshot())
