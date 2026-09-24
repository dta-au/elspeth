"""An interrupted pipe exchange must never supply another row's output."""

import queue
from collections.abc import Iterator

import pytest
from jinja2.exceptions import UndefinedError

from elspeth.plugins.infrastructure import templates


@pytest.fixture
def single_worker(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    available: queue.SimpleQueue[int] = queue.SimpleQueue()
    available.put(0)
    monkeypatch.setattr(templates, "_AVAILABLE_WORKERS", available)
    monkeypatch.setattr(templates, "_WORKERS", [None])
    try:
        yield
    finally:
        templates._stop_template_workers()


@pytest.mark.usefixtures("single_worker")
@pytest.mark.parametrize("phase", ["send", "poll", "recv"])
@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_interrupted_exchange_cannot_return_previous_row(
    monkeypatch: pytest.MonkeyPatch, phase: str, interruption: type[BaseException]
) -> None:
    template = templates.create_sandboxed_environment().from_string("{{ row.text }}")
    assert template.render(row={"text": "WARMUP"}) == "WARMUP"
    entry = templates._WORKERS[0]
    assert entry is not None
    process, connection = entry
    original_send = connection.send

    def interrupted_send(request: object) -> None:
        original_send(request)
        raise interruption("cancelled after sending")

    def interrupted_poll(timeout: float) -> bool:
        raise interruption("cancelled before polling")

    def interrupted_recv() -> object:
        raise interruption("cancelled before receiving")

    with monkeypatch.context() as exchange_patch:
        if phase == "send":
            exchange_patch.setattr(connection, "send", interrupted_send)
        elif phase == "poll":
            exchange_patch.setattr(connection, "poll", interrupted_poll)
        else:
            exchange_patch.setattr(connection, "recv", interrupted_recv)
        with pytest.raises(interruption):
            template.render(row={"text": "CANCELLED_ROW"})

    for text in ("NEW_ROW_1", "NEW_ROW_2"):
        assert template.render(row={"text": text}) == text
    assert not process.is_alive()


@pytest.mark.usefixtures("single_worker")
def test_consumed_error_response_allows_worker_reuse() -> None:
    template = templates.create_sandboxed_environment().from_string("{{ row.text }}")
    assert template.render(row={"text": "WARMUP"}) == "WARMUP"
    entry = templates._WORKERS[0]
    assert entry is not None
    process, _ = entry

    with pytest.raises(UndefinedError):
        template.render(row={})

    assert template.render(row={"text": "NEXT_ROW"}) == "NEXT_ROW"
    assert templates._WORKERS[0] is entry
    assert process.is_alive()
