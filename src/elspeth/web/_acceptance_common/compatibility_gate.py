"""The runbook's rollback-refusal jq gate, as code.

``docs/runbooks/aws-ecs-deployment.md`` fences the rollback decision with one
``jq -e`` filter over the validated compatibility receipt::

    .backward_compatible == false
    and .rollback_permitted == false
    and .schema_facts.previous.landscape_epoch == <rollback baseline>
    and .schema_facts.candidate.landscape_epoch == <live epoch>

That filter tests exactly four clauses and nothing else. This module is the
same predicate with the epoch literals read from the one shared derivation
(``schema_facts``), so the Container Apps runbook calls a command instead of
carrying a second copy of the literals, and a parity test feeds one corpus
through both the jq filter and this predicate and demands identical verdicts.

The predicate is deliberately **jq-faithful**, not stricter: a clause over a
missing path fails exactly as ``.x == literal`` fails on ``null`` in jq, a
``0`` is not ``false`` and a ``true`` is not ``1``. Stricter validation of the
record's other fields belongs to the provider's ``compatibility-record-validate``
step, which runs before the gate, never inside it.

Scenario A has no previous release: the runbook's Scenario B literal cannot
apply, so the previous clause becomes ``.schema_facts.previous == null`` — the
shape the shared derivation produces for that scenario.

Every value the gate compares is a boolean or an integer epoch. No datetime
enters the predicate, so it is indifferent to the database session timezone by
construction.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH

from .errors import AcceptanceCheckError, AcceptanceInputError
from .schema_facts import _ROLLBACK_BASELINE_LANDSCAPE_EPOCH, _expected_schema_facts
from .secure_documents import _read_protected_document

GATE_SCENARIO_IDS: Final = frozenset({"A", "B"})

GATE_CLAUSES: Final = (
    "backward_compatible",
    "rollback_permitted",
    "previous_landscape_epoch",
    "candidate_landscape_epoch",
)
"""The four clauses, in the runbook filter's order; a verdict names the failed ones."""

_CLAUSE_PATHS: Final[Mapping[str, tuple[str, ...]]] = {
    "backward_compatible": ("backward_compatible",),
    "rollback_permitted": ("rollback_permitted",),
    "previous": ("schema_facts", "previous"),
    "previous_landscape_epoch": ("schema_facts", "previous", "landscape_epoch"),
    "candidate_landscape_epoch": ("schema_facts", "candidate", "landscape_epoch"),
}


@dataclass(frozen=True)
class CompatibilityGateVerdict:
    """The gate's answer: ``passed`` iff every clause held; ``failed_clauses`` names the rest."""

    passed: bool
    failed_clauses: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.passed != (not self.failed_clauses):
            raise ValueError("a verdict passes exactly when no clause failed")
        if any(clause not in GATE_CLAUSES for clause in self.failed_clauses):
            raise ValueError("failed_clauses must name gate clauses")


@trust_boundary(
    tier=3,
    source="a decoded compatibility receipt or record document read from the operator's receipt store, on any provider",
    source_param="record",
    suppresses=("R1", "R5"),
    invariant=(
        "never raises on the record's content and returns a failed verdict naming every clause that did not hold, "
        "with jq semantics: a missing path compares as null, only the boolean false satisfies '== false', and only a "
        "non-boolean number equal to the expected epoch satisfies the epoch clauses"
    ),
    non_raising=True,
)
def compatibility_record_gate(record: object, *, scenario_id: str) -> CompatibilityGateVerdict:
    """Evaluate the rollback-refusal predicate over one decoded compatibility record.

    ``record`` is whatever ``json.loads`` produced — the gate is jq-faithful over
    any shape and never raises on the record's content. ``scenario_id`` selects
    the previous-release clause: ``"B"`` demands the rollback baseline's
    landscape epoch, ``"A"`` demands ``null``.
    """

    if scenario_id not in GATE_SCENARIO_IDS:
        raise AcceptanceInputError("compatibility-record-gate requires scenario_id A or B")
    previous_is_null = _expected_schema_facts(scenario_id)["previous"] is None

    # ``.a.b.c`` with jq semantics: any missing step yields null.
    values: dict[str, object] = {}
    for clause, keys in _CLAUSE_PATHS.items():
        node = record
        for key in keys:
            if not isinstance(node, Mapping) or key not in node:
                node = None
                break
            node = node[key]
        values[clause] = node

    # jq ``value == false``: only the boolean itself compares equal.
    backward_compatible = values["backward_compatible"]
    rollback_permitted = values["rollback_permitted"]
    # jq ``value == <number>``: booleans are not numbers, floats compare by value.
    previous_epoch = values["previous_landscape_epoch"]
    candidate_epoch = values["candidate_landscape_epoch"]
    held = {
        "backward_compatible": isinstance(backward_compatible, bool) and backward_compatible is False,
        "rollback_permitted": isinstance(rollback_permitted, bool) and rollback_permitted is False,
        "previous_landscape_epoch": (
            values["previous"] is None
            if previous_is_null
            else (
                not isinstance(previous_epoch, bool)
                and isinstance(previous_epoch, (int, float))
                and previous_epoch == _ROLLBACK_BASELINE_LANDSCAPE_EPOCH
            )
        ),
        "candidate_landscape_epoch": (
            not isinstance(candidate_epoch, bool) and isinstance(candidate_epoch, (int, float)) and candidate_epoch == SQLITE_SCHEMA_EPOCH
        ),
    }
    failed = tuple(clause for clause in GATE_CLAUSES if not held[clause])
    return CompatibilityGateVerdict(passed=not failed, failed_clauses=failed)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="compatibility-record-gate", add_help=True)
    parser.add_argument("--record", required=True, help="validated compatibility receipt (owner-only JSON file)")
    parser.add_argument("--scenario-id", required=True, choices=sorted(GATE_SCENARIO_IDS))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """``compatibility-record-gate --record <file> --scenario-id A|B``: exit 0 iff the gate passes.

    Mirrors ``jq -e``: the verdict is the exit code, the failed clauses are
    printed as one JSON object on stdout, and nothing from the record is echoed.
    """

    args = build_parser().parse_args(argv)
    try:
        record = _read_protected_document(Path(args.record), check="compatibility_record_file")
    except AcceptanceCheckError as exc:
        json.dump({"gate": "compatibility-record-gate", "passed": False, "error": exc.check}, sys.stdout, sort_keys=True)
        sys.stdout.write("\n")
        return 2
    verdict = compatibility_record_gate(record, scenario_id=args.scenario_id)
    json.dump(
        {
            "gate": "compatibility-record-gate",
            "scenario_id": args.scenario_id,
            "passed": verdict.passed,
            "failed_clauses": list(verdict.failed_clauses),
        },
        sys.stdout,
        sort_keys=True,
    )
    sys.stdout.write("\n")
    return 0 if verdict.passed else 1


if __name__ == "__main__":
    sys.exit(main())
