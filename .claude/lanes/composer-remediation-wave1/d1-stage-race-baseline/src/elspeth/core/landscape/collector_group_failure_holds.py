"""The member hold a collector group's FAILED verdict writes, and the one parser that reads it back.

``ExecutionRepository.complete_collector_failure`` completes every arrived
member's accept-time hold FAILED with one ``CollectorGroupFailure`` error, in
the verdict's own transaction, beside the group's ``collector_group_failures``
row. The hold is the only per-member witness of that verdict: the group row
names the group, and only the hold names which tokens it failed.

Two readers need it, and they must never disagree on its shape:

- resume (``BarrierRestoreReadModel.get_recorded_collector_group_failures``)
  completes a recorded verdict whose disposition a crashed process never
  finished;
- the counting authority (``terminal_transform_failures``) counts each failed
  member token once, under the reason its group row records.

So the writer's error and the parser live here together. The hold's
structured ``context`` carries the group-level cause and nothing row-derived:

- ``group_id``: the group the verdict failed (minted, never row data);
- ``failure_reason``: a :class:`CollectorGroupFailureReason` value;
- ``lost_members``: the minted member keys of lost members (a FORK branch name
  from config or an EXPAND child token id);
- ``member_disposition``: always ``scope_group_failed`` (spec §6.3).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass

from elspeth.contracts.enums import CollectorGroupFailureReason, GroupSettlementReason
from elspeth.contracts.errors import AuditIntegrityError, ExecutionError

COLLECTOR_GROUP_FAILURE_TYPE = "CollectorGroupFailure"
"""The ``ExecutionError`` type a collector group-failure hold records."""

_REASONS: frozenset[str] = frozenset(reason.value for reason in CollectorGroupFailureReason)


@dataclass(frozen=True, slots=True)
class RecordedCollectorGroupFailureHold:
    """One member's hold that records its collector group's FAILED verdict."""

    node_id: str
    group_id: str
    failure_reason: CollectorGroupFailureReason


def collector_group_failure_hold_error(
    *,
    group_id: str,
    failure_reason: CollectorGroupFailureReason,
    lost_members: Iterable[str],
) -> ExecutionError:
    """The one ``CollectorGroupFailure`` error every arrived member's hold records.

    The message names only the minted group id, the reason code and the lost
    members' minted keys. It is the same for every member of the group.
    """
    lost = sorted(lost_members)
    if lost:
        exception_text = f"Collector group {group_id!r} failed ({failure_reason.value}): lost members {lost!r}"
    else:
        exception_text = f"Collector group {group_id!r} failed ({failure_reason.value})"
    return ExecutionError(
        exception=exception_text,
        exception_type=COLLECTOR_GROUP_FAILURE_TYPE,
        phase="collector_flush",
        context={
            "group_id": group_id,
            "failure_reason": failure_reason.value,
            "lost_members": lost,
            "member_disposition": GroupSettlementReason.SCOPE_GROUP_FAILED.value,
        },
    )


def parse_collector_group_failure_hold(token_id: str, node_id: str, error_json: str | None) -> RecordedCollectorGroupFailureHold | None:
    """Parse one FAILED collector state's ``error_json``: the group verdict it records, or None for any other failure.

    Any other failure is ``None``: the flush state's own error on the opener
    (``TransformError``, ``PluginContractViolation``, or an exception the
    guard recorded) and a quarantined member of a successful flush. A hold
    whose type says ``CollectorGroupFailure`` but whose context deviates from
    :func:`collector_group_failure_hold_error` is corruption of our own audit
    data, never a shape to tolerate.

    Raises:
        AuditIntegrityError: The state has no error, the error is not an
            object with a ``type``, or a ``CollectorGroupFailure`` context
            lacks a ``group_id``, a closed-vocabulary ``failure_reason`` or
            the ``scope_group_failed`` disposition.
    """
    if error_json is None:
        raise AuditIntegrityError(f"FAILED collector hold of token {token_id!r} at {node_id!r} has no error_json")
    error = json.loads(error_json)
    if type(error) is not dict or "type" not in error:
        raise AuditIntegrityError(f"FAILED collector hold of token {token_id!r} at {node_id!r} has a malformed error_json")
    if error["type"] != COLLECTOR_GROUP_FAILURE_TYPE:
        return None
    context = error["context"] if "context" in error else None
    if (
        type(context) is not dict
        or "group_id" not in context
        or type(context["group_id"]) is not str
        or not context["group_id"]
        or "failure_reason" not in context
        or type(context["failure_reason"]) is not str
        or context["failure_reason"] not in _REASONS
        or "member_disposition" not in context
        or context["member_disposition"] != GroupSettlementReason.SCOPE_GROUP_FAILED.value
    ):
        raise AuditIntegrityError(
            f"Collector group-failure hold of token {token_id!r} at {node_id!r} does not carry the verdict's failure_reason, "
            "group_id and scope_group_failed disposition"
        )
    return RecordedCollectorGroupFailureHold(
        node_id=node_id,
        group_id=context["group_id"],
        failure_reason=CollectorGroupFailureReason(context["failure_reason"]),
    )
