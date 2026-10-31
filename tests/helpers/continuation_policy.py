"""The admitted continuation policy that continuation owner tests run against.

It is the production policy, built through the same settings mapping as
``web/app.py``, never a second set of literals (design §4.5, U-45 §13.5).
A test whose subject is a deadline builds its own explicit small policy.
"""

from __future__ import annotations

from typing import Any, Final

from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicy
from elspeth.web.composer.continuation_policy_settings import _FIRST_SEGMENT_SESSION_SQL_ACTIONS, continuation_policy_from_settings
from elspeth.web.config import WebSettings
from elspeth.web.continuation_settings import ContinuationActionBoundsSettings, ContinuationLocalStageSettings

# Session-SQL retention allowance for structure-observer trials only.
#
# B6 observer trials (tests/helpers/continuation_memo.py) run their session-SQL
# work under a per-call profile hook that re-validates every guarded class slot
# at each SQL event. Measured 2026-11-01 on Python 3.12 and 3.13: the original
# append retention stage takes 0.037 s unobserved and about 21 s observed when
# run alone. Across all 79 observer trials run at -n 8 per interpreter, the
# slowest retention stage took 50.3 s (3.13) / 48.4 s (3.12), median about 7 s.
# The production 5 s bound (design §4.4, U-45 §13.4) is a production SQL limit
# and cannot be these trials' clock. Value: twice the measured maximum, rounded
# up to a whole minute. Re-derive it if observer cost or trial content changes.
OBSERVED_SQL_TRIAL_RETENTION_SECONDS: Final = 120.0

# Session-SQL physical allowance for observer trials whose window also spans
# the physical stage (a RetentionMemoTrace opened before the first-Compose
# construction in tests/unit/web/sessions/test_continuation_composition_initialization.py).
# Measured 2026-11-01 on Python 3.13 at -n 3, host load about 14: that observed
# physical stage took up to 6.7 s (4.6 s serial) against the production 5 s
# bound. Same rule as above: twice the measured maximum, rounded up to a whole
# minute. Re-derive it if observer cost or trial content changes.
OBSERVED_SQL_TRIAL_PHYSICAL_SECONDS: Final = 60.0


def continuation_test_settings(**overrides: Any) -> WebSettings:
    """Minimal valid WebSettings; continuation fields keep their production defaults."""
    required: dict[str, Any] = {
        "composer_max_composition_turns": 15,
        "composer_max_discovery_turns": 10,
        "composer_timeout_seconds": 85.0,
        "composer_rate_limit_per_minute": 10,
        "shareable_link_signing_key": b"0" * 32,
    }
    return WebSettings(**{**required, **overrides})


def minimal_continuation_policy(**setting_overrides: Any) -> ContinuationAdmissionPolicy:
    policy = continuation_policy_from_settings(continuation_test_settings(composer_continuation_enabled=True, **setting_overrides))
    if policy is None:
        raise AssertionError("an enabled continuation setting produced no policy")
    return policy


def observed_sql_trial_policy(*, physical_seconds: float | None = None) -> ContinuationAdmissionPolicy:
    """The production policy with session-SQL retention widened for observer trials.

    Built through the same settings mapping and validators as production. The
    nominal segment grows by exactly the added first-segment chain time (three
    session-SQL actions), so the production chain-fit check still applies.
    ``physical_seconds`` also widens the session-SQL physical stage, for trials
    whose observed window spans that stage too; omitted, it stays production.
    """
    defaults = continuation_test_settings()
    production = defaults.composer_continuation_action_bounds.session_sql
    physical = production.physical if physical_seconds is None else physical_seconds
    widened = ContinuationLocalStageSettings(
        preparation=production.preparation,
        physical=physical,
        retention=OBSERVED_SQL_TRIAL_RETENTION_SECONDS,
    )
    added_chain = _FIRST_SEGMENT_SESSION_SQL_ACTIONS * (
        (OBSERVED_SQL_TRIAL_RETENTION_SECONDS - production.retention) + (physical - production.physical)
    )
    return minimal_continuation_policy(
        composer_continuation_action_bounds=ContinuationActionBoundsSettings(session_sql=widened),
        composer_continuation_segment_nominal_seconds=defaults.composer_continuation_segment_nominal_seconds + added_chain,
    )
