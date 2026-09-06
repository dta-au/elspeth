"""Every PostgreSQL-backed suite reaches its server through ``tests.helpers.postgres_target``.

The ``testcontainer-run`` receipt an acceptance driver stores says which
database the selection ran against (``testcontainers-docker`` or
``provisioned``), derived from ``ELSPETH_TEST_POSTGRES_URL``. That claim is
only true if EVERY suite in the selection honours the variable, so the seam
is one helper and this gate pins that no suite constructs a
``PostgresContainer`` of its own: a new suite that did would run on the
acceptance host's Docker while the receipt recorded the provisioned server
(elspeth-0ec6918940). A new PostgreSQL-backed suite calls
``postgres_test_target``; there is no second seam to register.
"""

from __future__ import annotations

import re
from pathlib import Path

from elspeth_lints.core.ast_walker import iter_python_files

REPO_ROOT = Path(__file__).resolve().parents[3]
TESTS_ROOT = REPO_ROOT / "tests"
SEAM = Path("tests/helpers/postgres_target.py")
_CONTAINER_CONSTRUCTION = re.compile(r"\bPostgresContainer\s*\(")
_CONTAINER_IMPORT = re.compile(r"^\s*from\s+testcontainers\.postgres\s+import\b|^\s*import\s+testcontainers\b", re.MULTILINE)


def test_no_postgres_container_is_constructed_outside_the_seam() -> None:
    constructions: list[str] = []
    imports: list[str] = []
    for path in iter_python_files(TESTS_ROOT):
        relative = path.relative_to(REPO_ROOT)
        if relative == SEAM:
            continue
        source = path.read_text(encoding="utf-8")
        if _CONTAINER_CONSTRUCTION.search(source):
            constructions.append(str(relative))
        if _CONTAINER_IMPORT.search(source):
            imports.append(str(relative))
    assert constructions == [], f"PostgresContainer constructed outside {SEAM}: {constructions}"
    assert imports == [], f"testcontainers imported outside {SEAM}: {imports}"


def test_the_seam_itself_constructs_the_container_and_reads_the_variable() -> None:
    source = (REPO_ROOT / SEAM).read_text(encoding="utf-8")
    assert len(_CONTAINER_CONSTRUCTION.findall(source)) == 1
    assert 'PROVISIONED_POSTGRES_URL_ENV = "ELSPETH_TEST_POSTGRES_URL"' in source
