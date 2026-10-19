"""Finite sink reserve facts and adversarial read-dependency controls."""

import ast
from collections import Counter
from pathlib import Path

import pytest
from tests.unit.architecture import test_web_landscape_mutation_fencing as fencing

ROOT = Path(__file__).parents[3]
COORDINATOR = "src/elspeth/engine/executors/sink_effects.py"
READ_HELPER = "src/elspeth/engine/executors/sink_disposition_reason.py"
READ_ORIGIN = "elspeth.engine.executors.sink_disposition_reason.resolve_sink_disposition_reason"
SOURCE_PATHS = (
    "src/elspeth/core/landscape/factory.py",
    "src/elspeth/core/landscape/execution_repository.py",
    "src/elspeth/core/landscape/execution/__init__.py",
    "src/elspeth/core/landscape/execution/sink_effects.py",
    "src/elspeth/core/landscape/execution/sink_effect_reservation.py",
    COORDINATOR,
    "src/elspeth/engine/executors/sink.py",
    "src/elspeth/engine/orchestrator/audit_export_effects.py",
    "src/elspeth/contracts/sink_effects.py",
    "src/elspeth/web/required_work.py",
    "src/elspeth/engine/orchestrator/sink_flush.py",
    "src/elspeth/engine/orchestrator/export.py",
    "src/elspeth/core/payload_store.py",
    "src/elspeth/engine/orchestrator/authority_guard.py",
    "src/elspeth/web/coordination/lifecycle.py",
    "src/elspeth/web/coordination/repository.py",
    "src/elspeth/web/execution/service.py",
    "src/elspeth/engine/orchestrator/run_lifecycle.py",
    "src/elspeth/engine/orchestrator/resume.py",
    READ_HELPER,
)


@pytest.fixture(scope="module")
def sink_units() -> tuple[fencing.SourceUnit, ...]:
    return tuple(fencing._parse_source(path, (ROOT / path).read_text()) for path in SOURCE_PATHS)


def _changed_unit(units: tuple[fencing.SourceUnit, ...], path: str, before: str, after: str) -> tuple[fencing.SourceUnit, ...]:
    original = next(unit for unit in units if unit.path == path)
    assert original.source.count(before) == 1
    changed = fencing._parse_source(path, original.source.replace(before, after))
    return tuple(changed if unit.path == path else unit for unit in units)


def test_actual_sink_receipt_retains_only_its_three_non_web_reserve_calls(sink_units: tuple[fencing.SourceUnit, ...]) -> None:
    failures, receipt = fencing._reserve_sink_effect_disjoint_receipts(sink_units)
    assert failures == []
    calls = Counter(
        (unit.path, ast.unparse(node.func.value))
        for unit in sink_units
        for node in ast.walk(unit.tree)
        if isinstance(node, ast.Call) and (unit.path, id(node)) in receipt["reserve_calls"]
    )
    assert calls == {
        (COORDINATOR, "self._effects"): 1,
        ("src/elspeth/core/landscape/execution/sink_effects.py", "self._reservation"): 2,
    }
    assert receipt["read_dependency_origins"] == frozenset({READ_ORIGIN})


@pytest.mark.parametrize(
    ("before", "after"),
    (
        ("self._effects.reserve(request.reservation, coordination_token=coordination_token)", "foreign.reserve(request.reservation)"),
        (
            "reservation = self._effects.reserve(request.reservation, coordination_token=coordination_token)",
            "reservation = self._effects.reserve(request.reservation, coordination_token=foreign)",
        ),
        ("effect.state is not SinkEffectState.FINALIZED or not durable_members", "not durable_members"),
        ("len(token_ids) != len(durable_members) or", "False or"),
        (
            'if compute_pipeline_membership_hash(members) != effect.membership_or_manifest_hash:\n            raise LandscapeRecordError("sink routing context does not bind the original effect membership")',
            'if False:\n            raise LandscapeRecordError("sink routing context does not bind the original effect membership")',
        ),
        ("hydrated = self._hydrate_member(durable)", "foreign(self._factory)\n                hydrated = self._hydrate_member(durable)"),
    ),
    ids=("foreign-reserve", "foreign-fence", "nonfinalized", "duplicate-members", "membership-hash", "factory-escape"),
)
def test_sink_receipt_refuses_changed_authority_or_membership(sink_units: tuple[fencing.SourceUnit, ...], before: str, after: str) -> None:
    changed = _changed_unit(sink_units, COORDINATOR, before, after)
    failures, _ = fencing._reserve_sink_effect_disjoint_receipts(changed)
    assert failures == ["sink-effect owner source shape changed SinkEffectCoordinator"]


@pytest.mark.parametrize(
    ("before", "after"),
    (
        ("type(state) is not NodeStateCompleted", "False"),
        ("len(candidates) != 1", "len(candidates) < 1"),
        ("item.row_id != row_id", "False"),
        ("state.input_hash != row_hash", "False"),
        ('config["routes"][edge.label] != item.pending_sink_name', "False"),
        ("factory.payload_store.retrieve(event.reason_ref) != reason_bytes", "False"),
        ("    candidates = []", "    factory.execution.sink_effects.reserve(None)\n    candidates = []"),
        ("from elspeth.contracts.audit import NodeStateCompleted", "from foreign.audit import NodeStateCompleted"),
        ("    candidates = []", "    resolve_sink_disposition_reason = foreign\n    candidates = []"),
    ),
    ids=(
        "nominal-completed",
        "ambiguous-gates",
        "foreign-row",
        "row-hash",
        "sink-identity",
        "payload-custody",
        "write",
        "import",
        "rebind",
    ),
)
def test_sink_receipt_refuses_changed_read_helper(sink_units: tuple[fencing.SourceUnit, ...], before: str, after: str) -> None:
    changed = _changed_unit(sink_units, READ_HELPER, before, after)
    failures, _ = fencing._reserve_sink_effect_disjoint_receipts(changed)
    assert failures == ["sink-effect read dependency source changed " + READ_HELPER]


def test_sink_receipt_requires_its_read_helper_source(sink_units: tuple[fencing.SourceUnit, ...]) -> None:
    failures, _ = fencing._reserve_sink_effect_disjoint_receipts(tuple(unit for unit in sink_units if unit.path != READ_HELPER))
    assert failures == ["sink-effect read dependency source missing " + READ_HELPER]


@pytest.mark.parametrize(
    "mutation",
    (
        "from elspeth.engine.executors import sink_disposition_reason as reasons\ndef corrupt():\n reasons.resolve_sink_disposition_reason = foreign\n",
        "from elspeth.engine.executors.sink_disposition_reason import resolve_sink_disposition_reason as resolve\ndef corrupt():\n resolve.__code__ = foreign.__code__\n",
        "import elspeth.engine.executors.sink_disposition_reason as reasons\ndef corrupt():\n setattr(reasons, 'resolve_sink_disposition_reason', foreign)\n",
    ),
    ids=("module-alias", "callable-alias", "reflection"),
)
def test_common_origin_proof_refuses_read_helper_replacement(sink_units: tuple[fencing.SourceUnit, ...], mutation: str) -> None:
    failures, receipt = fencing._reserve_sink_effect_disjoint_receipts(sink_units)
    assert failures == []
    sibling = fencing._parse_source("src/elspeth/sink_read_adversary.py", mutation)
    failures = fencing._reserve_selected_origin_effect_failures((*sink_units, sibling), receipt["read_dependency_origins"])
    assert any(sibling.path in failure for failure in failures), failures


def test_common_origin_proof_allows_unchanged_read_helper_imports(sink_units: tuple[fencing.SourceUnit, ...]) -> None:
    failures, receipt = fencing._reserve_sink_effect_disjoint_receipts(sink_units)
    assert failures == []
    sibling = fencing._parse_source(
        "src/elspeth/sink_read_adversary.py",
        "from elspeth.engine.executors import sink_disposition_reason as reasons\n"
        "from elspeth.engine.executors.sink_disposition_reason import resolve_sink_disposition_reason as resolve\n"
        "def harmless():\n return 'ordinary unrelated value'\n",
    )
    failures = fencing._reserve_selected_origin_effect_failures((*sink_units, sibling), receipt["read_dependency_origins"])
    assert not any(sibling.path in failure for failure in failures), failures
