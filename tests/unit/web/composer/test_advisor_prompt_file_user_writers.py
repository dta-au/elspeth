"""Census of every production writer that can store a blob row as written by the user.

The advisor keeps back the text of a prompt file only when the row says the
user wrote it (``advisor_prompt_file_author`` returns ``user``). Two writers of
such rows carry someone else's text, and the authorship decision recognises
them by their stored description: the shared-link import and the session-fork
copy. A third writer of user rows carrying another person's text would be
treated as the user's own, and its text would silently stop reaching the
advisor. This census fails when a writer appears that is not in the reviewed
list below, so that case is decided when it is written, not discovered later.

A writer site is a call that passes ``created_by`` either as the constant
``"user"`` (any call), or as a non-constant value to a blob-row write
(``create_blob``, ``_persist_blob_content``, or a SQL ``.values(...)``), which
copies an existing row's authorship. Constants ``"assistant"`` and
``"pipeline"`` are not user rows and are not listed.

This is a guard: it passes on the base and on the lane. The controls prove the
scanner reports a new writer and ignores a non-user one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import elspeth
from tests.helpers.tree_gate import iter_gate_files

_SRC = Path(elspeth.__file__).resolve().parent
_ROW_WRITES = frozenset({"create_blob", "_persist_blob_content", "values"})

# (path under src/elspeth, enclosing qualified name): why its user rows are honest.
REVIEWED_USER_ROW_WRITERS: dict[tuple[str, str], str] = {
    ("web/blobs/routes.py", "create_blobs_router.create_blob_upload"): "the user's own upload",
    ("web/blobs/routes.py", "create_blobs_router.create_blob_inline"): "text the user typed",
    ("web/blobs/prompt_text_routes.py", "create_prompt_text_router.save_prompt_text"): (
        "the user's editor save; it keeps the file's earlier description, so an import or fork copy stays recognised"
    ),
    ("web/shareable_reviews/importer.py", "_copy_files"): "another person's text: recognised by SHARED_IMPORT_DESCRIPTION_PREFIX",
    ("web/blobs/service.py", "BlobServiceImpl.copy_blobs_for_fork._persist_copy"): (
        "copies the source row's authorship: recognised by SESSION_FORK_COPY_DESCRIPTION_PREFIX"
    ),
    ("web/blobs/service.py", "_insert_pending_blob_row"): "persists a record the service already built",
    (
        "web/coordination/repository.py",
        "_RepositoryBlobMutations.commit_blob_replacement",
    ): "persists a replacement the service already built",
    (
        "web/coordination/repository.py",
        "_RepositoryBlobMutations.reserve_pending_output_blob",
    ): "persists a record the service already built",
    ("web/coordination/repository.py", "_RepositoryBlobMutations.reserve_blob"): "persists a record the service already built",
}


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _writes_user_row(call: ast.Call) -> bool:
    for keyword in call.keywords:
        if keyword.arg != "created_by":
            continue
        value = keyword.value
        if isinstance(value, ast.Constant):
            return value.value == "user"
        return _call_name(call.func) in _ROW_WRITES
    return False


def user_row_writers(source: str) -> set[str]:
    """Enclosing qualified names of every user-row writer call in one module's source."""
    found: set[str] = set()

    def visit(node: ast.AST, names: tuple[str, ...]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = (*names, node.name)
        if isinstance(node, ast.Call) and _writes_user_row(node):
            found.add(".".join(names) or "<module>")
        for child in ast.iter_child_nodes(node):
            visit(child, names)

    visit(ast.parse(source), ())
    return found


def _production_writers() -> set[tuple[str, str]]:
    writers: set[tuple[str, str]] = set()
    for path in iter_gate_files(_SRC):
        relative = path.relative_to(_SRC).as_posix()
        writers.update((relative, name) for name in user_row_writers(path.read_text(encoding="utf-8")))
    return writers


def test_every_user_row_writer_is_reviewed() -> None:
    found = _production_writers()
    unreviewed = sorted(found - REVIEWED_USER_ROW_WRITERS.keys())
    assert not unreviewed, (
        "A new production writer can store blob rows as written by the user. If it stores another person's text, "
        "give it a recognisable description and teach advisor_prompt_file_author, or the advisor will not review "
        f"that text. Then add it to REVIEWED_USER_ROW_WRITERS with the reason: {unreviewed}"
    )


def test_every_reviewed_writer_still_exists() -> None:
    gone = sorted(REVIEWED_USER_ROW_WRITERS.keys() - _production_writers())
    assert not gone, f"Reviewed writers no longer found (renamed or removed); update the list: {gone}"


def test_scanner_reports_a_new_writer_and_ignores_non_user_rows() -> None:
    source = (
        "async def new_user_writer(service):\n"
        "    await service.create_blob(b'x', created_by='user')\n"
        "def propagating_copy(service, row):\n"
        "    service._persist_blob_content(b'x', created_by=row.created_by)\n"
        "def composer_writer(service):\n"
        "    service.create_blob(b'x', created_by='assistant')\n"
        "def record_reader(row):\n"
        "    return BlobRecord(created_by=row.created_by)\n"
    )
    assert user_row_writers(source) == {"new_user_writer", "propagating_copy"}
