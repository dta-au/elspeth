"""CANDIDATE — maintenance-dimension proof node (elspeth-efb47cb5fd, T1-a).

One node per catalog ``(leg, case)`` whose ``maintenance`` cell is required on
the ``sqlite-wal-single-process-leader`` profile. The proof subject is NOT the
leg's runtime behavior; it is the leg's *verification record*, read from the
current published assessment (the maintained pointer in ``proof-matrix.md``):

* every evidence locator the record binds to this ``(leg, case, profile)`` is
  still collected — exactly, with no drift or deselection — under the local
  lane's marker expression by a real, non-executing pytest collection; and
* every unresolved cell of this ``(leg, case, profile)`` and, when the leg is
  unresolved, the leg itself, names a Filigree owner issue.

What this node does NOT establish, by construction: that a bound locator still
*passes* (the lane's evidence run and its reporter own that), that a skip/xfail
mark was added (``--collect-only`` cannot see marks), or that the named owner
issue is still *open* in Filigree (the tracker is not in the repository; see the
ruling draft ``docs/plans/2026-08-17-state-engine-maintenance-dimension-ruling.md``).

This file is a review artifact for the ruling, not landed evidence.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from scripts.state_engine_assessment_lib.selectors import _collect_pytest_node_ids

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
STATE_ENGINE_DOCS = REPOSITORY_ROOT / "docs/architecture/state_engine"
CATALOG_PATH = STATE_ENGINE_DOCS / "proof-catalog/v3/catalog.json"
PROOF_MATRIX_PATH = STATE_ENGINE_DOCS / "proof-matrix.md"

PROFILE_CASE = "sqlite-wal-single-process-leader"
LANE_MARKER_EXPRESSION = "not live_provider"
UNRESOLVED_STATUSES = frozenset({"partial", "fail", "unknown"})
UNRESOLVED_VERDICTS = frozenset({"gap", "unknown"})
FILIGREE_ISSUE_ID = re.compile(r"^elspeth-[0-9a-f]{10}$")
CURRENT_ASSESSMENT_POINTER = re.compile(r"\(assessments/(\d{4}-\d{2}-\d{2}-\d{4})/assessment\.json\)")


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict), path
    return value


def _current_assessment_id() -> str:
    """The maintained pointer: proof-matrix.md links exactly one dated manifest."""
    matches = CURRENT_ASSESSMENT_POINTER.findall(PROOF_MATRIX_PATH.read_text(encoding="utf-8"))
    assert len(matches) == 1, f"proof-matrix.md must name exactly one current assessment manifest, found {matches}"
    return matches[0]


def _maintenance_subjects() -> list[tuple[str, str]]:
    catalog = _load_json(CATALOG_PATH)
    subjects: list[tuple[str, str]] = []
    for leg in catalog["legs"]:
        for case in leg["required_cases"]:
            cell = case["cell_applicability"][PROFILE_CASE]["maintenance"]
            if cell["status"] == "required":
                subjects.append((leg["id"], case["case_id"]))
    return subjects


SUBJECTS = _maintenance_subjects()


@pytest.fixture(scope="module")
def current_assessment() -> dict[str, Any]:
    assessment_id = _current_assessment_id()
    document = _load_json(STATE_ENGINE_DOCS / "assessments" / assessment_id / "assessment.json")
    assert document["assessment_id"] == assessment_id
    return document


def _bound_locators(assessment: dict[str, Any], leg_id: str, case_id: str) -> list[str]:
    locators: set[str] = set()
    for record in assessment["evidence"]:
        if record["kind"] != "pytest":
            continue
        for coverage in record["coverage"]:
            if (coverage["leg_id"], coverage["case_id"], coverage["profile_case"]) == (leg_id, case_id, PROFILE_CASE):
                locators.update(coverage["node_ids"])
    return sorted(locators)


def _leg_record(assessment: dict[str, Any], leg_id: str) -> dict[str, Any]:
    matches = [leg for leg in assessment["legs"] if leg["id"] == leg_id]
    assert len(matches) == 1, f"assessment must record {leg_id} exactly once"
    return matches[0]


@pytest.mark.parametrize(("leg_id", "case_id"), SUBJECTS, ids=[f"{leg}::{case}" for leg, case in SUBJECTS])
def test_leg_case_locators_remain_collected_and_gaps_remain_owned(
    leg_id: str,
    case_id: str,
    current_assessment: dict[str, Any],
) -> None:
    # (a) Exact evidence locators remain collected in the maintained selection.
    #     A renamed/deleted bound test makes the collector exit non-zero; a
    #     locator that falls out of the lane marker collects to nothing. Both
    #     fail here. Nothing is executed.
    locators = _bound_locators(current_assessment, leg_id, case_id)
    if locators:
        collected = _collect_pytest_node_ids(REPOSITORY_ROOT, LANE_MARKER_EXPRESSION, locators)
        assert collected == locators, (
            f"{leg_id}/{case_id}/{PROFILE_CASE}: bound locators drifted out of the maintained selection\n"
            f"  bound:     {locators}\n  collected: {collected}"
        )

    # (b) Coherent actionable gap ownership. Every unresolved cell of this
    #     subject, and the leg itself when its verdict is unresolved, names a
    #     Filigree issue. (Openness of that issue is NOT checked here — see the
    #     module docstring.)
    leg = _leg_record(current_assessment, leg_id)
    if leg["derived_verdict"] in UNRESOLVED_VERDICTS:
        owner = leg["owner_issue"]
        assert isinstance(owner, str) and FILIGREE_ISSUE_ID.match(owner), (
            f"{leg_id}: unresolved leg must name a Filigree owner issue, got {owner!r}"
        )
    for override in leg["overrides"]:
        if override["case"] != case_id or override["profile_case"] != PROFILE_CASE:
            continue
        if override["status"] not in UNRESOLVED_STATUSES:
            continue
        owner = override.get("owner_issue")
        assert isinstance(owner, str) and FILIGREE_ISSUE_ID.match(owner), (
            f"{leg_id}/{case_id}/{PROFILE_CASE}/{override['dimension']}: unresolved cell must name a Filigree owner issue, got {owner!r}"
        )
