"""Deferred, test-only occurrence accounting for complete continuation evidence.

This observer neither admits evidence nor changes the production byte budget.
Its report describes wire occurrences, with overlapping parent/child and owner
attribution; it is not a closure census or a bound on memory or runtime work.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from time import perf_counter
from typing import Literal
from uuid import UUID

import pytest

from elspeth.contracts import bounded_serialization
from elspeth.contracts.bounded_serialization import BoundedCanonicalSize, utf8_size
from elspeth.web.composer.advisor_output import AdvisorAbandonmentResponse
from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES
from elspeth.web.composer.continuation import ContinuationValue, PrivateChunkRef
from elspeth.web.composer.continuation_evidence import TurnEvidenceSnapshot
from elspeth.web.composer.continuation_retention import _RetainedArrayView, _RetainedModelView, _RetainedObjectView

# Save before a fixture installs the observer; scalar measurements never recurse
# through the installed wrapper.
_REAL_COUNTER = bounded_serialization.bounded_canonical_json_size

type Counter = Callable[..., BoundedCanonicalSize]


@dataclass(frozen=True)
class FieldBytes:
    path: str
    value_bytes: int
    key_bytes: int
    colon_bytes: int
    comma_bytes: int
    raw_utf8_bytes: int
    string_wire_bytes: int

    @property
    def member_bytes(self) -> int:
        return self.value_bytes + self.key_bytes + self.colon_bytes + self.comma_bytes


@dataclass(frozen=True)
class OwnerOccurrences:
    label: str
    kind: str
    paths: tuple[str, ...]
    wire_bytes: tuple[int, ...]


@dataclass(frozen=True)
class ReferenceOccurrence:
    path: str
    codec: str
    digest: str
    byte_length: int


@dataclass(frozen=True)
class SizeMeasurement:
    status: Literal["exact", "incomplete"]
    reason: str | None = None
    total_bytes: int | None = None
    syntax_bytes: int = 0
    scalar_bytes: int = 0
    raw_utf8_bytes: int = 0
    string_wire_bytes: int = 0
    value_occurrences: int = 0
    owner_count: int = 0
    fields: tuple[FieldBytes, ...] = ()
    repeated_owners: tuple[OwnerOccurrences, ...] = ()
    references: tuple[ReferenceOccurrence, ...] = ()
    attribution: str = "parent/child and repeated-owner totals overlap; do not add them"


class _Incomplete(Exception):
    """A concrete missing condition for deferred exact-at-capture accounting."""


def _require_immutable(source: object, path: str, active: set[int]) -> None:
    if source is None or type(source) in (str, bool, int, float, UUID):
        return
    identity = id(source)
    if identity in active:
        raise _Incomplete(f"{path}: active source cycle")
    active.add(identity)
    try:
        if isinstance(source, (ContinuationValue, AdvisorAbandonmentResponse)):
            if type(source).model_config["frozen"] is not True:
                raise _Incomplete(f"{path}: retained model is not frozen")
            for name in type(source).model_fields:
                _require_immutable(source.__dict__[name], f"{path}.{name}", active)
        elif type(source) is tuple:
            for index, child in enumerate(source):
                _require_immutable(child, f"{path}[{index}]", active)
        else:
            # Includes MappingProxyType: its backing owner can still mutate.
            raise _Incomplete(f"{path}: unsupported or mutable borrowed {type(source).__name__}")
    finally:
        active.remove(identity)


def measure_retained_view(view: object) -> SizeMeasurement:
    """Count the same immutable borrowed view, after execution cleanup only.

    No complete encoding, model dump, embedded JSON parsing or payload copy is
    used. The finite per-string budget follows from its existing source length.
    """
    if type(view) is not _RetainedModelView:
        return SizeMeasurement("incomplete", "$: expected nominal retained model view")
    try:
        _require_immutable(view.source, "$", set())
    except _Incomplete as error:
        return SizeMeasurement("incomplete", str(error))

    fields: list[FieldBytes] = []
    references: list[ReferenceOccurrence] = []
    # Hold owners as well as ids: temporary projected UUID strings must not
    # disappear and allow id reuse to invent a repeated owner.
    owners: dict[int, tuple[object, str, list[str], list[int]]] = {}
    active: set[int] = set()
    syntax = scalar = raw = string_wire = occurrences = 0

    def string_size(text: str) -> int:
        measured = _REAL_COUNTER(text, limit=max(1, 2 + 6 * len(text)))
        assert measured.admitted_bytes is not None, "source-derived string bound failed"
        return measured.admitted_bytes

    def visit(node: object, path: str) -> int:
        nonlocal syntax, scalar, raw, string_wire, occurrences
        occurrences += 1
        owner = node.source if type(node) in (_RetainedModelView, _RetainedObjectView, _RetainedArrayView) else node
        identity = id(owner)
        if identity in active:
            raise _Incomplete(f"{path}: active projected cycle")
        if identity not in owners:
            owners[identity] = (owner, f"owner-{len(owners) + 1}", [], [])
        entry = owners[identity]
        # Reserve occurrence slots before descending, so first paths are in
        # traversal order even when a parent contains another owner occurrence.
        entry[2].append(path)
        occurrence_index = len(entry[3])
        entry[3].append(0)
        active.add(identity)
        try:
            if type(node) in (_RetainedModelView, _RetainedObjectView):
                total = 2
                syntax += 2
                if type(owner) is PrivateChunkRef:
                    references.append(ReferenceOccurrence(path, owner.codec, owner.digest, owner.byte_length))
                for index, (key, child) in enumerate(node.items()):
                    assert type(key) is str, "retained field key must be a string"
                    key_bytes = string_size(key)
                    key_raw = utf8_size(key)
                    raw += key_raw
                    string_wire += key_bytes
                    scalar += key_bytes
                    separators = 1 + int(index > 0)
                    syntax += separators
                    raw_before, wire_before = raw, string_wire
                    child_path = f"{path}.{key}"
                    child_bytes = visit(child, child_path)
                    total += key_bytes + separators + child_bytes
                    if path == "$" or path in ("$.effects", "$.transcript", "$.application_entry"):
                        fields.append(
                            FieldBytes(child_path, child_bytes, key_bytes, 1, int(index > 0), raw - raw_before, string_wire - wire_before)
                        )
            elif type(node) is _RetainedArrayView:
                total = 2
                syntax += 2
                for index, child in enumerate(node):
                    comma = int(index > 0)
                    syntax += comma
                    total += comma + visit(child, f"{path}[{index}]")
            elif type(node) is str:
                total = string_size(node)
                raw += utf8_size(node)
                string_wire += total
                scalar += total
            elif node is None or type(node) in (bool, int, float):
                measured = _REAL_COUNTER(node, limit=JSON_MAX_TOTAL_UTF8_BYTES)
                assert measured.admitted_bytes is not None, "valid scalar exceeded document budget"
                total = measured.admitted_bytes
                scalar += total
            else:
                raise _Incomplete(f"{path}: unsupported projected {type(node).__name__}")
            entry[3][occurrence_index] = total
            return total
        finally:
            active.remove(identity)

    try:
        total = visit(view, "$")
    except _Incomplete as error:
        return SizeMeasurement("incomplete", str(error))
    assert total == syntax + scalar, "syntax/scalar accounting does not reconcile"
    assert total == 2 + sum(item.member_bytes for item in fields if item.path.count(".") == 1), "field accounting does not reconcile"
    repeated = tuple(
        OwnerOccurrences(label, type(owner).__name__, tuple(paths), tuple(sizes))
        for owner, label, paths, sizes in owners.values()
        if len(paths) > 1
    )
    return SizeMeasurement(
        "exact",
        total_bytes=total,
        syntax_bytes=syntax,
        scalar_bytes=scalar,
        raw_utf8_bytes=raw,
        string_wire_bytes=string_wire,
        value_occurrences=occurrences,
        owner_count=len(owners),
        fields=tuple(fields),
        repeated_owners=repeated,
        references=tuple(references),
    )


@dataclass
class TurnEvidenceSizeWitness:
    """Pass through the real guard; retain only the first exact-target excess."""

    _counter: Counter = field(default=_REAL_COUNTER, repr=False)
    _view: _RetainedModelView | None = field(default=None, init=False, repr=False)
    _snapshot: TurnEvidenceSnapshot | None = field(default=None, init=False, repr=False)
    _result: BoundedCanonicalSize | None = field(default=None, init=False, repr=False)
    matching_calls: int = field(default=0, init=False)

    def observe_counter_call(self, value: object, *, limit: int) -> BoundedCanonicalSize:
        result = self._counter(value, limit=limit)
        if (
            type(value) is _RetainedModelView
            and type(value.source) is TurnEvidenceSnapshot
            and limit == JSON_MAX_TOTAL_UTF8_BYTES
            and result.exceeded
        ):
            self.matching_calls += 1
            if self._view is None:
                self._view, self._snapshot, self._result = value, value.source, result
        return result

    def measure_captured_view(self) -> dict[str, object]:
        """Emit payload-free metadata and release the borrowed closure afterward."""
        started = perf_counter()
        try:
            if self.matching_calls != 1 or self._view is None or self._result is None:
                measurement = SizeMeasurement("incomplete", "expected exactly one excessive complete-evidence view")
            else:
                assert self._view.source is self._snapshot
                measurement = measure_retained_view(self._view)
            return {
                "measurement": asdict(measurement),
                "matching_calls": self.matching_calls,
                "original_limit": self._result.limit if self._result is not None else None,
                "original_admitted_bytes": self._result.admitted_bytes if self._result is not None else None,
                "diagnostic_seconds": perf_counter() - started,
                "scope": "wire occurrences only; embedded JSON remains opaque; no live-memory or source-read claim",
            }
        finally:
            self._view = self._snapshot = self._result = None


def publish_turn_evidence_size_witness(
    request: pytest.FixtureRequest,
    record_testsuite_property: Callable[[str, object], None],
    report: dict[str, object],
) -> None:
    """Retain the same teardown annotation and its exact node membership."""
    request.node.add_report_section("teardown", "turn-evidence-size-witness", json.dumps(report, sort_keys=True))
    record_testsuite_property(
        "elspeth.turn_evidence_size_witness",
        json.dumps({"nodeid": request.node.nodeid, "report": report}, sort_keys=True),
    )


@pytest.fixture
def turn_evidence_size_witness(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    record_testsuite_property: Callable[[str, object], None],
) -> Iterator[None]:
    """Attach via usefixtures; all branches except literal success pass through."""
    if request.node.name != "test_actual_advisor_feedback_uses_successful_audit_projection_and_cold_source[success]":
        yield
        return
    witness = TurnEvidenceSizeWitness()
    monkeypatch.setattr(bounded_serialization, "bounded_canonical_json_size", witness.observe_counter_call)
    try:
        yield
    finally:
        report = witness.measure_captured_view()
        publish_turn_evidence_size_witness(request, record_testsuite_property, report)
        measurement = report["measurement"]
        assert isinstance(measurement, dict)
        assert measurement["status"] == "exact", report
