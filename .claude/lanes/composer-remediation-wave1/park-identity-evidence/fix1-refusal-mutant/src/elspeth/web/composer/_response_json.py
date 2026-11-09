"""Finite JSON leaves for selected response contracts, with immutable storage."""

from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType

from pydantic import JsonValue

from elspeth.contracts.errors import FrameworkBugError

type FrozenResponseJSON = None | bool | int | float | str | tuple[FrozenResponseJSON, ...] | Mapping[str, FrozenResponseJSON]


def parse_response_json(value: object) -> FrozenResponseJSON:
    """Admit JSON containers only; copy mutable inputs before retaining them."""
    try:
        return _parse_response_json(value)
    except RecursionError:
        raise FrameworkBugError("Discovery response JSON nesting is invalid") from None


def _parse_response_json(value: object) -> FrozenResponseJSON:
    if value is None or type(value) is bool or type(value) is int or type(value) is str:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is list or type(value) is tuple:
        return tuple(_parse_response_json(item) for item in value)
    if type(value) is dict or type(value) is MappingProxyType:
        if any(type(key) is not str for key in value):
            raise FrameworkBugError("Discovery response JSON object has invalid keys")
        return MappingProxyType({key: _parse_response_json(item) for key, item in value.items()})
    raise FrameworkBugError("Discovery response contains a non-JSON value")


def encode_response_json(value: FrozenResponseJSON) -> JsonValue:
    """Return new mutable wire containers, preserving object insertion order."""
    if isinstance(value, Mapping):
        return {key: encode_response_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [encode_response_json(item) for item in value]
    return value


def parse_frozen_response_json(value: object) -> FrozenResponseJSON:
    """Recheck an owned cached leaf without repairing mutable corruption."""
    try:
        return _parse_frozen_response_json(value)
    except RecursionError:
        raise FrameworkBugError("Cached discovery response JSON nesting is invalid") from None


def _parse_frozen_response_json(value: object) -> FrozenResponseJSON:
    if type(value) is list or type(value) is dict:
        raise FrameworkBugError("Cached discovery response contains mutable JSON containers")
    if type(value) is tuple:
        return tuple(_parse_frozen_response_json(item) for item in value)
    if type(value) is MappingProxyType:
        if any(type(key) is not str for key in value):
            raise FrameworkBugError("Cached discovery response JSON object has invalid keys")
        return MappingProxyType({key: _parse_frozen_response_json(item) for key, item in value.items()})
    return parse_response_json(value)
