"""Owned advisor decisions, independent of validation and persistence."""

from dataclasses import dataclass
from enum import StrEnum


class AdvisorBlockCause(StrEnum):
    GRAPH_REJECTED = "graph_rejected"
    UNAVAILABLE = "unavailable"
    MALFORMED = "malformed"
    MESSAGE_REJECTED = "message_rejected"


@dataclass(frozen=True, slots=True)
class AdvisorSignoffGateFact:
    """A blocked review, bound to the graph actually reviewed."""

    detail: str
    suggestion: str | None
    for_graph: str
    note: str | None
    cause: AdvisorBlockCause


@dataclass(frozen=True, slots=True)
class AdvisorGatePassed:
    """An actual CLEAN END checkpoint for this graph."""

    for_graph: str


@dataclass(frozen=True, slots=True)
class AdvisorGateBlocked:
    fact: AdvisorSignoffGateFact


type AdvisorGateDecision = AdvisorGatePassed | AdvisorGateBlocked
