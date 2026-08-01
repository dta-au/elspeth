"""Executable contracts for the shipped ELSPETH design skill."""

from __future__ import annotations

import html
import re
from pathlib import Path

import pytest
import yaml

from elspeth.core.config import load_settings_from_yaml_string

REPO_ROOT = Path(__file__).resolve().parents[3]
DESIGN_SKILL = REPO_ROOT / ".claude" / "skills" / "elspeth-design"
SKILL_WEBSITE = DESIGN_SKILL / "ui_kits" / "website"
CANONICAL_WEBSITE = REPO_ROOT / "website"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _website_yaml() -> str:
    page = _read(SKILL_WEBSITE / "get-started.html")
    match = re.search(
        r'<pre class="code" data-settings-example="threshold_gate">(?P<yaml>.*?)</pre>',
        page,
        re.DOTALL,
    )
    assert match is not None, "get-started must identify its runnable threshold_gate settings"
    without_markup = re.sub(r"<[^>]+>", "", match.group("yaml"))
    return html.unescape(without_markup)


def _mobile_menu(page: str) -> str:
    match = re.search(r'<details class="nav-menu">.*?</details>', page, re.DOTALL)
    assert match is not None, "every page needs the canonical mobile navigation disclosure"
    return " ".join(match.group(0).split())


def test_get_started_uses_the_current_settings_schema() -> None:
    settings = load_settings_from_yaml_string(_website_yaml())

    assert settings.sources
    assert settings.sinks
    for source in settings.sources.values():
        path = source.options.get("path")
        assert isinstance(path, str)
        assert (REPO_ROOT / path).is_file(), path


def test_get_started_embeds_the_tracked_threshold_gate_example() -> None:
    tracked = _read(REPO_ROOT / "examples" / "threshold_gate" / "settings.yaml")

    assert yaml.safe_load(_website_yaml()) == yaml.safe_load(tracked)


@pytest.mark.parametrize("filename", ["index.html", "authoring.html", "assurance.html", "use-cases.html", "get-started.html"])
def test_skill_website_mobile_navigation_matches_canonical_site(filename: str) -> None:
    assert _mobile_menu(_read(SKILL_WEBSITE / filename)) == _mobile_menu(_read(CANONICAL_WEBSITE / filename))

    css = _read(SKILL_WEBSITE / "site.css")
    assert ".nav-menu { position: relative; display: none; }" in css
    mobile = css[css.index("@media (max-width: 900px)") :]
    assert ".nav-menu { display: block; }" in mobile
    assert ".nav-right .btn-compact { display: none; }" in mobile
