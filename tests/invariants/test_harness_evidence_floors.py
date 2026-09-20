"""Require observed comparisons before value and forwarding gates pass."""

import pytest

from elspeth.contracts import TransformResult
from elspeth.contracts.contexts import TransformContext
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.plugins.transforms.field_mapper import FieldMapper
from elspeth.plugins.transforms.passthrough import PassThrough
from elspeth.testing import make_pipeline_row
from tests.invariants import test_pass_through_invariants as harness


def test_field_mapper_value_probe_executes_and_preserves_background_values() -> None:
    transform = FieldMapper(FieldMapper.probe_config())
    probe = make_pipeline_row({"background": "unchanged"})
    result = transform.execute_forward_invariant_probe(transform.forward_invariant_probe_rows(probe), harness._probe_context(transform))
    assert result.status == "success", result.reason
    assert result.row is not None
    assert result.row.to_dict() == {"background": "unchanged", "field_mapper_probe_target": "mapped"}


@pytest.mark.parametrize("outcome", ["error", "empty", "no_surviving_values"])
def test_value_harness_rejects_uncheckable_probes(monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    if outcome == "error":
        result = TransformResult.error({"reason": "validation_failed"})
    elif outcome == "empty":
        result = TransformResult.success_empty(success_reason={"action": "filter"})
    else:
        result = TransformResult.success(make_pipeline_row({}), success_reason={"action": "drop_fields"})
    monkeypatch.setattr(PassThrough, "execute_forward_invariant_probe", lambda self, rows, ctx: result)
    with pytest.raises(AssertionError, match="value comparisons"):
        harness.test_preserving_transforms_do_not_rewrite_values(_preserving_cls=PassThrough)


def test_value_harness_still_rejects_rewriting(monkeypatch: pytest.MonkeyPatch) -> None:
    def rewrite(self: PassThrough, rows: list[PipelineRow], ctx: TransformContext) -> TransformResult:
        payload = dict.fromkeys(rows[0], "rewritten value")
        return TransformResult.success(make_pipeline_row(payload), success_reason={"action": "rewrite"})

    monkeypatch.setattr(PassThrough, "execute_forward_invariant_probe", rewrite)
    with pytest.raises(AssertionError, match="rewrote"):
        harness.test_preserving_transforms_do_not_rewrite_values(_preserving_cls=PassThrough)


def test_value_harness_accepts_mixed_checkable_and_uncheckable_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def execute(self: PassThrough, rows: list[PipelineRow], ctx: TransformContext) -> TransformResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return TransformResult.error({"reason": "validation_failed"})
        if calls == 2:
            return TransformResult.success_empty(success_reason={"action": "filter"})
        return TransformResult.success(rows[0], success_reason={"action": "preserve"})

    monkeypatch.setattr(PassThrough, "execute_forward_invariant_probe", execute)
    harness.test_preserving_transforms_do_not_rewrite_values(_preserving_cls=PassThrough)
    assert calls > 2


class _ConsumesInput(PassThrough):
    """Drops a known field; optionally lies about forwarding other fields."""

    name = "evidence_floor_consumer"
    determinism = PassThrough.determinism
    passes_through_input = False
    forwards_input_fields = False
    keep_sentinel = True
    forward_extras = False

    def backward_invariant_probe_rows(self, row: PipelineRow) -> list[PipelineRow]:
        payload = row.to_dict()
        if not self.keep_sentinel:
            payload.pop(harness._FORWARDING_SENTINEL, None)
        payload["consumed"] = "known input"
        return [make_pipeline_row(payload)]

    def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        payload = row.to_dict() if self.forward_extras else {}
        payload.pop("consumed", None)
        return TransformResult.success(make_pipeline_row(payload), success_reason={"action": "consume"})


@pytest.mark.parametrize("outcome", ["error", "empty", "no_input", "no_sentinel"])
def test_underdeclaration_harness_requires_witnessed_sentinel(monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    if outcome == "no_sentinel":
        monkeypatch.setattr(_ConsumesInput, "keep_sentinel", False)
        monkeypatch.setattr(_ConsumesInput, "forward_extras", True)
        # The companion backward invariant succeeds, so it cannot close this gap.
        harness.test_non_pass_through_transforms_do_drop_fields(_non_pass_through_cls=_ConsumesInput)
    else:
        if outcome == "error":
            result = TransformResult.error({"reason": "validation_failed"})
        elif outcome == "empty":
            result = TransformResult.success_empty(success_reason={"action": "filter"})
        else:
            monkeypatch.setattr(_ConsumesInput, "backward_invariant_probe_rows", lambda self, row: [])
            result = TransformResult.success(make_pipeline_row({}), success_reason={"action": "consume"})
        monkeypatch.setattr(_ConsumesInput, "execute_backward_invariant_probe", lambda self, rows, ctx: result)
    with pytest.raises(pytest.fail.Exception, match="sentinel-bearing input"):
        harness.test_forwarding_declaration_is_truthful(_non_pass_through_cls=_ConsumesInput)


def test_underdeclaration_harness_still_rejects_forwarding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_ConsumesInput, "forward_extras", True)
    with pytest.raises(pytest.fail.Exception, match="forwarded the unknown field"):
        harness.test_forwarding_declaration_is_truthful(_non_pass_through_cls=_ConsumesInput)


def test_underdeclaration_harness_accepts_mixed_checkable_and_uncheckable_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def execute(self: _ConsumesInput, rows: list[PipelineRow], ctx: TransformContext) -> TransformResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return TransformResult.error({"reason": "validation_failed"})
        if calls == 2:
            return TransformResult.success_empty(success_reason={"action": "filter"})
        return self.process(rows[0], ctx)

    monkeypatch.setattr(_ConsumesInput, "execute_backward_invariant_probe", execute)
    harness.test_forwarding_declaration_is_truthful(_non_pass_through_cls=_ConsumesInput)
    assert calls > 2
