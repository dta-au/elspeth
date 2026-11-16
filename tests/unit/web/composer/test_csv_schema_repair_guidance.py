"""Repair advice must respect ownership of uploaded-source guarantees."""

import pytest

from elspeth.web.composer.tools.generation import explain_validation_code


def test_created_copy_index_is_repaired_on_consumer_input_schema() -> None:
    guidance = explain_validation_code("schema_contract_violation")
    assert guidance is not None
    repair = guidance[1]
    assert "batch_replicate" in repair
    assert "copy_index" in repair
    assert "input schema" in repair
    assert "patch_node_options" in repair
    assert "do not add it to the source" in repair


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
