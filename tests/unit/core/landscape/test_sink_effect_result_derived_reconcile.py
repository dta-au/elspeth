"""Closed carrier proofs for result-derived sink reconciliation."""

from __future__ import annotations

import pytest

from elspeth.contracts.results import ArtifactDescriptor
from elspeth.contracts.sink_effects import (
    SinkEffectAttemptAction,
    SinkEffectCommitResult,
    SinkEffectInspection,
    SinkEffectInspectionMode,
    SinkEffectReconcileResult,
)
from elspeth.core.canonical import canonical_json
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.execution.sink_effect_attempt_results import (
    decode_sink_effect_returned_result,
    encode_sink_effect_returned_result,
)

_DESCRIPTOR = ArtifactDescriptor(
    artifact_type="database",
    path_or_uri="database-result:sha256:" + "a" * 64,
    content_hash="b" * 64,
    size_bytes=2,
    metadata={"table": "output", "row_count": 0},
)

_INSPECTION = SinkEffectInspection(
    mode=SinkEffectInspectionMode.INSPECTED,
    reference="output.csv",
    evidence={"marker": "inspect"},
)
_COMMIT = SinkEffectCommitResult(
    descriptor=_DESCRIPTOR,
    evidence={"marker": "commit"},
    accepted_ordinals=(0,),
    diverted_ordinals=(),
)
_RECONCILE = SinkEffectReconcileResult.applied(
    _DESCRIPTOR,
    evidence={"marker": "reconcile"},
    accepted_ordinals=(0,),
    diverted_ordinals=(),
)


def test_reconcile_carrier_round_trips_exact_result_partition() -> None:
    result = SinkEffectReconcileResult.applied(
        _DESCRIPTOR,
        evidence={"marker": "exact"},
        accepted_ordinals=(0, 2),
        diverted_ordinals=(1,),
    )

    encoded = encode_sink_effect_returned_result(result)
    decoded = decode_sink_effect_returned_result(SinkEffectAttemptAction.RECONCILE, canonical_json(encoded))

    assert decoded == result


def test_reconcile_carrier_rejects_partial_or_overlapping_partition() -> None:
    with pytest.raises(ValueError, match="both be present"):
        SinkEffectReconcileResult.applied(
            _DESCRIPTOR,
            evidence={},
            accepted_ordinals=(0,),
        )
    with pytest.raises(ValueError, match="must not overlap"):
        SinkEffectReconcileResult.applied(
            _DESCRIPTOR,
            evidence={},
            accepted_ordinals=(0,),
            diverted_ordinals=(0,),
        )


@pytest.mark.parametrize(
    ("action", "stored"),
    [
        (SinkEffectAttemptAction.INSPECT, _COMMIT),
        (SinkEffectAttemptAction.INSPECT, _RECONCILE),
        (SinkEffectAttemptAction.COMMIT, _INSPECTION),
        (SinkEffectAttemptAction.COMMIT, _RECONCILE),
        (SinkEffectAttemptAction.RECONCILE, _INSPECTION),
        (SinkEffectAttemptAction.RECONCILE, _COMMIT),
    ],
    ids=lambda v: v.value if isinstance(v, SinkEffectAttemptAction) else type(v).__name__,
)
def test_cross_action_envelope_is_refused_not_decoded_as_the_wrong_member(
    action: SinkEffectAttemptAction,
    stored: SinkEffectInspection | SinkEffectCommitResult | SinkEffectReconcileResult,
) -> None:
    """`_returned_attempt`'s @overload narrowing relies on this refusal: a durable
    row whose evidence carries another action's envelope must raise, never
    return a different union member than the action requested.
    """
    encoded = canonical_json(encode_sink_effect_returned_result(stored))

    with pytest.raises(LandscapeRecordError, match="envelope is divergent"):
        decode_sink_effect_returned_result(action, encoded)


@pytest.mark.parametrize(
    ("action", "stored"),
    [
        (SinkEffectAttemptAction.INSPECT, _INSPECTION),
        (SinkEffectAttemptAction.COMMIT, _COMMIT),
        (SinkEffectAttemptAction.RECONCILE, _RECONCILE),
    ],
    ids=lambda v: v.value if isinstance(v, SinkEffectAttemptAction) else type(v).__name__,
)
def test_matching_action_envelope_round_trips(
    action: SinkEffectAttemptAction,
    stored: SinkEffectInspection | SinkEffectCommitResult | SinkEffectReconcileResult,
) -> None:
    encoded = canonical_json(encode_sink_effect_returned_result(stored))

    assert decode_sink_effect_returned_result(action, encoded) == stored
