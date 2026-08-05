"""The canonical invariant probes must actually EXECUTE, not merely be invoked.

``test_pass_through_invariants`` is the governance harness for ADR-009 §Clause 4,
and both of its per-transform invariants have the same shape: run the probe, and
if ``result.status != "success"`` treat the row as a legitimate processing error
and move on. That carve-out is correct — a quarantine is not a pass-through
violation — but it means a transform whose probe can NEVER succeed passes the
harness while checking nothing at all. Green, and vacuous.

This file is the gate on that carve-out: for every registered transform, the
canonical probe in its own direction must reach ``status == "success"`` at least
once. It answers the question the harness cannot ask about itself — "did the
invariant actually run?"

DISCOVERY, NOT A LITERAL ROSTER. This file previously named six forward and
three backward classes by hand. A hand-typed roster stops covering whatever is
added after it was typed, and it did: ``blob_fetch`` shipped pass-through with a
probe config naming a URL field it never injects, so its forward probe has always
errored — and because it was not one of the six, nothing noticed while
``test_annotated_transforms_preserve_input_fields[BlobFetch]`` reported green.

MEMBERSHIP IS BY CAPABILITY, and the capability is ``passes_through_input``:

* Forward is the pass-through direction. ADR-009's forward invariant asks whether
  an annotated transform preserves every input field, so it is asked of exactly
  the ``passes_through_input=True`` set.
* Backward is the complement. It asks whether a NON-annotated transform really
  does drop a field, so it is asked of exactly the ``passes_through_input=False``
  set.

Those are the same two rosters ``test_pass_through_invariants`` builds from
``_annotated_pass_through_plugins`` / ``_non_pass_through_plugins``. Deriving
membership the same way is deliberate: any other predicate could leave a
transform that the harness DOES exercise outside the gate that checks the
harness is exercising it — a hole between two files that both look complete.

Because the two rosters partition on a boolean, the partition is TOTAL — every
registered transform is in exactly one direction, and
``test_probe_rosters_partition_the_registry`` asserts that rather than trusting
it. There is consequently no legitimate structural exclusion today, and
``_STRUCTURAL_EXCLUSIONS`` is empty on purpose: exclusion must be a documented
decision, never a silent skip.

Failures accumulate rather than aborting at the first one. A bare loop with an
inline assert stops at the earliest failing transform and hides every transform
behind it, which for a coverage gate is the same blindness it exists to catch.
"""

from __future__ import annotations

from typing import Any, cast

from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.manager import PluginManager
from elspeth.testing import make_pipeline_row
from tests.invariants.test_pass_through_invariants import (
    _probe_context,
    _probe_instantiate,
    _UnprobeableTransform,
)

# Transforms excluded from BOTH directions for a documented structural reason.
#
# EMPTY, and that is the honest state: membership partitions on
# ``passes_through_input``, so every registered transform has exactly one
# direction, and every registered transform is probeable.
#
# A name added here must carry a structural justification — a transform that
# genuinely cannot be driven by either canonical probe — and never "it was
# failing". A failing probe is a finding to fix at the plugin, not a roster edit;
# suppressing it here reproduces exactly the hand-typed blindness this file was
# rewritten to remove.
_STRUCTURAL_EXCLUSIONS: frozenset[str] = frozenset()


def _registered_transform_classes() -> tuple[type[BaseTransform], ...]:
    """Every built-in transform class, from a live registry.

    The cast documents the same framework invariant
    ``test_pass_through_invariants._registered_transform_classes`` relies on: the
    registry is typed to the protocol, but every registered plugin subclasses
    ``BaseTransform`` to inherit the annotation and probe machinery read here.
    """
    manager = PluginManager()
    manager.register_builtin_plugins()
    return cast("tuple[type[BaseTransform], ...]", tuple(manager.get_transforms()))


def _plugin_name(plugin_cls: type[BaseTransform]) -> str:
    return getattr(plugin_cls, "name", plugin_cls.__name__)


def _forward_scope() -> tuple[type[BaseTransform], ...]:
    """Transforms whose canonical direction is FORWARD — the annotated set.

    Reads ``passes_through_input`` directly, with no ``getattr`` default:
    ``BaseTransform`` supplies the attribute, so a missing one is a framework
    bug, and a silent ``False`` coercion would move a transform between rosters
    instead of failing.
    """
    return tuple(
        cls for cls in _registered_transform_classes() if cls.passes_through_input and _plugin_name(cls) not in _STRUCTURAL_EXCLUSIONS
    )


def _backward_scope() -> tuple[type[BaseTransform], ...]:
    """Transforms whose canonical direction is BACKWARD — the non-annotated set."""
    return tuple(
        cls for cls in _registered_transform_classes() if not cls.passes_through_input and _plugin_name(cls) not in _STRUCTURAL_EXCLUSIONS
    )


def _probe_execution_failure(plugin_cls: type[BaseTransform], *, direction: str) -> str | None:
    """Run the canonical probe; return a failure description, or None on success.

    Returns rather than asserts so the caller can report every transform at once.
    An exception escaping the probe is a failure of the same kind as a non-success
    status — the invariant did not run — so it is captured, not propagated.
    """
    try:
        transform = _probe_instantiate(plugin_cls)
    except _UnprobeableTransform as exc:
        return f"not probeable: {exc.reason}"

    base_row = make_pipeline_row({"baseline": "kept"})
    try:
        if direction == "forward":
            probe_rows = transform.forward_invariant_probe_rows(base_row)
            result = transform.execute_forward_invariant_probe(probe_rows, _probe_context(transform))
        else:
            probe_rows = transform.backward_invariant_probe_rows(base_row)
            result = transform.execute_backward_invariant_probe(probe_rows, _probe_context(transform))
    except Exception as exc:
        return f"probe raised {type(exc).__name__}: {exc}"

    if result.status == "success":
        return None
    return f"status={result.status!r} reason={_probe_failure_reason(result)!r}"


def _probe_failure_reason(result: Any) -> Any:
    """The transform's own explanation for a non-success probe, for the report.

    ``TransformResult`` carries the structured failure record on ``reason`` —
    e.g. ``{'reason': 'validation_failed', 'error': ..., 'error_type': ...}``.
    Read defensively so a shape change turns into a degraded message rather than
    an AttributeError inside the gate, and say so explicitly when nothing is
    there: a bare ``None`` in the failure text would read as "no reason given"
    when it actually means this helper is looking at the wrong attribute.
    """
    reason = getattr(result, "reason", None)
    if reason is None:
        return "<no 'reason' on TransformResult — inspect the result shape directly>"
    return reason


def test_probe_rosters_partition_the_registry() -> None:
    """Guard the guard: every registered transform has exactly one direction.

    Three ways for this gate to go silently vacuous, all closed here: an empty
    roster, a transform in neither direction (the hand-typed failure mode), and a
    transform in both. The partition is what lets ``_STRUCTURAL_EXCLUSIONS`` stay
    empty and be a true statement rather than an unchecked hope.
    """
    registered = {_plugin_name(cls) for cls in _registered_transform_classes()}
    forward = {_plugin_name(cls) for cls in _forward_scope()}
    backward = {_plugin_name(cls) for cls in _backward_scope()}

    assert forward, "Forward probe roster is empty — the forward coverage check below would be vacuous."
    assert backward, "Backward probe roster is empty — the backward coverage check below would be vacuous."
    assert forward & backward == set(), f"Transforms claimed by both directions: {sorted(forward & backward)}"
    assert forward | backward == registered - _STRUCTURAL_EXCLUSIONS, (
        f"Transforms covered by neither probe direction: {sorted(registered - _STRUCTURAL_EXCLUSIONS - forward - backward)}. "
        f"Every registered transform must be exercised in one direction or be named in _STRUCTURAL_EXCLUSIONS with a reason."
    )


def test_every_registered_transform_is_probeable() -> None:
    """A transform that cannot be instantiated from ``probe_config()`` is invisible to ADR-009.

    ``test_pass_through_invariants`` SKIPS such a transform — correctly, since it
    budgets skips separately — which means the skip is the only signal, and skips
    are not read. Fail here instead, so the next author is told to supply a
    ``probe_config()`` rather than discovering the plugin was never governed.
    """
    unprobeable: dict[str, str] = {}
    for plugin_cls in _registered_transform_classes():
        try:
            _probe_instantiate(plugin_cls)
        except _UnprobeableTransform as exc:
            unprobeable[_plugin_name(plugin_cls)] = exc.reason

    assert unprobeable == {}, f"Transforms that cannot be probed at all, so no invariant runs against them: {unprobeable}"


def test_in_scope_forward_probes_execute_successfully_without_blind_skips() -> None:
    """Every pass-through transform's forward probe must reach success at least once.

    A transform failing here is NOT failing the pass-through invariant — it is
    failing to be ASKED. ``test_annotated_transforms_preserve_input_fields``
    returns early on a non-success probe, so the transform reports green while
    asserting nothing.

    Remediation is at the plugin, in one of two shapes both already used by its
    peers: override ``forward_invariant_probe_rows()`` to inject the fields the
    transform's own ``probe_config()`` names (``_augment_invariant_probe_row``
    exists for this), and override ``execute_forward_invariant_probe()`` when a
    transport seam must be stubbed. Every other network-backed pass-through
    transform does both.
    """
    failures = {
        _plugin_name(cls): failure
        for cls in _forward_scope()
        if (failure := _probe_execution_failure(cls, direction="forward")) is not None
    }
    assert failures == {}, (
        f"Forward invariant probes that never execute successfully, so ADR-009's forward "
        f"invariant silently checks nothing for them: {failures}"
    )


def test_in_scope_backward_probes_execute_successfully_without_blind_skips() -> None:
    """Every non-pass-through transform's backward probe must reach success at least once.

    Same failure mode from the other side:
    ``test_non_pass_through_transforms_do_drop_fields`` returns early on a
    non-success probe, so a transform whose backward probe always errors is
    reported as having dropped nothing to check.
    """
    failures = {
        _plugin_name(cls): failure
        for cls in _backward_scope()
        if (failure := _probe_execution_failure(cls, direction="backward")) is not None
    }
    assert failures == {}, (
        f"Backward invariant probes that never execute successfully, so ADR-009's backward "
        f"invariant silently checks nothing for them: {failures}"
    )
