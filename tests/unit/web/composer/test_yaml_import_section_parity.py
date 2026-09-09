"""Python↔TypeScript parity for importable runtime-YAML sections.

The backend importer owns the accepted top-level section vocabulary.  The
frontend preview repeats that vocabulary so it can count components before the
server validates the document.  A backend-only addition must fail here rather
than disappear from the preview or trigger the misleading "required section"
error that prompted elspeth-f543107f50.
"""

from __future__ import annotations

import re
from pathlib import Path

import elspeth
from elspeth.web.composer.yaml_importer import _PIPELINE_SECTION_KEYS

_PACKAGE_ROOT = Path(elspeth.__file__).parent
_IMPORT_MODAL_PATH = _PACKAGE_ROOT / "web" / "frontend" / "src" / "components" / "sidebar" / "ImportYamlModal.tsx"
_SECTION_TUPLE_RE = re.compile(
    r"export\s+const\s+IMPORT_YAML_SECTION_KEYS\s*=\s*\[(?P<body>[^\]]*)\]\s*as\s+const\s*;",
)
_MEMBER_RE = re.compile(r'"([^"]+)"')


def _frontend_section_keys() -> set[str]:
    source = _IMPORT_MODAL_PATH.read_text(encoding="utf-8")
    matches = _SECTION_TUPLE_RE.findall(source)
    assert len(matches) == 1, (
        f"Expected exactly one IMPORT_YAML_SECTION_KEYS tuple in {_IMPORT_MODAL_PATH.name}, "
        f"matched {len(matches)}. Keep the frontend preview vocabulary in that named tuple so "
        "its cross-language parity remains mechanically checkable."
    )
    return set(_MEMBER_RE.findall(matches[0]))


def test_frontend_import_sections_match_backend_importer() -> None:
    assert _frontend_section_keys() == set(_PIPELINE_SECTION_KEYS)


def test_frontend_import_section_tuple_is_not_empty() -> None:
    assert _IMPORT_MODAL_PATH.is_file()
    assert _frontend_section_keys()
