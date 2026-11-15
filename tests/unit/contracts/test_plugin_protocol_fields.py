"""Tests that the Phase-7A reference-content fields are declared on all
four plugin protocol classes in contracts/plugin_protocols.py.

Walks ``cls.__mro__`` and unions each base's own annotations rather
than calling ``typing.get_type_hints()`` or ``hasattr()``.
``plugin_protocols.py`` defers context types (``PluginSchema``,
``SourceContext``, ``TransformContext``, ``SinkContext``, etc.) into
TYPE_CHECKING blocks; ``typing.get_type_hints()`` would attempt to
resolve every annotation at runtime and raise ``NameError`` on those
forward references — the test would error rather than assert, and
would never pass in green state. ``hasattr()`` is unconditionally banned
(``engine-patterns-reference`` skill §hasattr Alternatives).
``SourceProtocol`` and ``SinkProtocol`` are not
``@runtime_checkable``, ruling out ``isinstance()``.

The MRO walk is the runtime-safe equivalent of structural inheritance:
the Phase-7A reference-content fields now live on the
``_PluginReferenceContent`` mixin and are inherited by each protocol.
The test verifies inheritance by union'ing annotations across every
base in the MRO, which mirrors how a static checker (mypy) resolves
inherited attributes for a Protocol's structural contract.
"""

from __future__ import annotations

import annotationlib

from elspeth.contracts.plugin_protocols import (
    BatchTransformProtocol,
    SinkProtocol,
    SourceProtocol,
    TransformProtocol,
)

_PHASE_7A_FIELDS = {
    "usage_when_to_use",
    "usage_when_not_to_use",
    "example_use",
    "capability_tags",
    "audit_characteristics",
}


def _collect_inherited_annotations(cls: type) -> set[str]:
    """Union each MRO entry's own annotations.

    ``annotationlib.get_annotations`` returns only the class's own
    annotations, never a base's, so contributions are strictly per-class.
    Since Python 3.14 (PEP 649) a class no longer stores ``__annotations__``
    in its ``__dict__``; the interpreter keeps an ``__annotate__`` function
    instead, so a ``vars(base)`` probe would find nothing. ``FORWARDREF``
    format leaves the TYPE_CHECKING-only names unevaluated rather than
    raising ``NameError``; only the keys are read.

    Walking the MRO and union'ing reproduces what a structural
    type-checker sees as the protocol's full attribute set, including
    names contributed by mixin bases like ``_PluginReferenceContent``.
    """
    names: set[str] = set()
    for base in cls.__mro__:
        names |= set(annotationlib.get_annotations(base, format=annotationlib.Format.FORWARDREF))
    return names


def _assert_protocol_has_fields(protocol: type, protocol_name: str) -> None:
    hints = _collect_inherited_annotations(protocol)
    missing = _PHASE_7A_FIELDS - hints
    assert not missing, (
        f"{protocol_name} is missing Phase-7A fields: {sorted(missing)}. "
        f"They should be inherited from _PluginReferenceContent in "
        f"src/elspeth/contracts/plugin_protocols.py."
    )


def test_source_protocol_has_phase_7a_fields() -> None:
    _assert_protocol_has_fields(SourceProtocol, "SourceProtocol")


def test_transform_protocol_has_phase_7a_fields() -> None:
    _assert_protocol_has_fields(TransformProtocol, "TransformProtocol")


def test_batch_transform_protocol_has_phase_7a_fields() -> None:
    _assert_protocol_has_fields(BatchTransformProtocol, "BatchTransformProtocol")


def test_sink_protocol_has_phase_7a_fields() -> None:
    _assert_protocol_has_fields(SinkProtocol, "SinkProtocol")


def _collect_inherited_methods(cls: type) -> set[str]:
    """Union non-dunder callables (incl. class/staticmethods) across the MRO."""
    names: set[str] = set()
    for base in cls.__mro__:
        names |= {
            name
            for name, value in vars(base).items()
            if not name.startswith("__") and (callable(value) or isinstance(value, (classmethod, staticmethod)))
        }
    return names


def test_batch_transform_protocol_mirrors_transform_members() -> None:
    """BatchTransformProtocol carries every TransformProtocol member.

    elspeth-8783933d99: the two protocols must not diverge on data members —
    a member added to TransformProtocol but not mirrored here leaves the
    batch surface describing a narrower contract than every conformer (all
    BaseTransform subclasses) actually carries. Derived from
    TransformProtocol, never restated as a hand-written list.
    """
    missing_annotations = _collect_inherited_annotations(TransformProtocol) - _collect_inherited_annotations(BatchTransformProtocol)
    assert not missing_annotations, f"BatchTransformProtocol is missing data members: {sorted(missing_annotations)}"
    missing_methods = _collect_inherited_methods(TransformProtocol) - _collect_inherited_methods(BatchTransformProtocol)
    assert not missing_methods, f"BatchTransformProtocol is missing methods: {sorted(missing_methods)}"
