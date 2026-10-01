"""Repair advice must respect ownership of uploaded-source guarantees."""

import pytest

from elspeth.web.composer.tools.generation import explain_validation_code


@pytest.mark.parametrize("code", ["sink_contract_violation", "schema_contract_violation"])
def test_edge_contract_repair_does_not_treat_uploaded_samples_as_guarantees(code: str) -> None:
    guidance = explain_validation_code(code)
    assert guidance is not None
    repair = guidance[1]
    assert "source_data_contract" in repair
    assert "acknowledge" in repair
    assert "flexible" in repair and "fields" in repair
    assert "must not author" in repair
    assert "bound blob or inspect_source" not in repair
