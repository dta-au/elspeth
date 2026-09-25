"""How the template render worker ends a request that fails (elspeth-5887fb7928 S3).

Every non-static template renders in a spawned worker. A request ends in one
reply, and the parent maps it:

- SETUP (limits, unpickling and restoring the context, building the
  template) is ELSPETH's own code on data ELSPETH packed: a failure there is
  ``setup_failed`` and aborts the run (``FrameworkBugError``).
- RENDER (the template meeting the row): a Tier-1 error is ELSPETH's bug and
  aborts; any other failure is this row's, routed by its class name only.
- A missing reply is a worker death, classified by exit status: RLIMIT_CPU's
  SIGXCPU is the row's template running out of CPU; another signal keeps its
  own reason; an exit without a signal is a framework bug.

No exception but ``SystemExit`` leaves the worker, so nothing it prints can
carry row data to the process's stderr.

Spawn targets are module-level so the spawned child can import them; each
patch lives only in the child.
"""

from __future__ import annotations

import multiprocessing
import os
import pickle
import queue
import signal
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from elspeth.contracts.errors import TIER_1_ERRORS, AuditIntegrityError, FrameworkBugError
from elspeth.plugins.infrastructure import templates
from elspeth.plugins.infrastructure.templates import SandboxedTemplate, TemplateError, _UndefinedContractError

_SENTINEL = "SENTINEL-S3-worker-5b2"
_WITHHELD = "(message withheld: it can quote row data)"
_SPAWN = multiprocessing.get_context("spawn")


@contextmanager
def _serving(target: Callable[[Any], None]) -> Iterator[multiprocessing.process.BaseProcess]:
    """Make ``target`` the only render worker the parent will use, then retire it."""
    parent, child = _SPAWN.Pipe(duplex=True)
    process = _SPAWN.Process(target=target, args=(child,), daemon=True)
    process.start()
    child.close()
    available: queue.SimpleQueue[int] = queue.SimpleQueue()
    available.put(0)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(templates, "_AVAILABLE_WORKERS", available)
        patch.setattr(templates, "_WORKERS", [(process, parent)])
        try:
            yield process
        finally:
            templates._stop_template_workers()


@contextmanager
def _raw_worker(target: Callable[[Any], None] = templates._template_worker) -> Iterator[tuple[Any, Any]]:
    """A worker driven over its pipe directly, to read the protocol reply itself."""
    parent, child = _SPAWN.Pipe(duplex=True)
    process = _SPAWN.Process(target=target, args=(child,), daemon=True)
    process.start()
    child.close()
    try:
        yield process, parent
    finally:
        parent.close()
        process.join(60)
        if process.is_alive():
            process.kill()
            process.join()


def _payload(**context: object) -> bytes:
    return pickle.dumps(templates._pack_context_value(context), protocol=5)


def _ask(parent: Any, source: str, payload: bytes, value_free: bool) -> tuple[str, str]:
    parent.send((source, payload, value_free))
    assert parent.poll(60), "the worker sent nothing"
    status, value = parent.recv()
    return status, value


# --- spawn targets (run in the child) ---------------------------------------


def _worker_whose_restore_raises_tier1(connection: Any) -> None:
    def corrupt_restore(value: object, *, memo: object = None) -> object:
        raise AuditIntegrityError(f"corrupt row transport with key {_SENTINEL}")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(templates, "_restore_context_value", corrupt_restore)
        templates._template_worker(connection)


def _worker_whose_literal_scan_raises(connection: Any) -> None:
    def broken_scan(ast: object) -> frozenset[str | int]:
        raise KeyError(_SENTINEL)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(templates, "_template_literals", broken_scan)
        templates._template_worker(connection)


def _worker_whose_sandbox_raises_tier1(connection: Any) -> None:
    def tier1_getattr(self: object, obj: object, attribute: str) -> object:
        raise AuditIntegrityError(f"owned code failed on {_SENTINEL}")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(templates._LocalSandboxedEnvironment, "getattr", tier1_getattr)
        templates._template_worker(connection)


def _worker_whose_sandbox_raises_foreign(connection: Any) -> None:
    """A Jinja error ELSPETH's own undefined type did not build: its message is not trusted."""
    from jinja2.exceptions import SecurityError, UndefinedError

    original_getattr = templates._LocalSandboxedEnvironment.getattr

    def foreign_getattr(self: templates._LocalSandboxedEnvironment, obj: object, attribute: str) -> object:
        if attribute == "unsafe":
            raise SecurityError(f"access to attribute {_SENTINEL!r} is unsafe")
        if attribute == "missing":
            raise UndefinedError(f"'dict object' has no attribute {_SENTINEL!r}")
        return original_getattr(self, obj, attribute)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(templates._LocalSandboxedEnvironment, "getattr", foreign_getattr)
        templates._template_worker(connection)


def _worker_killed_by_signal(connection: Any) -> None:
    connection.recv()
    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(60)


def _worker_exiting_without_reply(connection: Any) -> None:
    connection.recv()
    os._exit(0)


def _worker_replying_a_non_string(connection: Any) -> None:
    connection.recv()
    connection.send(("ok", 42))
    connection.recv()


def _worker_with_a_foreign_undefined_contract(connection: Any) -> None:
    from jinja2 import TemplateRuntimeError
    from jinja2.sandbox import ImmutableSandboxedEnvironment

    def undefined_with_foreign_exc(self: ImmutableSandboxedEnvironment, obj: object, attribute: str) -> object:
        return self.undefined(obj=obj, name=attribute, exc=TemplateRuntimeError)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ImmutableSandboxedEnvironment, "getattr", undefined_with_foreign_exc)
        templates._template_worker(connection)


# --- SETUP phase: ELSPETH's own failure aborts --------------------------------


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        pytest.param(_worker_whose_restore_raises_tier1, "Template worker setup failed: AuditIntegrityError", id="tier1-in-restore"),
        pytest.param(_worker_whose_literal_scan_raises, "Template worker setup failed: KeyError", id="non-tier1-in-setup"),
    ],
)
def test_a_setup_failure_in_the_worker_aborts_and_names_only_its_class(target: Callable[[Any], None], expected: str) -> None:
    with _serving(target) as process:
        with pytest.raises(FrameworkBugError) as caught:
            SandboxedTemplate("{{ row.q }}").render(row={"q": "x"})
        assert str(caught.value) == expected
        assert isinstance(caught.value, TIER_1_ERRORS)
        # The worker exits after reporting; its slot is retired, not reused.
        assert not process.is_alive()
        assert templates._WORKERS == [None]


def test_an_invalid_context_is_a_setup_failure_not_a_row_error() -> None:
    with _raw_worker() as (_, parent):
        status, value = _ask(parent, "{{ row }}", pickle.dumps(["not", "a", "context"], protocol=5), True)
    assert (status, value) == ("setup_failed", "FrameworkBugError")


# --- RENDER phase ------------------------------------------------------------


def test_a_tier1_error_raised_while_rendering_aborts_with_its_class_only() -> None:
    with _raw_worker(_worker_whose_sandbox_raises_tier1) as (_, parent):
        assert _ask(parent, "{{ row.q }}", _payload(row={"q": "x"}), True) == ("render_tier1", "AuditIntegrityError")
    with _serving(_worker_whose_sandbox_raises_tier1), pytest.raises(FrameworkBugError) as caught:
        SandboxedTemplate("{{ row.q }}").render(row={"q": "x"})
    assert str(caught.value) == "Template rendering raised a Tier-1 error: AuditIntegrityError"


@pytest.mark.parametrize(
    ("source", "row", "cls"),
    [
        pytest.param("{{ row.k.format(1) }}", {"k": "x {" + _SENTINEL + "}"}, "KeyError", id="format-named-field"),
        pytest.param("{{ row.k.format(1) }}", {"k": "{5}"}, "IndexError", id="format-index"),
        pytest.param("{{ row.q | dictsort }}", {"q": _SENTINEL}, "AttributeError", id="dictsort-on-str"),
        pytest.param("{{ row.q | truncate(row.n) }}", {"q": _SENTINEL, "n": 1}, "AssertionError", id="truncate-row-length"),
        pytest.param("{% macro f(n) %}{{ f(n + 1) }}{% endmacro %}{{ f(row.n) }}", {"n": 1}, "RecursionError", id="macro-recursion"),
    ],
)
def test_any_other_render_failure_is_the_rows_and_names_only_its_class(source: str, row: dict[str, object], cls: str) -> None:
    assert _render_error(source, row=row) == f"Template rendering failed: {cls} {_WITHHELD}"
    # The reply carries the class alone even where the worker may quote
    # messages (value_free=False), because nothing bounds what it quotes.
    with _raw_worker() as (_, parent):
        assert _ask(parent, source, _payload(row=row), False) == (cls, cls)


def test_a_fixed_row_with_an_extra_key_fails_as_the_row_s_keyerror() -> None:
    """S0 residual: TemplateRow iterates data keys but resolves through the name index."""
    from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract

    contract = SchemaContract(mode="FIXED", fields=(FieldContract("q", "q", str, True, "declared"),), locked=True)
    row = PipelineRow({"q": "x", _SENTINEL: 1}, contract)
    assert _render_error("{{ row == row }}", row=row) == f"Template rendering failed: KeyError {_WITHHELD}"


@pytest.mark.parametrize(
    ("source", "cls", "expected"),
    [
        pytest.param("{{ row.q.missing }}", "UndefinedError", f"Undefined variable: UndefinedError {_WITHHELD}", id="foreign-undefined"),
        pytest.param("{{ row.q.unsafe }}", "SecurityError", f"Sandbox violation: SecurityError {_WITHHELD}", id="foreign-security"),
    ],
)
def test_a_foreign_jinja_error_is_reported_by_class_at_the_worker_status(source: str, cls: str, expected: str) -> None:
    with _raw_worker(_worker_whose_sandbox_raises_foreign) as (_, parent):
        assert _ask(parent, source, _payload(row={"q": "x"}), True) == (cls, cls)
    with _serving(_worker_whose_sandbox_raises_foreign), pytest.raises(TemplateError) as caught:
        SandboxedTemplate(source).render(row={"q": "x"})
    assert str(caught.value) == expected


def test_the_exact_type_key_gate_holds_at_the_worker_status() -> None:
    """A bool key equals the int literal 1 but is not template text: it stays unspelled."""
    with _raw_worker() as (_, parent):
        status, value = _ask(parent, "{{ 1 }}{{ row.lst[row.b] }}", _payload(row={"lst": [], "b": True}), True)
    assert (status, value) == ("safe_undefined", "list object has no element <a key the template does not spell out>")


def test_a_numeric_part_of_a_dotted_literal_prints_as_template_text() -> None:
    """Jinja looks ``'a.0'`` up part by part, the digit part as int 0 (R5 minor)."""
    assert _render_error("{{ [row.k] | map(attribute='a.0') | join }}", row={"k": {"a": []}}) == (
        "Undefined variable: list object has no element 0"
    )


@pytest.mark.parametrize("part", ["²", "9" * 5000], ids=["non-decimal-digit", "beyond-int-conversion-limit"])
def test_a_digit_part_int_refuses_is_the_row_s_failure_not_a_setup_failure(part: str) -> None:
    """Jinja's own ``int()`` of such a part fails at render; the worker's literal scan must not fail first."""
    source = "{{ [row.k] | map(attribute='a." + part + "') | join }}"
    assert _render_error(source, row={"k": {"a": []}}) == f"Template rendering failed: ValueError {_WITHHELD}"


def test_a_tier1_error_that_is_also_a_routed_class_is_never_routed(monkeypatch: pytest.MonkeyPatch) -> None:
    """``SandboxedTemplate.render`` re-raises Tier-1 before its routed arms.

    No registered Tier-1 class inherits a class those arms catch today, so the
    ordering is proven with one registered for the test: a Tier-1 that is also
    a ValueError, raised on the parent side (where packing and the worker
    exchange run), must abort, not become ``Template rendering failed``.
    """
    from elspeth.contracts import tier_registry

    with monkeypatch.context() as isolated:
        isolated.setattr(tier_registry, "_REGISTRY", list(tier_registry._REGISTRY))
        isolated.setattr(tier_registry, "_REASONS", dict(tier_registry._REASONS))
        isolated.setattr(tier_registry, "_FROZEN", False)

        @tier_registry.tier_1_error(reason="test: a Tier-1 that is also a routed class", caller_module=__name__)
        class Tier1ValueError(ValueError):
            pass

        def failing_exchange(source: str, payload: bytes, *, value_free: bool = False) -> str:
            raise Tier1ValueError("owned code failed")

        isolated.setattr(templates, "_run_template_worker", failing_exchange)
        with pytest.raises(Tier1ValueError):
            SandboxedTemplate("{{ row.q }}").render(row={"q": "x"})


# --- worker deaths -----------------------------------------------------------


def test_running_out_of_cpu_is_routed_under_its_own_reason() -> None:
    source = "{% for i in range(row.big) %}{% for j in range(row.big) %}{% endfor %}{% endfor %}x"
    # RLIMIT_CPU (2 s) normally ends it; the 5 s wall clock only on a starved host.
    with pytest.raises(TemplateError, match=r"^Template exceeded the (CPU|execution time) limit$"):
        SandboxedTemplate(source).render(row={"big": 100000})


def test_a_worker_stopped_by_another_signal_names_the_signal_only() -> None:
    with _serving(_worker_killed_by_signal), pytest.raises(TemplateError) as caught:
        SandboxedTemplate("{{ row.q }}").render(row={"q": "x"})
    assert str(caught.value) == f"Template worker was stopped by signal {int(signal.SIGTERM)}"


def test_a_worker_ending_without_a_reply_or_a_signal_is_a_framework_bug() -> None:
    with _serving(_worker_exiting_without_reply), pytest.raises(FrameworkBugError) as caught:
        SandboxedTemplate("{{ row.q }}").render(row={"q": "x"})
    assert str(caught.value) == "Template worker ended without a reply (exit status 0)"


def test_a_malformed_request_ends_the_worker_quietly_with_status_1(capfd: pytest.CaptureFixture[str]) -> None:
    with _raw_worker() as (process, parent):
        parent.send(("only one element",))
        process.join(60)
        assert process.exitcode == 1
    time.sleep(0.5)
    assert capfd.readouterr().err == ""


def test_a_non_string_ok_reply_is_a_protocol_fault() -> None:
    with _serving(_worker_replying_a_non_string), pytest.raises(FrameworkBugError) as caught:
        SandboxedTemplate("{{ row.q }}").render(row={"q": "x"})
    assert str(caught.value) == "Template worker returned a non-string result"


# --- the Undefined contract tripwire, end to end ------------------------------


def test_the_parent_maps_a_broken_undefined_contract_to_the_tier1_error() -> None:
    """The parent half of the tripwire (rebase review finding 1): the worker's
    ``undefined_contract`` status becomes ``_UndefinedContractError``, never a routed row error."""
    expected = pytest.raises(_UndefinedContractError, match="unexpected exception type TemplateRuntimeError")
    with _serving(_worker_with_a_foreign_undefined_contract), expected as caught:
        SandboxedTemplate("{{ row.missing }}").render(row={})
    assert isinstance(caught.value, TIER_1_ERRORS)


# --- nothing reaches stderr (B9) ---------------------------------------------


@pytest.mark.parametrize(
    ("source", "row"),
    [
        pytest.param("{{ row.k.format(1) }}", {"k": "x {" + _SENTINEL + "}"}, id="format-keyerror-quoting-the-row"),
        pytest.param("{{ row.q | dictsort }}", {"q": _SENTINEL}, id="attribute-error"),
        pytest.param("{{ row.q | truncate(row.n) }}", {"q": _SENTINEL, "n": 1}, id="assertion-error"),
    ],
)
def test_a_failing_render_prints_nothing_even_when_the_worker_outlives_its_reply(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], source: str, row: dict[str, object]
) -> None:
    """The panel's B9: with the parent's kill delayed (a loaded host), a child
    traceback printed the row's value to the inherited stderr. The worker
    now replies instead of raising, so it has nothing to print."""
    templates._stop_template_workers()
    original_kill = _SPAWN.Process.kill

    def slow_kill(self: multiprocessing.process.BaseProcess) -> None:
        time.sleep(1.0)
        original_kill(self)

    monkeypatch.setattr(_SPAWN.Process, "kill", slow_kill)
    try:
        with pytest.raises(TemplateError):
            SandboxedTemplate(source).render(row=row)
    finally:
        templates._stop_template_workers()
    err = capfd.readouterr().err
    assert "Traceback" not in err
    assert _SENTINEL not in err


def _render_error(source: str, **context: object) -> str:
    with pytest.raises(TemplateError) as caught:
        SandboxedTemplate(source).render(**context)
    return str(caught.value)
