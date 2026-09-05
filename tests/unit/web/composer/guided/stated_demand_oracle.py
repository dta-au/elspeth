"""Brute-force oracle for the stated-constraint demand (elspeth-3d392c04ca,
elspeth-b24ec0945f).

Written INDEPENDENTLY of the demand derivation in ``deferred_intents``: it
enumerates the closed candidate space from a message's own tokens and asks
the PRODUCTION ``validate_deferred_intent_action`` whether any stated action
is accepted. Both 2026-08-26 falsification lanes established that this token
space is complete for the grounding regex, so "zero routing acceptances"
means the routing acceptance set is empty, not unsampled.
"""

from __future__ import annotations

import re

from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.guided.deferred_intents import (
    _MESSAGE_OPERATOR_PATTERN,
    DeferredIntentAccepted,
    DeferredIntentAction,
    validate_deferred_intent_action,
)
from elspeth.web.composer.guided.errors import InvariantError
from elspeth.web.composer.guided.stage_subjects import (
    PluginSubject,
    StatedGateRoutingConstraint,
    StatedPredicateConstraint,
)
from elspeth.web.composer.guided.state_machine import GuidedSession

CSV_SOURCE_SUBJECT = PluginSubject(
    kind="plugin",
    subject_id="33333333-3333-4333-8333-333333333333",
    plugin_kind="source",
    plugin_name="csv",
)
_WORD = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*")
# Candidate route targets: the token a destination phrase names. Derived from
# the destination grammar's ``(to|into|in) [a|the] <target>`` head so the
# oracle stays tractable on long corpus prompts; the grammar itself is what
# ``validate_deferred_intent_action`` then holds each candidate to.
_DESTINATION_HEAD = re.compile(r"\b(?:to|into|in)\s+(?:(?:a|the)\s+)?([A-Za-z0-9_-]+)", re.IGNORECASE)


def stated_acceptances_from_message_tokens(message: str, catalog: PolicyCatalogView) -> tuple[int, int]:
    """(routing_accepted, predicate_accepted) over every candidate stated action
    the message's own tokens can name, judged by the production validator."""

    tokens = [(match.group(), match.start(), match.end()) for match in _WORD.finditer(message)]
    routing_accepted = 0
    predicate_accepted = 0
    seen: set[tuple[object, ...]] = set()

    def accepted(action: DeferredIntentAction) -> bool:
        return (
            type(
                validate_deferred_intent_action(
                    action,
                    receiving_stage="source",
                    catalog=catalog,
                    guided=GuidedSession.initial(),
                    originating_message_content=message,
                )
            )
            is DeferredIntentAccepted
        )

    for operator, pattern in _MESSAGE_OPERATOR_PATTERN.items():
        for occurrence in re.finditer(pattern, message, re.IGNORECASE):
            after = [token for token in tokens if token[1] >= occurrence.end()]
            if not after:
                continue
            raw_value = after[0][0]
            value: object = int(raw_value) if raw_value.isdigit() else raw_value
            columns = {token[0] for token in tokens if token[2] <= occurrence.start()}
            targets = set(_DESTINATION_HEAD.findall(message[occurrence.end() :]))
            for column in columns:
                predicate_key = ("predicate", column, operator, value)
                if predicate_key not in seen:
                    seen.add(predicate_key)
                    predicate = DeferredIntentAction(
                        target_stage="topology",
                        catalog_kind=None,
                        catalog_name=None,
                        redacted_summary="oracle",
                        constraints=(
                            StatedPredicateConstraint(
                                kind="stated_predicate", subject=CSV_SOURCE_SUBJECT, column=column, operator=operator, value=value
                            ),
                        ),
                    )
                    if accepted(predicate):
                        predicate_accepted += 1
                for true_target in targets:
                    for false_target in targets - {true_target}:
                        routing_key = ("routing", column, operator, value, true_target, false_target)
                        if routing_key in seen:
                            continue
                        seen.add(routing_key)
                        try:
                            constraint = StatedGateRoutingConstraint(
                                kind="stated_gate_routing",
                                subject=CSV_SOURCE_SUBJECT,
                                column=column,
                                operator=operator,
                                value=value,
                                true_target=true_target,
                                false_target=false_target,
                            )
                        except (ValueError, InvariantError):
                            # Not a statable output name (e.g. "JSON"): the
                            # planner cannot emit it, so it is not a candidate.
                            continue
                        routing = DeferredIntentAction(
                            target_stage="topology",
                            catalog_kind=None,
                            catalog_name=None,
                            redacted_summary="oracle",
                            constraints=(constraint,),
                        )
                        if accepted(routing):
                            routing_accepted += 1
    return routing_accepted, predicate_accepted


def assert_demand_is_satisfiable(message: str, demand: str | None, catalog: PolicyCatalogView) -> tuple[int, int]:
    """The property elspeth-3d392c04ca asked to pin: demand raised ⇒ some stated
    action is accepted; no demand ⇒ no routing action grounds."""

    routing_accepted, predicate_accepted = stated_acceptances_from_message_tokens(message, catalog)
    if demand == "routing":
        assert routing_accepted > 0, "routing demanded but no routing action from the message's own tokens is accepted"
    elif demand == "predicate":
        assert predicate_accepted > 0 or routing_accepted > 0, "predicate demanded but nothing stated is accepted"
    else:
        assert routing_accepted == 0, "no demand, yet a routing action grounds — the demand was suppressed wrongly"
    return routing_accepted, predicate_accepted
