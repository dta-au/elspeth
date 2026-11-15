"""asyncio's exception diagnostics must reach the log without exception content.

The scenarios run real event loops and capture what the ``asyncio`` logger
emits, fully formatted (message plus traceback text). Every positive case has
a negative control proving the same scenario leaks without the scrubber, so a
clean capture cannot be an instrument that sees nothing.
"""

from __future__ import annotations

import asyncio
import functools
import gc
import logging
import threading
from collections.abc import Callable, Coroutine, Iterator
from typing import Any

import pytest
import uvloop

from elspeth.core import logging as elspeth_logging
from elspeth.core.logging import configure_logging, install_asyncio_diagnostic_scrubber

SECRET = "SECRET-PROMPT-CONTENT"

LoopFactory = Callable[[], asyncio.AbstractEventLoop] | None
Scenario = Callable[[], Coroutine[Any, Any, None]]
LOOP_FACTORIES = [pytest.param(None, id="asyncio"), pytest.param(uvloop.new_event_loop, id="uvloop")]


class _Capture(logging.Handler):
    """Collect each record as an operator's sink would render it."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.setFormatter(logging.Formatter("%(message)s"))
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@pytest.fixture
def asyncio_log() -> Iterator[_Capture]:
    """Capture the asyncio logger with the scrubber absent; restore it after."""
    logger = logging.getLogger("asyncio")
    scrubber = elspeth_logging._asyncio_diagnostic_scrubber
    was_installed = scrubber in logger.filters
    logger.removeFilter(scrubber)
    capture = _Capture()
    logger.addHandler(capture)
    try:
        yield capture
    finally:
        logger.removeHandler(capture)
        logger.removeFilter(scrubber)
        if was_installed:
            logger.addFilter(scrubber)


async def _fail_late() -> None:
    await asyncio.sleep(0.02)
    raise RuntimeError(SECRET)


def _failing_callback(argument: str) -> None:
    raise RuntimeError(SECRET)


async def _shielded_task_fails_after_awaiter_cancelled() -> None:
    task = asyncio.create_task(_fail_late())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), 0.005)
    await asyncio.sleep(0.05)
    # The outcome is retrieved, as most production sites do; 3.14 logs anyway.
    assert isinstance(task.exception(), RuntimeError)


async def _task_failure_never_retrieved() -> None:
    task = asyncio.create_task(_fail_late())
    await asyncio.sleep(0.05)
    del task
    gc.collect()


async def _loop_callback_fails() -> None:
    asyncio.get_running_loop().call_soon(functools.partial(_failing_callback, SECRET))
    await asyncio.sleep(0.01)


SCENARIOS = [
    pytest.param(_shielded_task_fails_after_awaiter_cancelled, id="shielded-late-failure"),
    pytest.param(_task_failure_never_retrieved, id="never-retrieved"),
    pytest.param(_loop_callback_fails, id="callback-failure"),
]


def _run(scenario: Scenario, loop_factory: LoopFactory) -> None:
    asyncio.run(scenario(), loop_factory=loop_factory)
    gc.collect()


@pytest.mark.parametrize("loop_factory", LOOP_FACTORIES)
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_diagnostic_is_reduced_to_class_and_location(asyncio_log: _Capture, scenario: Scenario, loop_factory: LoopFactory) -> None:
    install_asyncio_diagnostic_scrubber()

    _run(scenario, loop_factory)

    rendered = "\n".join(asyncio_log.lines)
    assert SECRET not in rendered
    diagnostics = [line for line in asyncio_log.lines if line.startswith("asyncio_unhandled_exception ")]
    assert len(diagnostics) == 1
    assert "exc_class=RuntimeError" in diagnostics[0]
    # Where it failed survives: this file and the raising function.
    assert __file__ in diagnostics[0]
    assert ":_fail_late" in diagnostics[0] or ":_failing_callback" in diagnostics[0]


@pytest.mark.parametrize("loop_factory", LOOP_FACTORIES)
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_negative_control_same_scenario_leaks_without_the_scrubber(
    asyncio_log: _Capture, scenario: Scenario, loop_factory: LoopFactory
) -> None:
    _run(scenario, loop_factory)

    assert SECRET in "\n".join(asyncio_log.lines)


@pytest.mark.parametrize("loop_factory", LOOP_FACTORIES)
def test_loops_nothing_was_installed_on_are_covered(asyncio_log: _Capture, loop_factory: LoopFactory) -> None:
    """The scrubber is process-wide: a loop started elsewhere, on another thread, is covered too."""
    install_asyncio_diagnostic_scrubber()

    thread = threading.Thread(target=_run, args=(_task_failure_never_retrieved, loop_factory))
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive()

    rendered = "\n".join(asyncio_log.lines)
    assert SECRET not in rendered
    assert "asyncio_unhandled_exception exc_class=RuntimeError" in rendered


def test_records_without_an_exception_pass_unchanged(asyncio_log: _Capture) -> None:
    install_asyncio_diagnostic_scrubber()

    logging.getLogger("asyncio").warning("Executing %s took %.3f seconds", "<Handle>", 0.25)

    assert asyncio_log.lines == ["Executing <Handle> took 0.250 seconds"]


def test_install_is_idempotent_and_configure_logging_installs_it(asyncio_log: _Capture) -> None:
    logger = logging.getLogger("asyncio")
    scrubber = elspeth_logging._asyncio_diagnostic_scrubber
    assert scrubber not in logger.filters

    configure_logging()
    install_asyncio_diagnostic_scrubber()

    assert logger.filters.count(scrubber) == 1


@pytest.mark.parametrize("value", ["true", "TRUE", "True"])
def test_litellm_json_logs_mode_is_refused(asyncio_log: _Capture, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """LiteLLM's JSON_LOGS handlers log str(exception) through a sink no filter reaches."""
    monkeypatch.setenv("JSON_LOGS", value)

    with pytest.raises(RuntimeError, match="JSON_LOGS=true is not supported"):
        install_asyncio_diagnostic_scrubber()
    with pytest.raises(RuntimeError, match="JSON_LOGS=true is not supported"):
        configure_logging()
    assert elspeth_logging._asyncio_diagnostic_scrubber not in logging.getLogger("asyncio").filters


@pytest.mark.parametrize("value", ["false", "0", ""])
def test_values_litellm_does_not_treat_as_enabled_are_accepted(asyncio_log: _Capture, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("JSON_LOGS", value)

    install_asyncio_diagnostic_scrubber()

    assert elspeth_logging._asyncio_diagnostic_scrubber in logging.getLogger("asyncio").filters
