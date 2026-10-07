"""Validate telemetry reservation dispatch and physical custody before effects.

Validate before bootstrap's first assert_process. Preserve installation setters,
getters and cleanup overrides; only process/reservation custody must stay owned.
"""

from _thread import RLock
from collections.abc import Callable
from types import MemberDescriptorType, MethodType
from typing import Literal

from elspeth.web.operator_telemetry_custody import OperatorTelemetryCleanupOwner, TelemetryCustodyUnresolved
from elspeth.web.operator_telemetry_installation import TelemetryInstallation, TelemetryInstallationRecord


def _class_descriptor(cls: type, name: str) -> object:
    for base in cls.__mro__:
        namespace = vars(base)
        if name in namespace:
            return namespace[name]
    raise TelemetryCustodyUnresolved("Telemetry custody descriptor is missing")


def _normal_lookup(value: object) -> type:
    cls = type(value)
    if (
        type(cls) is not type
        or _class_descriptor(cls, "__getattribute__") is not object.__getattribute__
        or _class_descriptor(cls, "__setattr__") is not object.__setattr__
        or any("__getattr__" in vars(base) for base in cls.__mro__)
        or any("__dict__" in vars(base) for base in cls.__mro__)
    ):
        raise TelemetryCustodyUnresolved("Telemetry custody has non-owned attribute dispatch")
    return cls


def _owned_field(value: object, name: str, owner_class: type) -> object:
    cls = _normal_lookup(value)
    expected = vars(owner_class).get(name)
    if type(expected) is not MemberDescriptorType or _class_descriptor(cls, name) is not expected:
        raise TelemetryCustodyUnresolved("Telemetry custody field descriptor was replaced")
    return expected.__get__(value, cls)


def _owned_method(value: object, name: Literal["reserve", "assert_process"], expected: Callable[..., object]) -> None:
    cls = _normal_lookup(value)
    # Reading the MRO dictionaries avoids invoking a supplied descriptor.
    descriptor = _class_descriptor(cls, name)
    if descriptor is not expected:
        raise TelemetryCustodyUnresolved("Telemetry custody method was replaced")
    selected = object.__getattribute__(value, name)
    if type(selected) is not MethodType or selected.__self__ is not value or selected.__func__ is not expected:
        raise TelemetryCustodyUnresolved("Telemetry custody bound method was replaced")


def validate_telemetry_reservation_owner(owner: OperatorTelemetryCleanupOwner) -> TelemetryInstallation:
    cls = type(owner)
    if type(cls) is not type or not issubclass(cls, OperatorTelemetryCleanupOwner):
        raise TypeError("Telemetry bootstrap requires the lexical application owner")
    cls = _normal_lookup(owner)
    _owned_method(owner, "assert_process", OperatorTelemetryCleanupOwner.assert_process)
    installation = _owned_field(owner, "installation", OperatorTelemetryCleanupOwner)
    owner_pid = _owned_field(owner, "creator_pid", OperatorTelemetryCleanupOwner)
    installation_cls = _normal_lookup(installation)
    if not issubclass(installation_cls, TelemetryInstallation):
        raise TelemetryCustodyUnresolved("Telemetry installation is foreign")
    if not isinstance(installation, TelemetryInstallation):
        raise TelemetryCustodyUnresolved("Telemetry installation is foreign")
    installation_pid = _owned_field(installation, "creator_pid", TelemetryInstallation)
    lock = _owned_field(installation, "lock", TelemetryInstallation)
    record = _owned_field(installation, "record", TelemetryInstallation)
    if type(owner_pid) is not int or type(installation_pid) is not int:
        raise TelemetryCustodyUnresolved("Telemetry process identity is foreign")
    if type(lock) is not RLock:
        raise TelemetryCustodyUnresolved("Telemetry reservation lock is foreign")
    if record is not None and (type(record) is not TelemetryInstallationRecord or type(record.state) is not str):
        raise TelemetryCustodyUnresolved("Telemetry reservation record is foreign")
    _owned_method(installation, "assert_process", TelemetryInstallation.assert_process)
    _owned_method(installation, "reserve", TelemetryInstallation.reserve)
    return installation
