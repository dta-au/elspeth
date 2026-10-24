"""The admitted continuation policy that continuation owner tests run against.

It is the production policy, built through the same settings mapping as
``web/app.py``, never a second set of literals (design §4.5, U-45 §13.5).
A test whose subject is a deadline builds its own explicit small policy.
"""

from __future__ import annotations

from typing import Any

from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicy
from elspeth.web.composer.continuation_policy_settings import continuation_policy_from_settings
from elspeth.web.config import WebSettings


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
