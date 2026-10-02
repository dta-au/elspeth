# tests/fixtures/stores.py
"""Mock stores and clocks for test isolation.

Contains MockPayloadStore and MockClock — the only canonical definitions.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from elspeth.contracts.payload_store import IntegrityError, PayloadNotFoundError, PayloadStore


class MockPayloadStore:
    """In-memory PayloadStore for testing.

    Implements PayloadStore protocol with integrity verification.
    """

    def __init__(self) -> None:
        self._storage: dict[str, bytes] = {}

    def store(self, content: bytes) -> str:
        content_hash = hashlib.sha256(content).hexdigest()
        if content_hash not in self._storage:
            self._storage[content_hash] = content
        return content_hash

    def retrieve(self, content_hash: str) -> bytes:
        if content_hash not in self._storage:
            raise PayloadNotFoundError(content_hash)
        content = self._storage[content_hash]
        actual_hash = hashlib.sha256(content).hexdigest()
        if not hmac.compare_digest(actual_hash, content_hash):
            raise IntegrityError(f"Payload integrity check failed: expected {content_hash}, got {actual_hash}")
        return content

    def retrieve_bounded(self, content_hash: str, *, max_bytes: int) -> bytes | None:
        if max_bytes < 0:
            raise ValueError("max_bytes must be nonnegative")
        content = self.retrieve(content_hash)
        return content if len(content) <= max_bytes else None

    def exists(self, content_hash: str) -> bool:
        return content_hash in self._storage

    def delete(self, content_hash: str) -> bool:
        if content_hash not in self._storage:
            return False
        del self._storage[content_hash]
        return True


class MockClock:
    """Deterministic clock for timeout testing.

    Usage:
        clock = MockClock()
        clock.advance(0.25)  # Advance 250ms
        assert clock.time() == 0.25
    """

    def __init__(self, start: float = 0.0) -> None:
        self._time = start

    def time(self) -> float:
        return self._time

    def advance(self, seconds: float) -> None:
        self._time += seconds

    def __call__(self) -> float:
        return self._time


@pytest.fixture
def payload_store() -> PayloadStore:
    """Fresh MockPayloadStore per test."""
    return MockPayloadStore()


@pytest.fixture
def mock_clock() -> MockClock:
    """Fresh MockClock per test."""
    return MockClock()
