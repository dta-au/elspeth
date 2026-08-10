"""Honesty-gate tests for the raising Tier-3 boundary function in redaction.py.

``@trust_boundary`` (non-``non_raising``) declarations require a ``test_ref``
that the ``trust_boundary.tests`` elspeth-lints rule can mechanically verify:
a raising assertion in the named test's own body that directly calls the
decorated function through its declared ``source_param``. This file holds
that direct call — the pydantic-model integration tests elsewhere exercise
``_StrictTimeoutSeconds`` indirectly and do not satisfy the honesty gate.
"""

from __future__ import annotations

import pytest

from elspeth.web.composer.redaction import _reject_coerced_timeout_seconds


def test_reject_coerced_timeout_seconds_rejects_bool() -> None:
    with pytest.raises(ValueError):
        _reject_coerced_timeout_seconds(True)


def test_reject_coerced_timeout_seconds_rejects_numeric_string() -> None:
    with pytest.raises(ValueError):
        _reject_coerced_timeout_seconds("30")


def test_reject_coerced_timeout_seconds_passes_none_through() -> None:
    assert _reject_coerced_timeout_seconds(None) is None


def test_reject_coerced_timeout_seconds_passes_number_through() -> None:
    assert _reject_coerced_timeout_seconds(30) == 30
    assert _reject_coerced_timeout_seconds(30.5) == 30.5
