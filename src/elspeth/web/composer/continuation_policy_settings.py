"""Map WebSettings to the complete admitted Composer Continuation policy.

The policy is built once per process and handed to the composer authority.
Each admitted operation retains its own copy inside its snapshot, so a later
settings change never touches an operation already admitted (design §4.2).
"""

from __future__ import annotations

from typing import Final

from elspeth.web.composer.continuation_policy import (
    ContinuationActionBounds,
    ContinuationActionPolicy,
    ContinuationAdmissionPolicy,
    ContinuationAdmissionPolicyV2,
    ContinuationAdmissionPolicyV3,
    ContinuationAdmissionPolicyV4,
    ContinuationAdmittedCostPolicy,
    ContinuationSegmentPolicy,
)
from elspeth.web.config import WebSettings
from elspeth.web.continuation_settings import (
    ContinuationLocalStageSettings,
    ContinuationProviderStageSettings,
)
from elspeth.web.prompt_guard_config import PromptGuardSettings

# The guard client's own timeout must fire first, so it is recorded as the
# guard's outcome; the continuation stage bound only backs it up.
GUARD_STAGE_MARGIN_SECONDS: Final = 5.0
_DEFAULT_GUARD_TIMEOUT_SECONDS: Final[float] = PromptGuardSettings.model_fields["timeout_seconds"].default


def _guard_seconds(guard: PromptGuardSettings | None) -> float:
    timeout = _DEFAULT_GUARD_TIMEOUT_SECONDS if guard is None else guard.timeout_seconds
    return timeout + GUARD_STAGE_MARGIN_SECONDS


def _provider_action(stages: ContinuationProviderStageSettings, *, guard: float) -> ContinuationActionPolicy:
    return ContinuationActionPolicy(
        preparation=stages.preparation,
        guard=guard,
        quota=stages.quota,
        physical=stages.physical,
        retention=stages.retention,
    )


def _local_action(stages: ContinuationLocalStageSettings) -> ContinuationActionPolicy:
    return ContinuationActionPolicy(
        preparation=stages.preparation,
        guard=None,
        quota=None,
        physical=stages.physical,
        retention=stages.retention,
    )


def continuation_policy_from_settings(settings: WebSettings) -> ContinuationAdmissionPolicyV2:
    """The complete validated Continuation admission policy."""
    if type(settings) is not WebSettings:
        raise TypeError("continuation policy requires the owned WebSettings")
    bounds = settings.composer_continuation_action_bounds
    primary_guard = _guard_seconds(settings.composer_prompt_guard)
    advisor_guard_settings = settings.composer_advisor_prompt_guard
    advisor_guard = _guard_seconds(advisor_guard_settings if advisor_guard_settings is not None else settings.composer_prompt_guard)
    advisor = bounds.advisor
    policy = ContinuationAdmissionPolicyV2(
        segment=ContinuationSegmentPolicy(
            nominal=settings.composer_continuation_segment_nominal_seconds,
            watermark=settings.composer_continuation_watermark_seconds,
            quantum=settings.composer_continuation_quantum_seconds,
            reserve=settings.composer_continuation_settlement_reserve_seconds,
        ),
        actions=ContinuationActionBounds(
            primary=_provider_action(bounds.primary, guard=primary_guard),
            advisor=ContinuationActionPolicy(
                preparation=advisor.preparation,
                guard=advisor_guard,
                quota=advisor.quota,
                physical=settings.composer_advisor_timeout_seconds,
                retention=advisor.retention,
            ),
            title=_provider_action(bounds.title, guard=primary_guard),
            read=_local_action(bounds.read),
            session_sql=_local_action(bounds.session_sql),
            blob=_local_action(bounds.blob),
            external=_local_action(bounds.external),
        ),
        provider_cost=ContinuationAdmittedCostPolicy(
            max_provider_calls=settings.composer_planner_max_provider_calls,
            max_request_bytes=settings.composer_planner_max_request_bytes,
            max_completion_tokens=settings.composer_planner_max_completion_tokens,
            max_cumulative_provider_cost=settings.composer_planner_max_cumulative_provider_cost,
            repair_budget=settings.composer_planner_repair_budget,
        ),
    )
    _require_first_segment_fits_its_chain(policy)
    return policy


def continuation_policy_v3_from_settings(settings: WebSettings) -> ContinuationAdmissionPolicyV3 | None:
    """Build a new-operation policy with the configured aggregate allowance.

    Explicit ``None`` disables new V3 policy construction. Stored V1/V2 operations
    retain their original policy; the application/authority join decides how
    to project this condition publicly before new irreversible work.
    """
    if type(settings) is not WebSettings:
        raise TypeError("continuation policy requires the owned WebSettings")
    limit = settings.composer_continuation_max_retained_payload_bytes
    if limit is None:
        return None
    costed = continuation_policy_from_settings(settings)
    return ContinuationAdmissionPolicyV3(
        segment=costed.segment,
        actions=costed.actions,
        provider_cost=costed.provider_cost,
        max_retained_payload_bytes=limit,
    )


def continuation_policy_v4_from_settings(settings: WebSettings) -> ContinuationAdmissionPolicyV4 | None:
    """Prepare a paid-overflow policy; leave admission to its owner.

    Explicit ``None`` disables new V4 policy construction. This builder
    neither changes the default V2 path nor activates V4.
    """
    if type(settings) is not WebSettings:
        raise TypeError("continuation policy requires the owned WebSettings")
    limit = settings.composer_continuation_max_retained_payload_bytes
    if limit is None:
        return None
    costed = continuation_policy_from_settings(settings)
    return ContinuationAdmissionPolicyV4(
        segment=costed.segment,
        actions=costed.actions,
        provider_cost=costed.provider_cost,
        max_retained_payload_bytes=limit,
        paid_provider_overflow_disposition="terminal_paid_overflow",
    )


# The admitted first segment runs, before the first PRIMARY call, one ingress
# initialization, the TITLE call, its application and the composition
# initialization (three session-SQL actions and one TITLE action). Landed
# owners cannot yield across sequences (design §2.2), so that whole chain and
# the PRIMARY admission (its total plus the reserve) must fit one nominal
# segment, or the first PRIMARY would never be admitted.
_FIRST_SEGMENT_SESSION_SQL_ACTIONS: Final = 3


def _require_first_segment_fits_its_chain(policy: ContinuationAdmissionPolicy | ContinuationAdmissionPolicyV2) -> None:
    actions = policy.actions
    chain = actions.title.total + _FIRST_SEGMENT_SESSION_SQL_ACTIONS * actions.session_sql.total
    required = actions.primary.total + policy.segment.reserve + chain
    if policy.segment.nominal < required:
        raise ValueError(
            "composer_continuation_segment_nominal_seconds must cover the first segment: "
            f"PRIMARY {actions.primary.total:g} s + reserve {policy.segment.reserve:g} s "
            f"+ TITLE and initialization {chain:g} s = {required:g} s"
        )
