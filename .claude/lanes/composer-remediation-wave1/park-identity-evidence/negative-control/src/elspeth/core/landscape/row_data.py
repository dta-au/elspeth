"""Payload retrieval result types with explicit state discrimination.

Replaces ambiguous `dict | None` returns from get_row_data() and
get_call_response_data() with explicit state handling. Callers match
on state instead of guessing why data is None.

Row data states:
    AVAILABLE: Data was found and returned
    REPR_FALLBACK: Data was quarantined and stored as lossy repr snapshot
        (original contained non-canonical values like NaN/Infinity)
    PURGED: Data existed but was deleted (retention policy)
    NEVER_STORED: Row exists but source_data_ref was never set
    STORE_NOT_CONFIGURED: No payload store configured
    ROW_NOT_FOUND: Row ID doesn't exist in the database

Call data states:
    AVAILABLE: Response data was found and returned
    PURGED: Response existed but was deleted (retention policy)
    HASH_ONLY: Response was recorded (hash exists) but payload was never persisted
    NEVER_STORED: Call exists but no response was ever recorded (hash and ref both NULL)
    STORE_NOT_CONFIGURED: No payload store configured
    CALL_NOT_FOUND: Call ID doesn't exist in the database
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, ClassVar

from elspeth.contracts.freeze import freeze_fields


class RowDataState(StrEnum):
    """Discriminator for row data retrieval results.

    Inherits from str for JSON serialization compatibility.
    """

    AVAILABLE = "available"
    REPR_FALLBACK = "repr_fallback"
    PURGED = "purged"
    NEVER_STORED = "never_stored"
    STORE_NOT_CONFIGURED = "store_not_configured"
    ROW_NOT_FOUND = "row_not_found"


@dataclass(frozen=True, slots=True)
class RowDataResult:
    """Result of a row data retrieval with explicit state.

    Invariants:
        - AVAILABLE and REPR_FALLBACK states require dict data
        - All other states require None data

    Example:
        result = recorder.get_row_data(row_id)
        match result.state:
            case RowDataState.AVAILABLE:
                process(result.data)  # Safe - guaranteed non-None
            case RowDataState.REPR_FALLBACK:
                log.warning("Data is lossy repr snapshot", data=result.data)
            case RowDataState.PURGED:
                log.info("Data was purged per retention policy")
            case RowDataState.ROW_NOT_FOUND:
                raise KeyError(f"Unknown row: {row_id}")
    """

    state: RowDataState
    data: Mapping[str, Any] | None

    _STATES_WITH_DATA: ClassVar[frozenset[RowDataState]] = frozenset({RowDataState.AVAILABLE, RowDataState.REPR_FALLBACK})

    def __post_init__(self) -> None:
        if type(self.state) is not RowDataState:
            raise TypeError(f"state must be RowDataState, got {type(self.state).__name__}")
        if self.state in self._STATES_WITH_DATA:
            if self.data is None:
                raise ValueError(f"{self.state} state requires non-None data")
            payload: object = self.data
            match payload:
                case dict() | MappingProxyType():
                    pass
                case _:
                    actual_type = type(payload).__name__
                    raise TypeError(f"{self.state} state requires dict data, got {actual_type}")
            freeze_fields(self, "data")
        if self.state not in self._STATES_WITH_DATA and self.data is not None:
            raise ValueError(f"{self.state} state requires None data")


class CallDataState(StrEnum):
    """Discriminator for call response data retrieval results.

    Mirrors RowDataState but without REPR_FALLBACK (call responses
    are not quarantined) and with CALL_NOT_FOUND instead of ROW_NOT_FOUND.
    """

    AVAILABLE = "available"
    PURGED = "purged"
    HASH_ONLY = "hash_only"
    NEVER_STORED = "never_stored"
    STORE_NOT_CONFIGURED = "store_not_configured"
    CALL_NOT_FOUND = "call_not_found"


@dataclass(frozen=True, slots=True)
class CallDataResult:
    """Result of a call response data retrieval with explicit state.

    Invariants:
        - AVAILABLE state requires dict data
        - All other states require None data

    Example:
        result = recorder.get_call_response_data(call_id)
        match result.state:
            case CallDataState.AVAILABLE:
                process(result.data)  # Safe - guaranteed non-None
            case CallDataState.PURGED:
                log.info("Response purged per retention policy")
            case CallDataState.NEVER_STORED:
                log.info("Call had no response body")
            case CallDataState.STORE_NOT_CONFIGURED:
                raise ConfigError("Payload store required for replay")
            case CallDataState.CALL_NOT_FOUND:
                raise KeyError(f"Unknown call: {call_id}")
    """

    state: CallDataState
    data: Mapping[str, Any] | None

    _STATES_WITH_DATA: ClassVar[frozenset[CallDataState]] = frozenset({CallDataState.AVAILABLE})

    def __post_init__(self) -> None:
        if type(self.state) is not CallDataState:
            raise TypeError(f"state must be CallDataState, got {type(self.state).__name__}")
        if self.state in self._STATES_WITH_DATA:
            if self.data is None:
                raise ValueError(f"{self.state} state requires non-None data")
            payload: object = self.data
            match payload:
                case dict() | MappingProxyType():
                    pass
                case _:
                    actual_type = type(payload).__name__
                    raise TypeError(f"{self.state} state requires dict data, got {actual_type}")
            freeze_fields(self, "data")
        if self.state not in self._STATES_WITH_DATA and self.data is not None:
            raise ValueError(f"{self.state} state requires None data")
