"""Type-aware bucket helpers for batch transform category values."""

from __future__ import annotations

from collections.abc import Hashable, Iterable

type ScalarBucketKey = tuple[type[object], object]


def scalar_bucket_key(value: object) -> ScalarBucketKey:
    """Return a hashable key that keeps equal cross-type scalars separate."""
    return (type(value), value)


def hashable_scalar_bucket_key(value: object) -> ScalarBucketKey | None:
    """Return a type-aware bucket key when ``value`` can safely enter a set/dict."""
    if not isinstance(value, Hashable):
        return None
    return scalar_bucket_key(value)


def same_scalar_bucket_value(left: object, right: object) -> bool:
    """Compare bucket values without merging ``True``/``1`` or ``False``/``0``."""
    return type(left) is type(right) and left == right


def scalar_bucket_contains(values: Iterable[object], candidate: object) -> bool:
    """Return whether ``candidate`` is already present under type-aware equality."""
    return any(same_scalar_bucket_value(value, candidate) for value in values)


def append_unique_bucket_value[T](values: list[T], candidate: T) -> None:
    """Append ``candidate`` only if no type-aware bucket match already exists."""
    if not scalar_bucket_contains(values, candidate):
        values.append(candidate)


def append_unique_bucket_value_with_seen[T](
    values: list[T],
    candidate: T,
    *,
    seen_hashable: set[ScalarBucketKey],
    seen_unhashable: list[T],
) -> None:
    """Append ``candidate`` once using O(1) tracking for hashable bucket values."""
    key = hashable_scalar_bucket_key(candidate)
    if key is not None:
        if key in seen_hashable:
            return
        seen_hashable.add(key)
        values.append(candidate)
        return

    if scalar_bucket_contains(seen_unhashable, candidate):
        return
    seen_unhashable.append(candidate)
    values.append(candidate)


def unique_bucket_values[T](candidates: Iterable[T]) -> list[T]:
    """Return first-seen unique values under type-aware scalar equality."""
    values: list[T] = []
    seen_hashable: set[ScalarBucketKey] = set()
    seen_unhashable: list[T] = []
    for candidate in candidates:
        append_unique_bucket_value_with_seen(
            values,
            candidate,
            seen_hashable=seen_hashable,
            seen_unhashable=seen_unhashable,
        )
    return values
