"""Controls proving the real partial-failure test rejects incomplete lineage."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from elspeth.contracts import TerminalOutcome, TerminalPath
from elspeth.core.landscape.lineage import LineageResult
from tests.e2e.recovery import test_partial_failure as subject


@pytest.mark.parametrize("mode", ["baseline", "all-none", "failed-none", "missing-error", "wrong-error", "wrong-source", "wrong-outcome"])
def test_partial_failure_oracle_rejects_missing_or_crosswired_lineage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    original_explain = subject.explain
    observed: list[TerminalOutcome] = []
    mutated: list[str] = []

    def controlled_explain(*args: Any, **kwargs: Any) -> LineageResult | None:
        lineage = original_explain(*args, **kwargs)
        assert lineage is not None and lineage.outcome is not None
        assert lineage.outcome.outcome is not None
        observed.append(lineage.outcome.outcome)
        if mode == "all-none":
            mutated.append(mode)
            return None
        if lineage.outcome.outcome is TerminalOutcome.FAILURE and mode != "baseline":
            assert len(lineage.transform_errors) == 1
            mutated.append(mode)
            if mode == "failed-none":
                return None
            if mode == "missing-error":
                return replace(lineage, transform_errors=())
            if mode == "wrong-error":
                return replace(lineage, transform_errors=(replace(lineage.transform_errors[0], token_id="other-token"),))
            if mode == "wrong-source":
                return replace(lineage, source_row=replace(lineage.source_row, source_data={"id": 999, "value": 9990}))
            if mode == "wrong-outcome":
                return replace(
                    lineage,
                    outcome=replace(
                        lineage.outcome,
                        outcome=TerminalOutcome.SUCCESS,
                        path=TerminalPath.DEFAULT_FLOW,
                        sink_name="default",
                        error_hash=None,
                        context_json=None,
                    ),
                )
            raise ValueError(f"Unknown mutation mode: {mode}")
        return lineage

    monkeypatch.setattr(subject, "explain", controlled_explain)
    if mode == "baseline":
        subject.TestPartialFailure().test_partial_failures_produce_correct_audit_trail(tmp_path)
        assert observed.count(TerminalOutcome.FAILURE) == 3
        assert observed.count(TerminalOutcome.SUCCESS) == 7
        assert mutated == []
    else:
        with pytest.raises(AssertionError):
            subject.TestPartialFailure().test_partial_failures_produce_correct_audit_trail(tmp_path)
        assert observed
        assert mutated == [mode]
