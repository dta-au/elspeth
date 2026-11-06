"""Nominal borrowed JSON projections for validation before materialization.

These views grant no authority and promise no snapshot.  Their iterators expose
the prospective JSON values without allocating a complete projected container;
the consumer must validate and detach before retaining or dispatching them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping

type JsonValue = None | bool | int | float | str | Mapping[str, JsonValue] | list[JsonValue] | tuple[JsonValue, ...]


class ReadonlyJsonArray(ABC):
    """An owned array projection with the original element order and width."""

    @abstractmethod
    def __len__(self) -> int:
        """Return the projected array's width."""

    @abstractmethod
    def __iter__(self) -> Iterator[object]:
        """Read projected values without constructing the complete array."""


class ReadonlyJsonString(ABC):
    """Owned sizing projection; never a materialized provider wire value."""

    @abstractmethod
    def canonical_string_bytes(self, *, limit: int) -> int:
        """Validate the complete projection and return its size or limit+1."""


class ReadonlyJsonObject(ABC):
    """An owned object projection, including exact projected key presence."""

    @abstractmethod
    def __len__(self) -> int:
        """Return the projected object's width."""

    @abstractmethod
    def __iter__(self) -> Iterator[object]:
        """Read projected keys in their original order."""

    @abstractmethod
    def __getitem__(self, key: object) -> object:
        """Read a present projected value or raise KeyError."""

    def items(self) -> Iterator[tuple[object, object]]:
        """Read each projected pair without constructing a dictionary."""
        for key in self:
            yield key, self[key]
