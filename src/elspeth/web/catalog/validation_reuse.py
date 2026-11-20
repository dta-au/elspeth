"""Shared pieces of within-round validation reuse.

A Composer round (one compose call) owns one ``PolicyCatalogView`` and one
runtime-preflight cache. Both may hand back a verdict they already computed
for the same state object, and both must stop doing so once a mutation-kind
tool call has ended: such a call can change a secret or a blob that no other
key component sees. ``MutationEpoch`` is the counter they share for that, and
``VALIDATION_REUSE_COUNTER`` makes every reuse visible in telemetry, because
the existing ``composer.runtime_preflight.total`` counts computes only.

Both layers are switched by ``WebSettings.composer_validation_reuse_enabled``.
"""

from __future__ import annotations

from typing import Final, Literal

from opentelemetry import metrics

ValidationReuseLayer = Literal["runtime_preflight", "policy_view"]

# Why a runtime preflight verdict computed for a blob-bearing state, with reuse
# switched on, was not stored for reuse. One count per such compute. Without
# it, reuse that silently stops (for example an evidence read that fails on
# every call) looks the same as a round that never repeats a preflight.
PreflightReuseDeclineReason = Literal[
    "no_blob_service",
    "no_operation_context",
    "blob_ref_not_uuid",
    "secret_ref_sites",
    "evidence_read_failed",
    "evidence_changed",
    "mutation_during_compute",
    "joined_in_flight",
    "compute_failed",
]

VALIDATION_REUSE_COUNTER: Final = metrics.get_meter(__name__).create_counter(
    "composer.runtime_preflight.reused",
    description="Validation verdicts handed back without recomputing, by layer",
)

PREFLIGHT_REUSE_DECLINED_COUNTER: Final = metrics.get_meter(__name__).create_counter(
    "composer.runtime_preflight.reuse_declined",
    description="Blob-bearing runtime preflight verdicts computed with reuse on but not stored for reuse, by reason",
)


class MutationEpoch:
    """Count of mutation-kind tool calls that have ended within one round.

    ``execute_tool`` advances it in a ``finally`` for every name in
    ``_ALL_MUTATION_TOOL_NAMES``, whether the call succeeded, was refused or
    raised. A stored verdict records the value it was computed under and is
    reused only while the value is unchanged.
    """

    __slots__ = ("_value",)

    def __init__(self) -> None:
        self._value = 0

    @property
    def value(self) -> int:
        return self._value

    def advance(self) -> None:
        self._value += 1
