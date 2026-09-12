"""Declaration-selected response admission, independent of disclosure policy."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import JsonValue

from elspeth.contracts.errors import FrameworkBugError


class AdmittedResponse(ABC):
    """A concrete admitted value whose encoder is bound before type erasure."""

    @abstractmethod
    def to_wire(self) -> JsonValue:
        """Encode the selected owned value into fresh JSON containers."""

    @abstractmethod
    def readmit(self, contract: ResponseContract) -> AdmittedResponse:
        """Recheck a cached value against its current declaration contract."""


class ResponseContract(ABC):
    """Nominal heterogeneous registry entry; never a disclosure permission."""

    @abstractmethod
    def admit(self, value: object) -> AdmittedResponse:
        """Reject corrupt producer data and bind its precise owned encoder."""


@dataclass(frozen=True, slots=True)
class SelectedResponseContract[T](ResponseContract):
    """Pair one producer's admission with serialization of exactly its T."""

    parse: Callable[[object], T]
    encode: Callable[[T], JsonValue]

    def admit(self, value: object) -> AdmittedResponse:
        admitted = self.parse(value)
        return _AdmittedResponse(admitted, self, type(admitted))


@dataclass(frozen=True, slots=True)
class _AdmittedResponse[T](AdmittedResponse):
    value: T
    contract: SelectedResponseContract[T]
    _canonical_root_type: type[T]

    def to_wire(self) -> JsonValue:
        return self.contract.encode(self.value)

    def readmit(self, contract: ResponseContract) -> AdmittedResponse:
        if contract is not self.contract:
            raise FrameworkBugError("Cached discovery response contract changed")
        if type(self.value) is not self._canonical_root_type:
            raise FrameworkBugError("Cached discovery response canonical root changed")
        return contract.admit(self.value)
