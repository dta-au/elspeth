"""The shared declared-input miss classifier and the proof-entry lookup (ADR-013 Amendment 2026-09-27).

``classify_declared_input_miss`` is the ONE rule both input seams apply (the
transform preflight and ``validate_batch_inputs``): a WHOLE-set test, so a
miss touching any proven or divergent field is Tier 1 on the full set.
``declared_input_proof_entry`` refuses a node the build's proof does not
cover — it never reads a missing entry as "proves nothing", which would route
every miss including the proven ones (architect T7).
"""

from __future__ import annotations

import pytest

from elspeth.contracts.errors import DeclaredInputFieldAbsentViolation, OrchestrationInvariantError, PluginContractViolation
from elspeth.engine.executors.declared_input_miss import classify_declared_input_miss, declared_input_proof_entry


class TestClassify:
    def test_absent_and_unproven_routes(self) -> None:
        assert classify_declared_input_miss(missing=frozenset({"b"}), proven=frozenset(), payload_keys=frozenset({"a"})) == "absent"

    def test_a_proven_field_makes_the_whole_miss_proven(self) -> None:
        kind = classify_declared_input_miss(missing=frozenset({"b", "c"}), proven=frozenset({"c"}), payload_keys=frozenset({"a"}))

        assert kind == "proven"

    def test_a_payload_key_the_contract_lost_is_divergent(self) -> None:
        kind = classify_declared_input_miss(missing=frozenset({"b", "c"}), proven=frozenset(), payload_keys=frozenset({"a", "c"}))

        assert kind == "divergent"

    def test_proven_outranks_divergent(self) -> None:
        kind = classify_declared_input_miss(missing=frozenset({"b"}), proven=frozenset({"b"}), payload_keys=frozenset({"b"}))

        assert kind == "proven"

    def test_no_miss_is_a_wiring_error(self) -> None:
        with pytest.raises(OrchestrationInvariantError, match="no missing field"):
            classify_declared_input_miss(missing=frozenset(), proven=frozenset(), payload_keys=frozenset())


class TestProofEntry:
    def test_a_node_the_proof_does_not_cover_is_refused_not_read_as_empty(self) -> None:
        with pytest.raises(OrchestrationInvariantError, match="has no entry"):
            declared_input_proof_entry({}, node_id="t1", declared=frozenset({"b"}), component="Transform 'x'")

    def test_an_entry_outside_the_declaration_is_refused(self) -> None:
        with pytest.raises(OrchestrationInvariantError, match="does not declare"):
            declared_input_proof_entry({"t1": frozenset({"z"})}, node_id="t1", declared=frozenset({"b"}), component="Transform 'x'")

    def test_the_entry_is_returned(self) -> None:
        entry = declared_input_proof_entry(
            {"t1": frozenset({"b"})}, node_id="t1", declared=frozenset({"b", "c"}), component="Transform 'x'"
        )

        assert entry == frozenset({"b"})


class TestRoutedViolation:
    def test_it_is_a_routed_plugin_contract_violation_not_tier_one(self) -> None:
        from elspeth.contracts.errors import TIER_1_ERRORS

        violation = DeclaredInputFieldAbsentViolation(component="Transform 'x'", fields=("b",))

        assert isinstance(violation, PluginContractViolation)
        assert not isinstance(violation, TIER_1_ERRORS)

    def test_its_reason_is_missing_field_with_config_names_only(self) -> None:
        reason = DeclaredInputFieldAbsentViolation(component="Transform 'x'", fields=("b", "c")).to_transform_error_reason()

        assert reason["reason"] == "missing_field"
        assert reason["fields"] == ["b", "c"]

    def test_it_needs_a_field(self) -> None:
        with pytest.raises(ValueError, match="at least one field"):
            DeclaredInputFieldAbsentViolation(component="Transform 'x'", fields=())
