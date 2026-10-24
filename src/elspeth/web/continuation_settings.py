"""Operator settings for Composer Continuation per-action stage allowances.

Each role carries only the stages an operator sets. Two stages are derived
elsewhere so one setting keeps one meaning (design §4.1, §4.4):

- the advisor's physical bound is ``composer_advisor_timeout_seconds``;
- every provider role's guard bound is its prompt-guard ``timeout_seconds``
  plus a fixed margin, so the guard client's own timeout fires first.

Local roles (read, session SQL, blob, external) have no guard or quota stage.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

# The upper bound keeps every derived deadline representable as a timedelta.
StageSeconds = Annotated[float, Field(strict=True, gt=0, le=86_400, allow_inf_nan=False)]


class ContinuationProviderStageSettings(BaseModel):
    """Primary and TITLE provider actions; guard is derived, not set here."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    preparation: StageSeconds
    quota: StageSeconds
    physical: StageSeconds
    retention: StageSeconds


class ContinuationAdvisorStageSettings(BaseModel):
    """Advisor provider action; physical is ``composer_advisor_timeout_seconds``."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    preparation: StageSeconds
    quota: StageSeconds
    retention: StageSeconds


class ContinuationLocalStageSettings(BaseModel):
    """A local action: no guard and no quota stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    preparation: StageSeconds
    physical: StageSeconds
    retention: StageSeconds


def _provider(physical: float) -> ContinuationProviderStageSettings:
    return ContinuationProviderStageSettings(preparation=5.0, quota=10.0, physical=physical, retention=5.0)


def _local(physical: float) -> ContinuationLocalStageSettings:
    return ContinuationLocalStageSettings(preparation=5.0, physical=physical, retention=5.0)


class ContinuationActionBoundsSettings(BaseModel):
    """Production defaults and their justification: design §4.4.

    ``session_sql.retention`` is 5.0 s per U-45 §13.4 (post-fix PostgreSQL
    retention 1.45 s loaded; production estimate up to 2.7 s at 50 prior pairs).
    ``primary.physical`` stays below the gateway's 300 s upstream timeout.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    primary: ContinuationProviderStageSettings = Field(default_factory=lambda: _provider(240.0))
    advisor: ContinuationAdvisorStageSettings = Field(
        default_factory=lambda: ContinuationAdvisorStageSettings(preparation=5.0, quota=10.0, retention=5.0)
    )
    title: ContinuationProviderStageSettings = Field(default_factory=lambda: _provider(30.0))
    read: ContinuationLocalStageSettings = Field(default_factory=lambda: _local(10.0))
    session_sql: ContinuationLocalStageSettings = Field(default_factory=lambda: _local(5.0))
    blob: ContinuationLocalStageSettings = Field(default_factory=lambda: _local(30.0))
    external: ContinuationLocalStageSettings = Field(default_factory=lambda: _local(30.0))
