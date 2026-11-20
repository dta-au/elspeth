import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture(autouse=True)
def _restore_gateway_logger() -> Iterator[None]:
    """``build()`` configures the process-wide gateway logger; undo it after each test.

    Without this, a test that calls ``build()`` would leave a stderr handler
    and ``propagate = False`` behind, and a later ``caplog`` test on the
    same logger would see no records.
    """
    gateway_logger = logging.getLogger("elspeth_llm_gateway")
    handlers = list(gateway_logger.handlers)
    level = gateway_logger.level
    propagate = gateway_logger.propagate
    yield
    gateway_logger.handlers[:] = handlers
    gateway_logger.setLevel(level)
    gateway_logger.propagate = propagate
