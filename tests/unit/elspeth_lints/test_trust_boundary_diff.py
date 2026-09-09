"""PR-diff surfacing for newly-added ``@trust_boundary`` decorators."""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from elspeth_lints.core import trust_boundary_diff
from elspeth_lints.core.cli import main
from elspeth_lints.core.trust_boundary_diff import (
    find_new_trust_boundary_decorators,
    render_trust_boundary_diff_summary,
)


def _init_git_fixture(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    src = tmp_path / "src" / "elspeth"
    src.mkdir(parents=True)
    return src


def _commit(repo_root: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(repo_root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo_root), "commit", "-q", "-m", message], check=True)
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


_OBSERVATION_DECORATOR = """
@observation_boundary(
    tier=3,
    source="x",
    source_param="data",
    suppresses=("R1",),
    invariant="returns None on absence",
)
def handler(data):
    return data.get("x")
"""


def _boundary_after_loop_source(
    *,
    loop_kind: str,
    initial_import: str,
    body: str,
    orelse: str | None = None,
) -> str:
    header = {
        "for": "for item in items:",
        "async for": "async for item in items:",
        "while": "while enabled:",
    }[loop_kind]
    loop = f"{header}\n{textwrap.indent(textwrap.dedent(body).strip(), '    ')}"
    if orelse is not None:
        loop += f"\nelse:\n{textwrap.indent(textwrap.dedent(orelse).strip(), '    ')}"
    outer_body = f"{initial_import}\n\n{loop}\n\n{textwrap.dedent(_OBSERVATION_DECORATOR).strip()}"
    return f"async def outer(items, enabled, stop):\n{textwrap.indent(outer_body, '    ')}\n"


def _with_suite(with_kind: str, body: str) -> str:
    return f"{with_kind} suppressing_context():\n{textwrap.indent(textwrap.dedent(body).strip(), '    ')}"


def _boundary_in_finally_source(transfer: str) -> str:
    try_body = f"""
        from foreign import observation_boundary
        {transfer}
        from elspeth.contracts.trust_boundary import observation_boundary
    """
    try_suite = f"try:\n{textwrap.indent(textwrap.dedent(try_body).strip(), '    ')}\nfinally:\n{textwrap.indent(textwrap.dedent(_OBSERVATION_DECORATOR).strip(), '    ')}"
    if transfer in {"break", "continue"}:
        return f"async def outer(items):\n    for item in items:\n{textwrap.indent(try_suite, '        ')}\n"
    return f"def outer():\n{textwrap.indent(try_suite, '    ')}\n"


@pytest.mark.parametrize(
    "prefix",
    [
        "",
        "@elspeth.contracts.trust_boundary.observation_boundary(\n"
        "    tier=3, source='x', source_param='data', suppresses=('R1',),\n"
        "    invariant='returns None on absence',\n"
        ")\ndef handler(data):\n    return data.get('x')\n",
        "from foreign import observation_boundary\n",
        "from .elspeth.contracts.trust_boundary import observation_boundary\n",
        "if enabled:\n    from elspeth.contracts.trust_boundary import observation_boundary\n",
        "from elspeth.contracts.trust_boundary import observation_boundary\nobservation_boundary = foreign\n",
    ],
)
def test_source_records_require_proven_scoped_boundary_import(prefix: str) -> None:
    source = prefix if prefix.startswith("@elspeth.") else prefix + _OBSERVATION_DECORATOR

    records = trust_boundary_diff._records_from_source(source=source, source_file="handler.py")

    assert records == ()


def test_source_records_accept_canonical_fully_qualified_import() -> None:
    source = "import elspeth.contracts.trust_boundary\n\n" + _OBSERVATION_DECORATOR.replace(
        "@observation_boundary", "@elspeth.contracts.trust_boundary.observation_boundary"
    )

    records = trust_boundary_diff._records_from_source(source=source, source_file="handler.py")

    assert [record.symbol for record in records] == ["handler"]


@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_source_records_apply_foreign_finally_to_break_path(loop_kind: str) -> None:
    source = _boundary_after_loop_source(
        loop_kind=loop_kind,
        initial_import="from foreign import observation_boundary",
        body="""
            try:
                from elspeth.contracts.trust_boundary import observation_boundary
                break
            finally:
                from foreign import observation_boundary
        """,
        orelse="from elspeth.contracts.trust_boundary import observation_boundary",
    )

    records = trust_boundary_diff._records_from_source(source=source, source_file="handler.py")

    assert records == ()


@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_source_records_apply_canonical_finally_to_break_path(loop_kind: str) -> None:
    source = _boundary_after_loop_source(
        loop_kind=loop_kind,
        initial_import="from foreign import observation_boundary",
        body="""
            try:
                from foreign import observation_boundary
                break
            finally:
                from elspeth.contracts.trust_boundary import observation_boundary
        """,
        orelse="from elspeth.contracts.trust_boundary import observation_boundary",
    )

    records = trust_boundary_diff._records_from_source(source=source, source_file="handler.py")

    assert [record.symbol for record in records] == ["outer.handler"]


@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_source_records_apply_foreign_finally_to_continue_path(loop_kind: str) -> None:
    source = _boundary_after_loop_source(
        loop_kind=loop_kind,
        initial_import="from foreign import observation_boundary",
        body="""
            try:
                from elspeth.contracts.trust_boundary import observation_boundary
                continue
            finally:
                from foreign import observation_boundary
        """,
    )

    records = trust_boundary_diff._records_from_source(source=source, source_file="handler.py")

    assert records == ()


@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_source_records_apply_canonical_finally_to_continue_path(loop_kind: str) -> None:
    source = _boundary_after_loop_source(
        loop_kind=loop_kind,
        initial_import="from elspeth.contracts.trust_boundary import observation_boundary",
        body="""
            try:
                from foreign import observation_boundary
                continue
            finally:
                from elspeth.contracts.trust_boundary import observation_boundary
        """,
    )

    records = trust_boundary_diff._records_from_source(source=source, source_file="handler.py")

    assert [record.symbol for record in records] == ["outer.handler"]


@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_source_records_finally_break_replaces_continue(loop_kind: str) -> None:
    source = _boundary_after_loop_source(
        loop_kind=loop_kind,
        initial_import="from elspeth.contracts.trust_boundary import observation_boundary",
        body="""
            try:
                from foreign import observation_boundary
                continue
                from elspeth.contracts.trust_boundary import observation_boundary
            finally:
                break
        """,
        orelse="from elspeth.contracts.trust_boundary import observation_boundary",
    )

    records = trust_boundary_diff._records_from_source(source=source, source_file="handler.py")

    assert records == ()


@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_source_records_finally_continue_replaces_break(loop_kind: str) -> None:
    source = _boundary_after_loop_source(
        loop_kind=loop_kind,
        initial_import="from elspeth.contracts.trust_boundary import observation_boundary",
        body="""
            try:
                from foreign import observation_boundary
                break
                from elspeth.contracts.trust_boundary import observation_boundary
            finally:
                continue
        """,
        orelse="from elspeth.contracts.trust_boundary import observation_boundary",
    )

    records = trust_boundary_diff._records_from_source(source=source, source_file="handler.py")

    assert [record.symbol for record in records] == ["outer.handler"]


@pytest.mark.parametrize("pending_exit", ["return None", "raise RuntimeError"])
@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_source_records_finally_break_replaces_non_loop_exit(loop_kind: str, pending_exit: str) -> None:
    source = _boundary_after_loop_source(
        loop_kind=loop_kind,
        initial_import="from elspeth.contracts.trust_boundary import observation_boundary",
        body=f"""
            try:
                from foreign import observation_boundary
                {pending_exit}
                from elspeth.contracts.trust_boundary import observation_boundary
            finally:
                break
        """,
        orelse="from elspeth.contracts.trust_boundary import observation_boundary",
    )

    records = trust_boundary_diff._records_from_source(source=source, source_file="handler.py")

    assert records == ()


@pytest.mark.parametrize("final_exit", ["return None", "raise RuntimeError"])
@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_source_records_finally_non_loop_exit_replaces_break(loop_kind: str, final_exit: str) -> None:
    source = _boundary_after_loop_source(
        loop_kind=loop_kind,
        initial_import="from elspeth.contracts.trust_boundary import observation_boundary",
        body=f"""
            try:
                from foreign import observation_boundary
                break
                from elspeth.contracts.trust_boundary import observation_boundary
            finally:
                {final_exit}
        """,
        orelse="from elspeth.contracts.trust_boundary import observation_boundary",
    )

    records = trust_boundary_diff._records_from_source(source=source, source_file="handler.py")

    assert [record.symbol for record in records] == ["outer.handler"]


@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_source_records_nested_finally_replaces_transfer_kind(loop_kind: str) -> None:
    source = _boundary_after_loop_source(
        loop_kind=loop_kind,
        initial_import="from elspeth.contracts.trust_boundary import observation_boundary",
        body="""
            try:
                try:
                    from foreign import observation_boundary
                    continue
                    from elspeth.contracts.trust_boundary import observation_boundary
                finally:
                    break
            finally:
                continue
        """,
        orelse="from elspeth.contracts.trust_boundary import observation_boundary",
    )

    records = trust_boundary_diff._records_from_source(source=source, source_file="handler.py")

    assert [record.symbol for record in records] == ["outer.handler"]


@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_source_records_finally_break_preserves_implicit_exception_path(loop_kind: str) -> None:
    source = _boundary_after_loop_source(
        loop_kind=loop_kind,
        initial_import="from elspeth.contracts.trust_boundary import observation_boundary",
        body="""
            try:
                from foreign import observation_boundary
                might_raise()
                from elspeth.contracts.trust_boundary import observation_boundary
            finally:
                break
        """,
        orelse="from elspeth.contracts.trust_boundary import observation_boundary",
    )

    records = trust_boundary_diff._records_from_source(source=source, source_file="handler.py")

    assert records == ()


@pytest.mark.parametrize("with_kind", ["with", "async with"])
@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_source_records_with_suppressed_explicit_exception_reaches_break(
    loop_kind: str,
    with_kind: str,
) -> None:
    source = _boundary_after_loop_source(
        loop_kind=loop_kind,
        initial_import="from foreign import observation_boundary",
        body=_with_suite(
            with_kind,
            """
                raise RuntimeError
                from elspeth.contracts.trust_boundary import observation_boundary
            """,
        )
        + "\nbreak",
        orelse="from elspeth.contracts.trust_boundary import observation_boundary",
    )

    records = trust_boundary_diff._records_from_source(source=source, source_file="handler.py")

    assert records == ()


@pytest.mark.parametrize("with_kind", ["with", "async with"])
@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_source_records_with_suppressed_implicit_exception_reaches_break(
    loop_kind: str,
    with_kind: str,
) -> None:
    source = _boundary_after_loop_source(
        loop_kind=loop_kind,
        initial_import="from foreign import observation_boundary",
        body=_with_suite(
            with_kind,
            """
                might_raise()
                from elspeth.contracts.trust_boundary import observation_boundary
            """,
        )
        + "\nbreak",
        orelse="from elspeth.contracts.trust_boundary import observation_boundary",
    )

    records = trust_boundary_diff._records_from_source(source=source, source_file="handler.py")

    assert records == ()


@pytest.mark.parametrize("with_kind", ["with", "async with"])
@pytest.mark.parametrize("loop_kind", ["for", "async for", "while"])
def test_source_records_with_canonical_exception_ignores_unreachable_foreign(
    loop_kind: str,
    with_kind: str,
) -> None:
    source = _boundary_after_loop_source(
        loop_kind=loop_kind,
        initial_import="from elspeth.contracts.trust_boundary import observation_boundary",
        body=_with_suite(
            with_kind,
            """
                raise RuntimeError
                from foreign import observation_boundary
            """,
        )
        + "\nbreak",
        orelse="from elspeth.contracts.trust_boundary import observation_boundary",
    )

    records = trust_boundary_diff._records_from_source(source=source, source_file="handler.py")

    assert [record.symbol for record in records] == ["outer.handler"]


@pytest.mark.parametrize("transfer", ["break", "continue", "return None", "raise RuntimeError"])
def test_source_records_finally_use_reachable_pre_finally_aliases(transfer: str) -> None:
    records = trust_boundary_diff._records_from_source(
        source=_boundary_in_finally_source(transfer),
        source_file="handler.py",
    )

    assert records == ()


def test_source_records_finally_use_implicit_exception_aliases_before_unreachable_repair() -> None:
    source = f"""
        def outer(exported):
            try:
                from foreign import observation_boundary
                might_raise()
                from elspeth.contracts.trust_boundary import observation_boundary
            finally:
{textwrap.indent(textwrap.dedent(_OBSERVATION_DECORATOR).strip(), "                ")}
                exported.append(handler)
                return handler
    """

    records = trust_boundary_diff._records_from_source(
        source=textwrap.dedent(source),
        source_file="handler.py",
    )

    assert records == ()


def test_diff_reports_new_trust_boundary_decorator(tmp_path: Path) -> None:
    src = _init_git_fixture(tmp_path)
    target = src / "handler.py"
    target.write_text("def handler(arguments):\n    return arguments.get('x')\n", encoding="utf-8")
    baseline = _commit(tmp_path, "baseline")

    target.write_text(
        textwrap.dedent("""\
        from elspeth.contracts import trust_boundary

        @trust_boundary(
            tier=3,
            source="LLM tool args",
            source_param="arguments",
            suppresses=("R1",),
            invariant="raises on bad args",
            test_ref="tests/test_handler.py::test_rejects_bad_args",
            test_fingerprint="abc123",
        )
        def handler(arguments):
            return arguments.get("x")
    """),
        encoding="utf-8",
    )
    _commit(tmp_path, "add trust boundary")

    report = find_new_trust_boundary_decorators(
        root=src,
        baseline_ref=baseline,
        repo_root=tmp_path,
    )

    assert len(report.new_decorators) == 1
    decorator = report.new_decorators[0]
    assert decorator.source_file == "src/elspeth/handler.py"
    assert decorator.symbol == "handler"
    assert decorator.source_param == "arguments"
    assert decorator.suppresses == ("R1",)
    assert decorator.source == "LLM tool args"
    assert decorator.test_ref == "tests/test_handler.py::test_rejects_bad_args"


def test_diff_reports_new_observation_boundary_decorator(tmp_path: Path) -> None:
    src = _init_git_fixture(tmp_path)
    target = src / "handler.py"
    target.write_text("def handler(arguments):\n    return None\n", encoding="utf-8")
    baseline = _commit(tmp_path, "baseline")

    target.write_text(
        textwrap.dedent("""\
        from elspeth.contracts.trust_boundary import observation_boundary

        @observation_boundary(
            tier=3,
            source="optional LLM field",
            source_param="arguments",
            suppresses=("R1",),
            invariant="returns None on absence",
        )
        def handler(arguments):
            return arguments.get("value")
    """),
        encoding="utf-8",
    )
    _commit(tmp_path, "add observation boundary")

    report = find_new_trust_boundary_decorators(
        root=src,
        baseline_ref=baseline,
        repo_root=tmp_path,
    )

    assert len(report.new_decorators) == 1
    decorator = report.new_decorators[0]
    assert decorator.symbol == "handler"
    assert decorator.source_param == "arguments"
    assert decorator.test_ref is None


def test_diff_grandfathers_existing_trust_boundary_decorator(tmp_path: Path) -> None:
    src = _init_git_fixture(tmp_path)
    (src / "handler.py").write_text(
        textwrap.dedent("""\
        from elspeth.contracts import trust_boundary

        @trust_boundary(
            tier=3,
            source="LLM tool args",
            source_param="arguments",
            suppresses=("R1",),
            invariant="raises on bad args",
            test_ref="tests/test_handler.py::test_rejects_bad_args",
            test_fingerprint="abc123",
        )
        def handler(arguments):
            return arguments.get("x")
    """),
        encoding="utf-8",
    )
    baseline = _commit(tmp_path, "baseline")

    (src / "handler.py").write_text(
        textwrap.dedent("""\
        from elspeth.contracts import trust_boundary

        @trust_boundary(
            tier=3,
            source="LLM tool args",
            source_param="arguments",
            suppresses=("R1",),
            invariant="raises on bad args",
            test_ref="tests/test_handler.py::test_rejects_bad_args",
            test_fingerprint="abc123",
        )
        def handler(arguments):
            value = arguments.get("x")
            return value
    """),
        encoding="utf-8",
    )
    _commit(tmp_path, "body-only change")

    report = find_new_trust_boundary_decorators(
        root=src,
        baseline_ref=baseline,
        repo_root=tmp_path,
    )

    assert report.new_decorators == ()


def test_summary_names_new_decorators() -> None:
    from elspeth_lints.core.trust_boundary_diff import TrustBoundaryDecoratorRecord, TrustBoundaryDiffReport

    report = TrustBoundaryDiffReport(
        baseline_ref="abc123",
        root="src/elspeth",
        new_decorators=(
            TrustBoundaryDecoratorRecord(
                source_file="src/elspeth/handler.py",
                line=3,
                symbol="Handler.run",
                source_param="arguments",
                suppresses=("R1", "R5"),
                source="LLM tool args",
                test_ref="tests/test_handler.py::test_rejects_bad_args",
                metadata_readable=True,
                identity_hash="deadbeef",
            ),
        ),
    )

    summary = render_trust_boundary_diff_summary(report)

    assert "New @trust_boundary decorators: 1" in summary
    assert "src/elspeth/handler.py:3 Handler.run" in summary
    assert "source_param=arguments" in summary
    assert "suppresses=R1,R5" in summary


def test_cli_summarizes_new_trust_boundary_decorator(tmp_path: Path, capsys) -> None:
    src = _init_git_fixture(tmp_path)
    target = src / "handler.py"
    target.write_text("def handler(arguments):\n    return arguments.get('x')\n", encoding="utf-8")
    baseline = _commit(tmp_path, "baseline")

    target.write_text(
        textwrap.dedent("""\
        from elspeth.contracts import trust_boundary

        @trust_boundary(
            tier=3,
            source="LLM tool args",
            source_param="arguments",
            suppresses=("R1",),
            invariant="raises on bad args",
            test_ref="tests/test_handler.py::test_rejects_bad_args",
            test_fingerprint="abc123",
        )
        def handler(arguments):
            return arguments.get("x")
    """),
        encoding="utf-8",
    )
    _commit(tmp_path, "add trust boundary")

    status = main(
        [
            "check-trust-boundary-diff",
            "--baseline-ref",
            baseline,
            "--root",
            "src/elspeth",
            "--repo-root",
            str(tmp_path),
        ]
    )

    out = capsys.readouterr().out
    assert status == 0
    assert "New @trust_boundary decorators: 1" in out
    assert "src/elspeth/handler.py" in out
