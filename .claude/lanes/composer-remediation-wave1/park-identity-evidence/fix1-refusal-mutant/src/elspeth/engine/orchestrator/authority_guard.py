"""Preserve caller ownership refusal across pipeline error wrapping."""

from collections.abc import Callable


class CallerAuthorityGuard:
    """Latch caller refusal separately from processing failures.

    Ownership loss is not evidence that the pipeline failed. The lifecycle
    stops its heartbeat and relinquishes its seat before propagating the
    original refusal, leaving the successor to reconcile durable work.
    """

    def __init__(self, callback: Callable[[], None] | None) -> None:
        self._callback = callback
        self._failure: BaseException | None = None

    def check(self) -> None:
        if self._failure is not None:
            raise self._failure
        if self._callback is not None:
            try:
                self._callback()
            except BaseException as exc:
                self._failure = exc
                raise

    def release_on_loss(self, release: Callable[[], object]) -> None:
        """Call only after stopping heartbeat; never synthesize a terminal result."""
        if self._failure is not None:
            release()
            raise self._failure
