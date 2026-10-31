"""Release-facing contracts for the standalone ELSPETH project website."""

from __future__ import annotations

import tomllib
from datetime import date
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH
from elspeth.web.sessions.models import SESSION_SCHEMA_EPOCH

ROOT = Path(__file__).resolve().parents[3]
CURRENT_VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
WEBSITE = ROOT / "website"
PAGES = {
    "index.html": "index.html",
    "authoring.html": "authoring.html",
    "assurance.html": "assurance.html",
    "use-cases.html": "use-cases.html",
    "get-started.html": "get-started.html",
}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _soup(name: str) -> BeautifulSoup:
    return BeautifulSoup(_text(WEBSITE / name), "html.parser")


def test_pages_upload_action_is_recursively_sha_pinned() -> None:
    workflow = _text(ROOT / ".github/workflows/pages.yaml")

    assert "actions/upload-pages-artifact@fc324d3547104276b827a68afc52ff2a11cc49c9  # v5.0.0" in workflow
    assert "actions/upload-pages-artifact@56afc609e74202658d3ffba0e8f6dda462b719fa" not in workflow


def test_changelog_describes_release_boundaries_precisely() -> None:
    changelog = _text(ROOT / "CHANGELOG.md")
    release = changelog.split("## 0.7.0", maxsplit=1)[0]

    assert "before external publication" in release
    assert "before I/O" not in release
    assert "shared proposal and validation contract" in release
    assert "elspeth export-resume <run-id> --execute" in release
    assert "sink-effect-v1" not in release


def test_changelog_preserves_historical_epochs_and_documents_current_compatibility() -> None:
    changelog = _text(ROOT / "CHANGELOG.md")
    current_heading = f"\n## {CURRENT_VERSION} - "
    assert changelog.count(current_heading) == 1
    current_release = changelog.split(current_heading, maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    historical_089_heading = "\n## 0.8.9 - 2026-10-21\n"
    assert changelog.count(historical_089_heading) == 1
    release_089 = changelog.split(historical_089_heading, maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    release_086 = changelog.split("\n## 0.8.6 - ", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    release_085 = changelog.split("\n## 0.8.5 - ", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    release_084 = changelog.split("\n## 0.8.4 - ", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    release_082 = changelog.split("\n## 0.8.2 - ", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    release_081 = changelog.split("\n## 0.8.1 - ", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    release_080 = changelog.split("\n## 0.8.0 - ", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    release_071 = changelog.split("\n## 0.7.1 - ", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]

    assert f"Session epoch advances from 78 to {SESSION_SCHEMA_EPOCH}" in current_release
    assert f"Landscape epoch {SQLITE_SCHEMA_EPOCH} remains unchanged" in current_release
    current_text = " ".join(current_release.split())
    assert "An existing epoch-78 Sessions store is refused, not migrated" in current_text
    assert "fresh Sessions store" in current_text
    assert "leave the epoch-78 database in place" in current_text
    assert "Automatic image publication from main remains held" in current_text
    assert "Verify any release tag and container digest separately" in current_text
    assert "this entry does not prove either has been published" in current_text

    assert "Session epoch advances from 77 to 78" in release_089
    assert "Landscape advances from epoch 49 to 50" in release_089
    assert "immutable graph snapshots" in release_089
    assert "both distinct epoch-76" in release_089
    assert "cannot share session stores or replicas" in release_089
    assert "Preserve epoch-75 stores" in release_089
    assert "epoch-77 session stores and epoch-49 Landscape stores" in " ".join(release_089.split())
    assert "new epoch-78 target" in release_089
    assert "Drain and isolate old/new deployments" in release_089
    assert "no migration,\nreset, restamping or downgrade" in release_089
    release_date = current_release.splitlines()[0].strip()
    assert date.fromisoformat(release_date).isoformat() == release_date
    assert "Composer correctness and recovery" in release_086
    assert "Reply admission does not clear graph" in release_086
    assert "Session epoch advances from 75 to 76" in release_086
    assert "explicitly new epoch-76 target" in release_086
    assert "Automatic image publication from main remains held" in " ".join(release_089.split())
    assert "The 0.8.9 tag and container publication follow successful required checks and the protected merge" in " ".join(
        release_089.split()
    )
    assert "LLM compatibility and Composer recovery" in release_085
    assert "Omitted LLM temperature uses the provider default" in release_085
    assert "Session epoch 72 adds durable Composer jobs" in release_084
    assert "Landscape epoch 49 remains unchanged" in release_084
    assert "A Landscape database already at epoch 49 remains usable" in " ".join(release_084.split())

    assert "Session epoch 71 and Landscape epoch 49" in release_082
    assert "unchanged from 0.8.1; this release adds no schema-epoch cutover" in " ".join(release_082.split())

    assert "SESSION_SCHEMA_EPOCH` advances from 53\nto 71" in release_081
    assert "SQLITE_SCHEMA_EPOCH` advances from 38 to 49" in release_081
    assert "Session epoch 71 removes mode-specific operation state" in release_081
    assert "install 0.8.1" in " ".join(release_081.split())

    assert "SESSION_SCHEMA_EPOCH` advances from 35\nto 53" in release_080
    assert "SQLITE_SCHEMA_EPOCH` advances from 29 to 38" in release_080
    assert "retryable" in release_080 and "blob deletion" in release_080.lower()
    assert "install 0.8.0" in " ".join(release_080.split())

    assert "SESSION_SCHEMA_EPOCH` advances from 26\nto 35" in release_071
    assert "install\n0.7.1" in release_071
    assert "Blob deletion cleanup remains retryable" not in release_071


def test_current_release_compatibility_validator_rejects_malformed_iso_date(monkeypatch: pytest.MonkeyPatch) -> None:
    test_changelog_preserves_historical_epochs_and_documents_current_compatibility()
    original_text = _text
    changelog = original_text(ROOT / "CHANGELOG.md")
    current_heading = next(line for line in changelog.splitlines() if line.startswith(f"## {CURRENT_VERSION} - "))
    drifted = changelog.replace(current_heading, f"## {CURRENT_VERSION} - 2026-10-XX", 1)
    assert drifted != changelog

    def text_with_bad_date(path: Path) -> str:
        return drifted if path == ROOT / "CHANGELOG.md" else original_text(path)

    monkeypatch.setattr(f"{__name__}._text", text_with_bad_date)
    with pytest.raises(ValueError, match="Invalid isoformat string"):
        test_changelog_preserves_historical_epochs_and_documents_current_compatibility()


def test_every_page_has_description_favicon_and_current_navigation() -> None:
    favicon = WEBSITE / "favicon.svg"
    assert favicon.is_file()

    for name, current_href in PAGES.items():
        soup = _soup(name)
        description = soup.find("meta", attrs={"name": "description"})
        icon = soup.find("link", attrs={"rel": "icon"})
        current_links = soup.find_all("a", attrs={"aria-current": "page"})

        assert description is not None and description.get("content", "").strip(), name
        assert icon is not None and icon.get("href") == "favicon.svg", name
        assert len(current_links) == 2, name
        assert all(link.get("href") == current_href for link in current_links), name


def test_every_page_advertises_current_release() -> None:
    for name in PAGES:
        html = _text(WEBSITE / name)
        assert f"Changelog · v{CURRENT_VERSION}" in html, name
        assert f"ELSPETH {CURRENT_VERSION}" in html, name


def test_home_surfaces_current_and_predecessor_releases_without_invented_counts() -> None:
    html = _text(WEBSITE / "index.html")

    assert f"Current version: {CURRENT_VERSION}" in html
    assert "0.7.0" in html and "LLM-primary" in html
    assert "0.7.1" in html and "recoverable publication" in html.lower()
    assert "0.8.0" in html and "unified lineage" in html.lower()
    assert "240 rows" not in html
    assert "pre-1.0" in html


def test_authoring_describes_current_staged_and_structural_capabilities() -> None:
    html = _text(WEBSITE / "authoring.html")

    assert "Freeform — one open conversation to build and revise the pipeline" in html
    assert "source → transforms → sink → wire" not in html
    assert "LLM-primary" in html
    assert "candidate remains separate" in html.lower()
    assert "structural queues, fork, and coalesce nodes" in html
    assert "require_all" in html
    assert "The freeform Web Composer uses an LLM" in html
    assert "In the bundled" not in html
    assert "38ac0f55" not in html and "098ec06d" not in html
    for capability in ("structural queue", "fork", "coalesce", "text source", "text sink"):
        assert capability in html.lower()


def test_assurance_explains_recovery_without_claiming_bundled_evidence() -> None:
    html = _text(WEBSITE / "assurance.html")

    assert "Recoverable publication" in html
    assert "before external publication" in html
    assert "elspeth export-resume &lt;run-id&gt; --execute" in html
    assert "sink-effect-recovery.md" in html
    assert "bundled run" not in html.lower()
    assert "examples/*/runs/audit.db" not in html


def test_use_cases_are_illustrative_and_distinguish_extension_work() -> None:
    html = _text(WEBSITE / "use-cases.html")

    assert "Illustrative workflow patterns" in html
    assert "Built in" in html
    assert "custom plugins" in html
    assert "sub-second streaming engine" in html

    soup = _soup("use-cases.html")
    tables = soup.select(".table-scroll")
    assert tables
    assert all(table.get("tabindex") == "0" for table in tables)
    assert all(table.get("role") == "region" for table in tables)
    assert all(table.get("aria-label") for table in tables)


def test_get_started_has_runnable_cli_and_complete_composer_paths() -> None:
    html = _text(WEBSITE / "get-started.html")

    assert 'uv pip install -e ".[dev]"' in html
    assert 'uv pip install -e ".[webui,dev]"' in html
    assert "examples/threshold_gate/settings.yaml" in html
    assert "data/submissions.csv" not in html
    assert "npm install" in html and "npm run build" in html
    assert "ELSPETH_WEB__SECRET_KEY" in html
    assert "elspeth composer users add" in html and "--password" in html
    soup = _soup("get-started.html")
    current = soup.select(f'[data-release="{CURRENT_VERSION}"]')
    assert len(current) == 1
    current_text = current[0].get_text(" ", strip=True)
    assert f"From 0.8.0 to {CURRENT_VERSION}" in current_text
    assert f"Sessions epoch {SESSION_SCHEMA_EPOCH}" in current_text
    assert f"Landscape epoch {SQLITE_SCHEMA_EPOCH}" in current_text
    assert f"Sessions advance from 78 to {SESSION_SCHEMA_EPOCH}" in current_text
    assert f"Landscape epoch {SQLITE_SCHEMA_EPOCH} remains unchanged" in current_text
    assert "Qualification uses explicitly new compatible stores" in current_text
    assert f"explicitly new epoch-{SQLITE_SCHEMA_EPOCH} Landscape target" in current_text
    assert "epoch-49 Landscape stores with compatible prior application access" in current_text
    assert "preserved and refused before incompatible writes" in current_text
    assert "Do not recreate, delete, restamp or migrate them" in current_text
    assert "ordinary freeform composition" in current_text
    assert "older readers ignore those completion blockers" in current_text
    assert "Preserve epoch-75 stores" in current_text
    assert f"explicitly new epoch-{SESSION_SCHEMA_EPOCH} target" in current_text
    assert "both distinct epoch-76 contracts" in current_text
    assert "Quota API quantities are exact signed-64-bit decimal strings" in current_text
    assert "Proxy deployments must explicitly configure their trusted forwarding peers" in current_text
    release_086 = soup.select('[data-release="0.8.6"]')
    assert len(release_086) == 1
    assert "Sessions epoch 76" in release_086[0].get_text(" ", strip=True)
    assert "do not share session stores" in current_text
    release_082 = soup.select('[data-release="0.8.2"]')
    assert len(release_082) == 1
    historical_082_text = release_082[0].get_text(" ", strip=True)
    assert "Sessions epoch 71" in historical_082_text and "Landscape epoch 49" in historical_082_text
    assert "Upgrades from 0.8.1 keep both database epochs unchanged" in historical_082_text
    predecessor = soup.select('[data-release="0.8.1"]')
    assert len(predecessor) == 1
    predecessor_text = predecessor[0].get_text(" ", strip=True)
    assert "From 0.8.0 to 0.8.1" in predecessor_text
    assert "Sessions epoch 71" in predecessor_text and "Landscape epoch 49" in predecessor_text
    historical = soup.select('[data-release="0.8.0"]')
    assert len(historical) == 1
    historical_text = historical[0].get_text(" ", strip=True)
    assert "from 0.7.1 to 0.8.0" in historical_text
    assert "both database schemas" in historical_text and "coordinated recreation" in historical_text
    assert f"before installing {CURRENT_VERSION}" in html
    assert "If upgrading from 0.8.0 or earlier" in html
    assert "aws-ecs-deployment.md" in html


def test_theme_toggle_rerenders_replaced_lucide_icon() -> None:
    script = _text(WEBSITE / "site.js")
    set_icon = script.split("function setIcon()", maxsplit=1)[1].split("// --- copy", maxsplit=1)[0]

    assert "window.lucide.createIcons" in set_icon


def test_assurance_proof_uses_semantic_figure_markup() -> None:
    soup = _soup("assurance.html")
    proof = soup.find("figure", class_="term")

    assert proof is not None
    assert proof.find("figcaption") is not None
    assert not soup.find_all("h4")
