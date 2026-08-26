"""Governance harness for ADR-009 §Clause 4 — pass-through annotation invariants.

Core tests here:

- **Forward invariant** (`test_annotated_transforms_preserve_input_fields`):
  For every registered ``passes_through_input=True`` transform, runs
  Hypothesis-generated probe rows through the transform-owned invariant
  execution hook and asserts every emitted row preserves every input field
  (contract AND payload). Fails CI on mis-annotation.

- **Backward invariant** (``test_non_pass_through_transforms_do_drop_fields``):
  Fails CI when a non-annotated transform that opted into probing (i.e.
  implements ``probe_config()``) preserves all input fields on every probe
  row. Remediation is either adding ``passes_through_input=True`` or
  supplying a ``probe_config()`` that exercises a case the transform
  demonstrably does not preserve.

- **Skip-rate budget** (``test_harness_skip_rate_budget``): asserts
  ``skip_rate ≤ 25%`` across the annotated plugin set. Track 2 additions
  that slip the budget must implement ``probe_config()`` per the contract.

The ``forwards_input_fields`` axis (elspeth-15c72686f2) gets the same
two-direction treatment: ``test_forwarding_transforms_remove_only_what_they
_declare`` truth-tests every declared removal set, and
``test_undeclared_transforms_do_not_forward_unknown_fields`` catches the
under-declaration direction — a transform that forwards a sentinel extra on
every emission while declaring nothing.

The harness uses ``pytest_generate_tests`` to parametrize over registered
transforms at collection time — with a guard that crashes if the plugin
list is empty (silent "0 tests" passes are the worst kind of theatre).
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from hypothesis import HealthCheck, given, settings

from elspeth.contracts.declaration_contracts import (
    DeclarationContract,
    registered_declaration_contracts,
)
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from tests.invariants.conftest import probe_row


def _iter_contracts_for_invariant_harness() -> list[DeclarationContract]:
    """Contracts the harness exercises. One-to-one with the registry today."""
    return list(registered_declaration_contracts())


class _UnprobeableTransform(Exception):
    """Raised when probe_config() is not implemented or the constructor rejects its output."""

    def __init__(self, *, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _registered_transform_classes() -> list[type[BaseTransform]]:
    """Return registered transforms as ``BaseTransform`` subclasses.

    ``PluginManager.get_transforms()`` is typed ``list[type[TransformProtocol]]``
    because the public surface is the protocol. In practice every registered
    plugin subclasses ``BaseTransform`` to inherit the
    ``passes_through_input``, ``is_batch_aware``, and ``probe_config()``
    machinery the harness relies on. The cast documents that framework
    invariant; if a non-``BaseTransform`` plugin were ever registered the
    harness would still typecheck and fail loudly at first use.
    """
    return cast(
        "list[type[BaseTransform]]",
        get_shared_plugin_manager().get_transforms(),
    )


def _annotated_pass_through_plugins() -> list[type[BaseTransform]]:
    """Every registered transform class with ``passes_through_input=True``.

    Reads ``cls.passes_through_input`` directly — no ``getattr`` default.
    Missing attribute is a framework bug (``BaseTransform`` supplies it) and
    a silent ``False`` coercion would hide the annotation from governance.
    """
    return [cls for cls in _registered_transform_classes() if cls.passes_through_input]


def _non_pass_through_plugins() -> list[type[BaseTransform]]:
    """Every registered transform class without pass-through annotation."""
    return [cls for cls in _registered_transform_classes() if not cls.passes_through_input]


def _probe_instantiate(cls: type[BaseTransform]) -> BaseTransform:
    """Build a transform instance via its ``probe_config()`` declaration.

    Narrow exception catches only ``NotImplementedError`` (missing
    implementation — legitimate skip) and ``TypeError`` (wrong constructor
    args — config shape mismatch). Any other exception is a plugin bug and
    must propagate (CLAUDE.md: plugin bugs must crash).
    """
    try:
        config = cls.probe_config()
    except NotImplementedError as exc:
        if cls.passes_through_input:
            reason = f"{cls.__name__}.probe_config() not implemented: {exc}"
        else:
            reason = f"{cls.__name__}.probe_config() not implemented (non-pass-through transform has not opted into invariant probing)."
        raise _UnprobeableTransform(reason=reason) from exc
    try:
        return cls(config)
    except TypeError as exc:
        raise _UnprobeableTransform(reason=f"{cls.__name__}.__init__ rejected probe_config() output: {exc}") from exc


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Dynamic parametrization — resolves after plugin registration fixtures run.

    Using a fixture-less ``pytest.mark.parametrize`` at module scope would
    evaluate ``_annotated_pass_through_plugins()`` at collection time,
    before plugin registration side effects have fired. The guard here
    crashes loudly if the list is empty.
    """
    if "_annotated_cls" in metafunc.fixturenames:
        plugins = _annotated_pass_through_plugins()
        assert plugins, (
            "Expected at least 1 passes_through_input=True transform; found "
            f"{[cls.__name__ for cls in plugins]!r}. Plugin registration "
            "may have failed — the invariant harness would silently pass."
        )
        metafunc.parametrize("_annotated_cls", plugins, ids=lambda c: c.__name__)

    if "_non_pass_through_cls" in metafunc.fixturenames:
        plugins = _non_pass_through_plugins()
        metafunc.parametrize("_non_pass_through_cls", plugins, ids=lambda c: c.__name__)

    if "_preserving_cls" in metafunc.fixturenames:
        preserving = [cls for cls in _registered_transform_classes() if cls.preserves_input_values]
        assert preserving, (
            "Expected at least 1 preserves_input_values=True transform "
            "(passthrough and llm declare it); plugin registration may have "
            "failed — the value-preservation harness would silently pass."
        )
        metafunc.parametrize("_preserving_cls", preserving, ids=lambda c: c.__name__)


def _probe_context(transform: BaseTransform) -> Any:
    """Minimal TransformContext for probe invocations.

    The harness runs transforms in isolation without a real run; the context
    is a lightweight stub with a mock landscape recorder.

    For ``is_batch_aware`` transforms the context also carries a synthetic
    :class:`AggregationBatchContext`.  Most batch-aware transforms compute
    from ``rows`` alone and never read ``ctx.aggregation_batch``, but the
    contract on :class:`AggregationExecutor` is that the batch context IS
    populated before ``process()`` runs.  The probe must mirror that
    contract; otherwise transforms that legitimately rely on pagination
    metadata (e.g. ``ReportAssemble`` emitting "page N of M") cannot run
    inside the governance sweep without a special opt-out.
    """
    from elspeth.contracts.node_state_context import AggregationBatchContext
    from tests.fixtures.factories import make_context

    ctx = make_context()
    if transform.is_batch_aware:
        ctx.aggregation_batch = AggregationBatchContext(
            trigger_type="count",
            batch_id="probe-batch",
            batch_size=1,
            flush_index=1,
            rows_seen_total=1,
            row_start=1,
            row_end=1,
            is_end_of_source=True,
        )
    return ctx


def _emitted_rows_from_result(result: Any) -> list[PipelineRow]:
    if result.row is not None:
        return [result.row]
    if result.rows is not None:
        return list(result.rows)
    return []


def _observed_fields(row: PipelineRow) -> frozenset[str]:
    """Fields present in both the contract and payload for ``row``."""
    contract_fields = frozenset(fc.normalized_name for fc in row.contract.fields)
    payload_fields = frozenset(row.keys())
    return contract_fields & payload_fields


def _effective_input_fields(probe_rows: list[PipelineRow]) -> frozenset[str]:
    """Mirror runtime pass-through input-field semantics for probe rows."""
    if not probe_rows:
        return frozenset()
    observed_sets = [_observed_fields(row) for row in probe_rows]
    effective = observed_sets[0]
    for observed in observed_sets[1:]:
        effective = effective & observed
    return effective


@given(row=probe_row())
@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
def test_annotated_transforms_preserve_input_fields(
    _annotated_cls: type[BaseTransform],
    row: PipelineRow,
) -> None:
    """Forward invariant — ADR-009 §Clause 4.

    Every emitted row from a ``passes_through_input=True`` transform must
    preserve every input field in both its contract and its payload
    (runtime observation is the intersection of the two).

    Failures are actionable: Hypothesis shrinks to a minimal probe row,
    producing the smallest repro case for the plugin author. The remediation
    is clear — either fix the implementation or remove the annotation.
    """
    try:
        transform = _probe_instantiate(_annotated_cls)
    except _UnprobeableTransform as exc:
        pytest.skip(f"{_annotated_cls.__name__}: {exc.reason}")

    probe_rows = transform.forward_invariant_probe_rows(row)

    result = transform.execute_forward_invariant_probe(
        probe_rows,
        _probe_context(transform),
    )

    if result.status != "success":
        # Legitimate processing error on this probe (e.g., quarantine). Not
        # a pass-through contract violation.
        return

    emitted_rows = _emitted_rows_from_result(result)
    if not emitted_rows:
        # Empty emission — ADR-009 §Clause 3 carve-out. Drops nothing.
        return

    input_fields = _effective_input_fields(probe_rows)
    for emitted in emitted_rows:
        runtime_contract = frozenset(fc.normalized_name for fc in emitted.contract.fields)
        runtime_payload = frozenset(emitted.keys())
        runtime_observed = runtime_contract & runtime_payload
        dropped = input_fields - runtime_observed
        assert not dropped, (
            f"{_annotated_cls.__name__} is annotated passes_through_input=True "
            f"but dropped fields {sorted(dropped)!r} from probe row "
            f"{row.to_dict()!r}. Either fix the implementation or remove "
            "the annotation."
        )


@given(row=probe_row())
@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
def test_preserving_transforms_do_not_rewrite_values(
    _preserving_cls: type[BaseTransform],
    row: PipelineRow,
) -> None:
    """Value invariant — elspeth-e6e552ce34.

    Every emitted row from a ``preserves_input_values=True`` transform must
    carry each surviving input field with a value ``==`` to its input value.
    This is the promise that lets the build-time type-resolution walk recurse
    through the transform; a rewrite here means the annotation is a lie and
    build verdicts built on it are unsound. Unprobeable declarers (no
    ``probe_config()``, e.g. llm's provider dependency) skip, exactly like
    the presence harness above — their truth rests on code review at the
    declaration site.
    """
    try:
        transform = _probe_instantiate(_preserving_cls)
    except _UnprobeableTransform as exc:
        pytest.skip(f"{_preserving_cls.__name__}: {exc.reason}")

    probe_rows = transform.forward_invariant_probe_rows(row)
    input_values = {name: probe.to_dict()[name] for probe in probe_rows for name in _observed_fields(probe) if name in probe.to_dict()}

    result = transform.execute_forward_invariant_probe(
        probe_rows,
        _probe_context(transform),
    )
    if result.status != "success":
        return

    for emitted in _emitted_rows_from_result(result):
        payload = emitted.to_dict()
        rewritten = {
            name: (input_values[name], payload[name]) for name in input_values if name in payload and payload[name] != input_values[name]
        }
        assert not rewritten, (
            f"{_preserving_cls.__name__} is annotated preserves_input_values=True "
            f"but rewrote {rewritten!r} on probe row {row.to_dict()!r}. Either "
            "fix the implementation or remove the annotation — the type-"
            "resolution walk recurses through this transform on that promise."
        )


def test_harness_skip_rate_budget() -> None:
    """Skip-rate budget — ADR-009 §Clause 4.

    Assert ``skip_rate ≤ 25%`` across the annotated plugin set. Track 2
    additions that can't be probed in isolation must implement
    ``probe_config()`` per the contract; raising the budget is not an
    acceptable response.
    """
    transforms = _annotated_pass_through_plugins()
    if not transforms:
        pytest.skip("No annotated transforms registered.")

    unprobeable: list[str] = []
    for cls in transforms:
        try:
            _probe_instantiate(cls)
        except _UnprobeableTransform as exc:
            unprobeable.append(f"{cls.__name__}: {exc.reason}")

    skip_rate = len(unprobeable) / len(transforms)
    assert skip_rate <= 0.25, (
        f"Harness skip rate {skip_rate:.0%} exceeds 25% budget "
        f"({len(unprobeable)}/{len(transforms)} annotated transforms unprobeable). "
        f"Implement probe_config() on: {unprobeable!r}"
    )


# Backward-invariant sweep budget. Scalar-only probes — bounded to keep
# invariant runs fast; the per-transform forward invariant carries the
# correctness load, this one is a sanity check on non-annotated probeable
# transforms. ``_SWEEP_MIN_PROBES`` guards against Hypothesis strategy
# exhaustion masquerading as clean runs.
_SWEEP_EXAMPLES = 15
_SWEEP_MIN_PROBES = 5


def test_non_pass_through_transforms_do_drop_fields(
    _non_pass_through_cls: type[BaseTransform],
) -> None:
    """Backward invariant — ADR-009 §Clause 4.

    For every non-annotated transform that opted into probing (i.e.,
    implements ``probe_config()``), run ``_SWEEP_EXAMPLES`` probe rows and
    assert at least one probe produces an emission that drops a field. A
    transform that preserves all fields on every probe is either
    mis-annotated (should carry ``passes_through_input=True``) or its
    ``probe_config()`` does not exercise a case where fields are dropped
    — both are governance defects that must be addressed in this PR.

    Non-annotated transforms WITHOUT ``probe_config()`` are skipped with a
    diagnostic reason:
    probing is opt-in, and the forward invariant + skip-rate budget are the
    load-bearing governance for annotated transforms. Transforms whose
    ``probe_config()`` raises or whose constructor rejects it are also
    skipped — those are diagnostic signals, not governance gates.

    Scalar-only probes may miss structured-data or mixed-validity batch
    drops, so remediation options are either (a) add the annotation if the
    transform really is pass-through, or (b) override
    ``backward_invariant_probe_rows()`` to return a representative input
    shape that triggers the actual drop path.
    """
    try:
        transform = _probe_instantiate(_non_pass_through_cls)
    except _UnprobeableTransform as exc:
        # Non-annotated transforms that did not opt into probing — or whose
        # probe config is incompatible with their constructor — are out of
        # scope for the backward invariant. The forward invariant and
        # skip-rate budget cover the annotated set; this test only
        # exercises transforms that explicitly declared a probe config.
        pytest.skip(f"{_non_pass_through_cls.__name__}: {exc.reason}")

    probes_preserved = True
    probe_count = 0

    @given(probe=probe_row())
    @settings(
        max_examples=_SWEEP_EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    def _sweep(probe: PipelineRow) -> None:
        nonlocal probes_preserved, probe_count
        probe_count += 1
        probe_rows = transform.backward_invariant_probe_rows(probe)
        result = transform.execute_backward_invariant_probe(
            probe_rows,
            _probe_context(transform),
        )
        if result.status != "success":
            return
        emitted_rows = _emitted_rows_from_result(result)
        if not emitted_rows:
            return
        input_fields = frozenset(field_name for input_row in probe_rows for field_name in _observed_fields(input_row))
        for emitted in emitted_rows:
            observed = _observed_fields(emitted)
            if input_fields - observed:
                probes_preserved = False
                return

    _sweep()

    if probe_count < _SWEEP_MIN_PROBES:
        # Strategy exhaustion is a harness failure, not a plugin failure —
        # make the operator aware without blaming the transform.
        pytest.fail(
            f"{_non_pass_through_cls.__name__}: only {probe_count} probe rows "
            f"exercised (expected ≥ {_SWEEP_MIN_PROBES}). Harness probe generation "
            "is under-powered for this transform."
        )

    if probes_preserved:
        pytest.fail(
            f"{_non_pass_through_cls.__name__} is NOT annotated "
            f"passes_through_input=True but preserved every input field in "
            f"{probe_count} probe rows. Either (a) add passes_through_input=True "
            "if the transform is in fact pass-through, or (b) override "
            f"{_non_pass_through_cls.__name__}.backward_invariant_probe_rows() "
            "to return a shape that triggers the field-dropping code path."
        )


_FORWARDING_SENTINEL = "elspeth_probe_extra_ride_along"


def _with_sentinel_field(probe: PipelineRow) -> PipelineRow:
    """Return ``probe`` plus one unknown extra field no transform declares.

    The sentinel models the elspeth-15c72686f2 defect vector: an upstream
    producer's extra column (an llm's ``<response_field>_usage``) that the
    transform under probe never heard of. A transform that forwards it is
    forwarding unknown input fields.
    """
    from elspeth.contracts.schema_contract import FieldContract, SchemaContract

    payload = probe.to_dict().copy()
    payload[_FORWARDING_SENTINEL] = "ride-along"
    fields = (
        *probe.contract.fields,
        FieldContract(
            normalized_name=_FORWARDING_SENTINEL,
            original_name=_FORWARDING_SENTINEL,
            python_type=str,
            required=True,
            source="inferred",
            nullable=False,
        ),
    )
    contract = SchemaContract(mode="OBSERVED", fields=fields, locked=True)
    return PipelineRow(payload, contract)


def test_undeclared_transforms_do_not_forward_unknown_fields(
    _non_pass_through_cls: type[BaseTransform],
) -> None:
    """Under-declaration guard for ``forwards_input_fields`` (elspeth-15c72686f2).

    The truth-test below verifies every transform that DECLARES forwarding;
    it skips everything that does not, so a future transform that forwards
    the row while declaring nothing would silently recreate the original
    defect class: both definite-emits walks stop at it, upstream extras
    become invisible to the build-time firewall, and the graph is back to
    "build green, every row dies at the locked sink". The commit that
    introduced the declaration relied on a one-time manual audit of the
    non-declaring transforms; this test makes that audit permanent.

    Method: seed each probe row with a sentinel field no transform knows.
    A fresh-dict transform (the batch_* family, report_assemble) never emits
    it. If EVERY successful emission carries the sentinel, the transform
    demonstrably forwards unknown input fields and must declare — either
    ``forwards_input_fields=True`` (with its removals) or
    ``passes_through_input=True`` if the stronger claim holds.
    """
    try:
        transform = _probe_instantiate(_non_pass_through_cls)
    except _UnprobeableTransform as exc:
        pytest.skip(f"{_non_pass_through_cls.__name__}: {exc.reason}")

    if transform.forwards_input_fields:
        pytest.skip(f"{_non_pass_through_cls.__name__} declares forwarding — verified by the removal truth-test.")

    probe_count = 0
    asserted_count = 0
    sentinel_always_forwarded = True

    @given(probe=probe_row())
    @settings(
        max_examples=_SWEEP_EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    def _sweep(probe: PipelineRow) -> None:
        nonlocal probe_count, asserted_count, sentinel_always_forwarded
        probe_count += 1
        probe_rows = transform.backward_invariant_probe_rows(_with_sentinel_field(probe))
        # A probe hook that rebuilds its input rows without the sentinel
        # cannot witness forwarding either way — do not count that emission.
        if any(_FORWARDING_SENTINEL not in _observed_fields(input_row) for input_row in probe_rows):
            return
        result = transform.execute_backward_invariant_probe(probe_rows, _probe_context(transform))
        if result.status != "success":
            return
        for emitted in _emitted_rows_from_result(result):
            asserted_count += 1
            if _FORWARDING_SENTINEL not in _observed_fields(emitted):
                sentinel_always_forwarded = False
                return

    _sweep()

    if probe_count < _SWEEP_MIN_PROBES:
        pytest.fail(
            f"{_non_pass_through_cls.__name__}: only {probe_count} probe rows "
            f"exercised (expected >= {_SWEEP_MIN_PROBES}). Harness probe generation "
            "is under-powered for this transform."
        )

    # No successful sentinel-carrying emission — no evidence either way. The
    # all-probes-error case is already failed by the backward invariant above,
    # so a silent pass here cannot hide a dead probe config.
    if asserted_count == 0:
        return

    if sentinel_always_forwarded:
        pytest.fail(
            f"{_non_pass_through_cls.__name__} forwarded the unknown field "
            f"{_FORWARDING_SENTINEL!r} on every successful emission "
            f"({asserted_count} rows) but declares forwards_input_fields=False. "
            "The definite-emits walks stop at undeclared transforms, so upstream "
            "extras become invisible to the build-time firewall (elspeth-15c72686f2). "
            "Declare forwards_input_fields=True with the removal set process() "
            "actually removes — or passes_through_input=True if nothing is removed."
        )


def test_forwarding_transforms_remove_only_what_they_declare(
    _non_pass_through_cls: type[BaseTransform],
) -> None:
    """Truth-test for the ``forwards_input_fields`` declaration (elspeth-15c72686f2).

    ``forwards_input_fields`` is what a transform declares when it forwards the
    whole row but cannot claim ``passes_through_input`` — because it consumes a
    column (line_explode's ``source_field``, json_explode's ``array_field``,
    field_mapper's rename sources) or because it drops whole ROWS
    (batch_outlier_annotator). Unlike ``passes_through_input`` it has no runtime
    cross-check, so without this test it would be a claim nothing verifies —
    and it is a claim the build-time extras firewall REJECTS graphs on, so a
    wrong one produces a false rejection of a working pipeline.

    The assertion is per emitted row against the INTERSECTION of the probe's
    input rows, not their union. The union is what the backward invariant above
    uses, and it is wrong here: batch_outlier_annotator's probe deliberately
    feeds one row that gets skipped entirely, and a field carried only by that
    row legitimately never reaches the output. The intersection asks the
    question the declaration actually makes — a field present on EVERY input
    row survives onto every emitted row, minus the declared removals.

    Over-declaring removals is safe (it shrinks the predicted emit set, so the
    firewall rejects less), which is why this checks only the ``⊇`` direction.
    Under-declaring is the failure this catches.
    """
    try:
        transform = _probe_instantiate(_non_pass_through_cls)
    except _UnprobeableTransform as exc:
        pytest.skip(f"{_non_pass_through_cls.__name__}: {exc.reason}")

    if not transform.forwards_input_fields:
        pytest.skip(f"{_non_pass_through_cls.__name__} does not declare forwards_input_fields.")

    removed = transform.removed_input_fields
    probe_count = 0
    asserted_count = 0
    violations: list[str] = []

    @given(probe=probe_row())
    @settings(
        max_examples=_SWEEP_EXAMPLES,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
    )
    def _sweep(probe: PipelineRow) -> None:
        nonlocal probe_count, asserted_count
        probe_count += 1
        probe_rows = transform.backward_invariant_probe_rows(probe)
        result = transform.execute_backward_invariant_probe(probe_rows, _probe_context(transform))
        if result.status != "success":
            return
        emitted_rows = _emitted_rows_from_result(result)
        if not emitted_rows:
            return
        surviving = frozenset.intersection(*[_observed_fields(input_row) for input_row in probe_rows]) - removed
        for emitted in emitted_rows:
            asserted_count += 1
            dropped = surviving - _observed_fields(emitted)
            if dropped:
                violations.append(
                    f"emitted row dropped {sorted(dropped)!r} that no declared removal accounts for "
                    f"(removed_input_fields={sorted(removed)!r})"
                )
                return

    _sweep()

    if probe_count < _SWEEP_MIN_PROBES:
        pytest.fail(
            f"{_non_pass_through_cls.__name__}: only {probe_count} probe rows "
            f"exercised (expected >= {_SWEEP_MIN_PROBES}). Harness probe generation "
            "is under-powered for this transform."
        )

    # An emptiness-graded check scores "every probe errored" as a pass. A
    # declaring transform that never reached the assertion has not been
    # verified at all, and the declaration would ship unchecked.
    if asserted_count == 0:
        pytest.fail(
            f"{_non_pass_through_cls.__name__} declares forwards_input_fields=True but no "
            f"probe produced a success emission in {probe_count} rows, so the declaration "
            "was never checked. Override backward_invariant_probe_rows() to return a shape "
            "the transform can actually process."
        )

    if violations:
        pytest.fail(
            f"{_non_pass_through_cls.__name__} declares forwards_input_fields=True but "
            f"{violations[0]}. Either widen removed_input_fields to name the field, or drop "
            "the forwards_input_fields declaration — the build-time extras firewall rejects "
            "graphs on this claim, so an under-stated removal set predicts fields that never arrive."
        )
