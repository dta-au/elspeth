"""Qualify a raw main-image Trivy report with the temporary Chroma exception.

The scanner must retain HIGH/CRITICAL findings, including unfixed and suppressed
records. This gate accepts only the documented four Chroma identities on the
locked package version. It never edits the report or applies to the gateway.
See docs/security-assurance/10-vulnerability-and-supply-chain.md section 3.4
and https://github.com/dta-au/elspeth/issues/280.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

CHROMA_CVES = frozenset({"CVE-2026-45829", "CVE-2026-45830", "CVE-2026-45831", "CVE-2026-45833"})
CHROMA_REVIEW_BY = date(2026, 10, 30)
CHROMA_METADATA = "opt/venv/lib/python3.13/site-packages/chromadb-1.5.5.dist-info/METADATA"
MAIN_IMAGE = re.compile(r"^[^\s@]+/elspeth@sha256:[0-9a-f]{64}$")
RESULT_MEMBERS = frozenset(
    {
        "Target",
        "Class",
        "Type",
        "Packages",
        "Vulnerabilities",
        "MisconfSummary",
        "Misconfigurations",
        "Secrets",
        "Licenses",
        "CustomResources",
        "ExperimentalModifiedFindings",
        "ModifiedFindings",
    }
)


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _unique_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, member in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON member: {key}")
        value[key] = member
    return value


def qualify(report: Any, *, image: str, platform: str, today: date) -> dict[str, Any]:
    """Reject mismatched/incomplete evidence and every unaccepted finding."""
    if MAIN_IMAGE.fullmatch(image) is None or platform not in {"linux/amd64", "linux/arm64"}:
        raise ValueError("expected a main-image immutable digest and supported platform")
    root = _mapping(report, "report")
    if root["SchemaVersion"] != 2 or root["ArtifactType"] != "container_image" or root["ArtifactName"] != image:
        raise ValueError("report does not identify the expected container digest")
    metadata = _mapping(root["Metadata"], "metadata")
    config = _mapping(metadata["ImageConfig"], "image config")
    if f"{config['os']}/{config['architecture']}" != platform:
        raise ValueError("report platform does not match the scanned platform")
    results = _list(root["Results"], "results")
    accepted: list[str] = []
    blocking: list[str] = []
    result_types: set[tuple[str, str]] = set()
    for raw_result in results:
        result = _mapping(raw_result, "result")
        unknown = result.keys() - RESULT_MEMBERS
        if unknown:
            raise ValueError(f"unrecognised result members: {sorted(unknown)}")
        result_types.add((result["Class"], result["Type"]))
        if result.get("ExperimentalModifiedFindings") or result.get("ModifiedFindings"):
            raise ValueError("raw evidence contains suppressed findings; scanner suppression is forbidden")
        for raw_finding in _list(result.get("Vulnerabilities", []), "vulnerabilities"):
            finding = _mapping(raw_finding, "vulnerability")
            identity, package, version = (finding[key] for key in ("VulnerabilityID", "PkgName", "InstalledVersion"))
            if not all(isinstance(value, str) and value for value in (identity, package, version)):
                raise ValueError("finding identity, package and version must be nonempty strings")
            if finding["Severity"] not in {"HIGH", "CRITICAL"}:
                raise ValueError("report must contain only the requested HIGH/CRITICAL findings")
            identifier = _mapping(finding.get("PkgIdentifier", {}), "package identifier")
            source = _mapping(finding.get("DataSource", {}), "advisory source")
            fixed_version = finding.get("FixedVersion", "")
            if not isinstance(fixed_version, str):
                raise ValueError("fixed version must be a string when present")
            known_chroma = (
                (result["Class"], result["Type"]) == ("lang-pkgs", "python-pkg")
                and result["Target"] == "Python"
                and identity in CHROMA_CVES
                and package == "chromadb"
                and version == "1.5.5"
                and identifier.get("PURL") == "pkg:pypi/chromadb@1.5.5"
                and finding.get("PkgPath") == CHROMA_METADATA
                and source.get("ID") == "ghsa"
                and fixed_version == ""
            )
            label = f"{identity} {package}=={version}"
            if known_chroma:
                accepted.append(label)
            else:
                blocking.append(label)
    if not {("os-pkgs", "debian"), ("lang-pkgs", "python-pkg")} <= result_types:
        raise ValueError("raw evidence must include Debian and Python package results")
    return {
        "image": image,
        "platform": platform,
        "evaluated_on": today.isoformat(),
        "chroma_review_by": CHROMA_REVIEW_BY.isoformat(),
        "chroma_review_overdue": bool(accepted) and today > CHROMA_REVIEW_BY,
        "follow_up": "https://github.com/dta-au/elspeth/issues/280",
        "accepted_findings": accepted,
        "blocking_findings": blocking,
        "qualified": not blocking,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--platform", choices=("linux/amd64", "linux/arm64"), required=True)
    args = parser.parse_args(argv)
    try:
        result = qualify(
            json.loads(args.report.read_text(encoding="utf-8"), object_pairs_hook=_unique_members),
            image=args.image,
            platform=args.platform,
            today=datetime.now(UTC).date(),
        )
        args.summary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Main image qualification failed: {exc}", file=sys.stderr)
        return 1
    print(f"Accepted Chroma findings: {len(result['accepted_findings'])}; blocking findings: {len(result['blocking_findings'])}")
    if result["chroma_review_overdue"]:
        print("Chroma exception review is overdue; maintainers must review the advisories and issue #280.", file=sys.stderr)
    for finding in result["blocking_findings"]:
        print(f"BLOCKING: {finding}")
    return 0 if result["qualified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
