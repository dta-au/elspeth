"""Exact Chroma exception and unfiltered image qualification controls."""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from scripts.cicd.check_main_image_vulnerabilities import CHROMA_METADATA, main, qualify

IMAGE = "ghcr.io/dta-au/elspeth@sha256:" + "a" * 64
TODAY = date(2026, 10, 7)
CVES = ("CVE-2026-45829", "CVE-2026-45830", "CVE-2026-45831", "CVE-2026-45833")


def _report() -> dict[str, Any]:
    return {
        "SchemaVersion": 2,
        "ArtifactType": "container_image",
        "ArtifactName": IMAGE,
        "Metadata": {"ImageConfig": {"os": "linux", "architecture": "amd64"}},
        "Results": [
            {"Target": IMAGE + " (debian 13.7)", "Class": "os-pkgs", "Type": "debian"},
            {
                "Target": "Python",
                "Class": "lang-pkgs",
                "Type": "python-pkg",
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": cve,
                        "PkgName": "chromadb",
                        "InstalledVersion": "1.5.5",
                        "PkgIdentifier": {"PURL": "pkg:pypi/chromadb@1.5.5"},
                        "PkgPath": CHROMA_METADATA,
                        "DataSource": {"ID": "ghsa"},
                        "Severity": "CRITICAL" if cve in {CVES[0], CVES[3]} else "HIGH",
                    }
                    for cve in CVES
                ],
            },
        ],
    }


def _qualify(report: Any, *, today: date = TODAY) -> dict[str, Any]:
    return qualify(report, image=IMAGE, platform="linux/amd64", today=today)


def test_accepts_exact_four_findings_without_modifying_raw_evidence() -> None:
    report = _report()
    original = copy.deepcopy(report)
    result = _qualify(report)
    assert result["qualified"] is True
    assert len(result["accepted_findings"]) == 4
    assert result["blocking_findings"] == []
    assert report == original


@pytest.mark.parametrize("registry", ["ghcr.io/dta-au", "registry.azurecr.io"])
@pytest.mark.parametrize("architecture", ["amd64", "arm64"])
def test_each_main_registry_platform_uses_same_exception(registry: str, architecture: str) -> None:
    image = registry + "/elspeth@sha256:" + "a" * 64
    report = _report()
    report["ArtifactName"] = image
    report["Metadata"]["ImageConfig"]["architecture"] = architecture
    assert qualify(report, image=image, platform=f"linux/{architecture}", today=TODAY)["qualified"] is True


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("VulnerabilityID", "CVE-2026-99999"),
        ("VulnerabilityID", "GHSA-f4j7-r4q5-qw2c"),
        ("PkgName", "libexpat1"),
        ("InstalledVersion", "1.5.9"),
        ("PkgIdentifier", {"PURL": "pkg:deb/debian/chromadb@1.5.5"}),
        ("PkgIdentifier", {}),
        ("PkgPath", "usr/lib/python3.13/site-packages/chromadb-1.5.5.dist-info/METADATA"),
        ("DataSource", {"ID": "debian"}),
        ("FixedVersion", "1.5.10"),
    ],
)
def test_change_to_any_accepted_identity_or_provenance_remains_blocking(key: str, value: Any) -> None:
    report = _report()
    report["Results"][1]["Vulnerabilities"][0][key] = value
    result = _qualify(report)
    assert result["qualified"] is False
    assert len(result["accepted_findings"]) == 3
    assert len(result["blocking_findings"]) == 1


def test_debian_and_new_chroma_findings_remain_blocking() -> None:
    report = _report()
    debian = {
        "VulnerabilityID": "CVE-2026-19445",
        "PkgName": "python3.13-minimal",
        "InstalledVersion": "3.13.5-2+deb13u5",
        "Severity": "HIGH",
    }
    report["Results"][0]["Vulnerabilities"] = [debian]
    other = copy.deepcopy(report["Results"][1]["Vulnerabilities"][0])
    other["VulnerabilityID"] = "CVE-2026-99999"
    report["Results"][1]["Vulnerabilities"].append(other)
    result = _qualify(report)
    assert result["qualified"] is False
    assert len(result["accepted_findings"]) == 4
    assert result["blocking_findings"] == ["CVE-2026-19445 python3.13-minimal==3.13.5-2+deb13u5", "CVE-2026-99999 chromadb==1.5.5"]


def test_chroma_labels_in_an_os_result_do_not_gain_exception() -> None:
    report = _report()
    report["Results"][0]["Vulnerabilities"] = report["Results"][1].pop("Vulnerabilities")
    result = _qualify(report)
    assert result["qualified"] is False
    assert len(result["blocking_findings"]) == 4


@pytest.mark.parametrize(("today", "overdue"), [(date(2026, 10, 30), False), (date(2026, 10, 31), True)])
def test_review_date_is_visible_without_inventing_a_hard_expiration(today: date, overdue: bool) -> None:
    result = _qualify(_report(), today=today)
    assert result["qualified"] is True
    assert result["chroma_review_overdue"] is overdue


def test_clean_image_does_not_require_chroma_exception() -> None:
    report = _report()
    report["Results"][1].pop("Vulnerabilities")
    assert _qualify(report, today=date(2027, 1, 1))["qualified"] is True


@pytest.mark.parametrize("field", ["SchemaVersion", "ArtifactType", "ArtifactName", "Metadata", "Results"])
def test_incomplete_report_is_rejected(field: str) -> None:
    report = _report()
    report.pop(field)
    with pytest.raises(KeyError):
        _qualify(report)


@pytest.mark.parametrize("suppressed_field", ["ExperimentalModifiedFindings", "ModifiedFindings"])
def test_wrong_platform_suppressed_findings_and_empty_results_are_rejected(suppressed_field: str) -> None:
    wrong = _report()
    wrong["Metadata"]["ImageConfig"]["architecture"] = "arm64"
    with pytest.raises(ValueError, match="platform"):
        _qualify(wrong)
    suppressed = _report()
    suppressed["Results"][0][suppressed_field] = [{"Status": "ignored"}]
    with pytest.raises(ValueError, match="suppressed"):
        _qualify(suppressed)
    empty = _report()
    empty["Results"] = []
    with pytest.raises(ValueError, match="Debian and Python"):
        _qualify(empty)


def test_gateway_and_mutable_main_image_are_rejected() -> None:
    for image in (IMAGE.replace("/elspeth@", "/elspeth-llm-gateway@"), "ghcr.io/dta-au/elspeth:latest"):
        with pytest.raises(ValueError, match="main-image immutable"):
            qualify(_report(), image=image, platform="linux/amd64", today=TODAY)


def test_cli_invalid_json_or_missing_file_returns_failure(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    arguments = ["--report", str(report), "--summary", str(tmp_path / "summary.json"), "--image", IMAGE, "--platform", "linux/amd64"]
    assert main(arguments) == 1
    report.write_text("{", encoding="utf-8")
    assert main(arguments) == 1
    report.write_text(json.dumps({"Results": []}), encoding="utf-8")
    assert main(arguments) == 1


def test_duplicate_json_members_cannot_hide_a_blocking_result(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    summary = tmp_path / "summary.json"
    arguments = ["--report", str(report), "--summary", str(summary), "--image", IMAGE, "--platform", "linux/amd64"]
    clean = json.dumps(_report())
    blocking = '{"Target":"Debian","Class":"os-pkgs","Type":"debian","Vulnerabilities":[{"VulnerabilityID":"CVE-2026-19445","PkgName":"python3.13-minimal","InstalledVersion":"3.13.5-2+deb13u5","Severity":"HIGH"}]}'
    report.write_text('{"Results":[' + blocking + "]," + clean[1:], encoding="utf-8")
    assert main(arguments) == 1
    assert not summary.exists()


def test_cli_accepts_known_findings_and_retains_raw_report(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    summary = tmp_path / "summary.json"
    raw = json.dumps(_report())
    report.write_text(raw, encoding="utf-8")
    arguments = ["--report", str(report), "--summary", str(summary), "--image", IMAGE, "--platform", "linux/amd64"]
    assert main(arguments) == 0
    assert len(json.loads(summary.read_text(encoding="utf-8"))["accepted_findings"]) == 4
    assert report.read_text(encoding="utf-8") == raw


@pytest.mark.parametrize("member", ["vulnerabilities", "Vulnerabilites", "UnknownFindings"])
def test_noncanonical_finding_members_cannot_hide_records(member: str) -> None:
    report = _report()
    report["Results"][1][member] = report["Results"][1].pop("Vulnerabilities")
    with pytest.raises(ValueError, match="unrecognised result members"):
        _qualify(report)


@pytest.mark.parametrize("fixed_version", [None, False, [], {}])
def test_malformed_fixed_version_is_rejected(fixed_version: Any) -> None:
    report = _report()
    report["Results"][1]["Vulnerabilities"][0]["FixedVersion"] = fixed_version
    with pytest.raises(ValueError, match="fixed version"):
        _qualify(report)
