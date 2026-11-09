"""Value-free vendor diagnostics scoped to Power Automate authentication.

Azure identity logs acquisition failures before returning them to the caller.
The public record factory seam protects every handler, including foreign
handlers, before exception objects or positional arguments can be formatted.
The factory is installed permanently; a ContextVar selects only the current
Power Automate SDK call. Other plugins and concurrent threads keep their
ordinary diagnostics and logging configuration.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
from types import TracebackType
from typing import Any

_AUTH_SCOPE: ContextVar[bool] = ContextVar("power_automate_auth_vendor_diagnostics", default=False)
_INSTALL_LOCK = Lock()
_VENDOR_NAMESPACES = ("azure", "msal", "msal_extensions", "requests", "urllib3", "httpx", "httpcore")
_SAFE_EVENT = "power_automate_auth_vendor_diagnostic"
_SAFE_LOGGER = "elspeth.plugins.power_automate.auth"

_ExceptionInfo = tuple[type[BaseException], BaseException, TracebackType | None] | tuple[None, None, None] | None


class _PowerAutomateAuthRecordFactory:
    def __init__(self, previous: Callable[..., logging.LogRecord]) -> None:
        self._previous = previous

    def __call__(
        self,
        name: str | None,
        level: int | None,
        pathname: str,
        lineno: int,
        msg: object,
        args: tuple[object, ...] | Mapping[str, object] | None,
        exc_info: _ExceptionInfo,
        func: str | None = None,
        sinfo: str | None = None,
        **kwargs: Any,
    ) -> logging.LogRecord:
        # logging.makeLogRecord uses an unnamed placeholder before copying
        # an already-admitted record, as ProcessorFormatter does internally.
        if _AUTH_SCOPE.get() and name is not None and any(name == prefix or name.startswith(prefix + ".") for prefix in _VENDOR_NAMESPACES):
            # Sanitize before invoking a chained factory: it may inspect or
            # persist arguments independently of handlers and formatters.
            return self._previous(_SAFE_LOGGER, level, "power_automate_auth", 0, _SAFE_EVENT, (), None, None, None)
        return self._previous(name, level, pathname, lineno, msg, args, exc_info, func, sinfo, **kwargs)


@contextmanager
def power_automate_auth_diagnostics() -> Iterator[None]:
    """Protect constructor, get_token and close without changing logger levels."""
    with _INSTALL_LOCK:
        current = logging.getLogRecordFactory()
        if not isinstance(current, _PowerAutomateAuthRecordFactory):
            logging.setLogRecordFactory(_PowerAutomateAuthRecordFactory(current))
    token = _AUTH_SCOPE.set(True)
    try:
        yield
    finally:
        _AUTH_SCOPE.reset(token)
