"""Metadata for the ``masquerade.attribute-probes`` gate."""

from __future__ import annotations

from elspeth_lints.core.protocols import Category, RuleMetadata, RuleScope, Severity

RULE_ID = "masquerade.attribute-probes"

#: Repository-relative roots this gate covers, matching the four buckets
#: measured in the corpus of record (scratchpad/inv3.json): production,
#: tests, scripts, and elspeth-lints itself. Order is scan order, not
#: significant otherwise.
SCAN_SUBDIRS: tuple[str, ...] = ("src/elspeth", "tests", "scripts", "elspeth-lints/src")

RULE_METADATA = RuleMetadata(
    id=RULE_ID,
    name="No new unadjudicated attribute-masquerading probes",
    description=(
        "getattr/hasattr/inspect.getattr_static call sites and __getattr__ definitions "
        "outside the recognised external-boundary, test-assertion, and PEP 562 amnesties "
        "must be adjudicated into config/cicd/masquerade_baseline.yaml; a new site of this "
        "kind cannot land without either matching a permanent amnesty or a recorded "
        "classification. See docs/architecture/adr/032-validate-by-trust-domain.md."
    ),
    severity=Severity.ERROR,
    category=Category.TRUST_TIER,
    cwe=("CWE-843", "CWE-20"),
    scope=RuleScope.WHOLE_REPO,
    path_filter=r"^(src/elspeth/|tests/|scripts/|elspeth-lints/src/).*\.py$",
    examples_violation_count=5,
    examples_clean_count=4,
)
