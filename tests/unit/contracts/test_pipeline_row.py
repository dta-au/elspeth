# tests/unit/contracts/test_pipeline_row.py
"""Tests for PipelineRow wrapper class."""

import pytest

from elspeth.contracts.schema_contract import (
    PipelineRow,
    SchemaContract,
)
from elspeth.testing import make_field, make_row


class TestPipelineRowAccess:
    """Test PipelineRow data access patterns."""

    @pytest.fixture
    def sample_row(self) -> PipelineRow:
        """Sample row with dual-name access."""
        field_a = make_field("amount_usd", int, original_name="'Amount USD'", required=True, source="declared")
        field_b = make_field("customer_id", str, original_name="Customer ID", required=True, source="declared")
        contract = SchemaContract(
            mode="FLEXIBLE",
            fields=(field_a, field_b),
            locked=True,
        )
        data = {"amount_usd": 100, "customer_id": "C123"}
        return make_row(data, contract=contract)

    def test_carried_value_reads_the_literal_key_frozen(self) -> None:
        """carried_value reads a carried key only: no alias resolution, no thaw."""
        field = make_field("tags", object, original_name="Tags", required=True, source="declared")
        contract = SchemaContract(mode="FIXED", fields=(field,), locked=True)
        row = make_row({"tags": ["a", "b"]}, contract=contract)

        assert row.carries("tags")
        assert not row.carries("Tags")
        assert "Tags" in row  # the alias-resolving check differs, which is why carries exists
        assert row.carried_value("tags") == ("a", "b")
        assert type(row.carried_value("tags")) is type(row["tags"])
        with pytest.raises(KeyError):
            row.carried_value("Tags")
        with pytest.raises(KeyError):
            row.carried_value("missing")

    def test_getitem_normalized_name(self, sample_row: PipelineRow) -> None:
        """Access by normalized name via bracket notation."""
        assert sample_row["amount_usd"] == 100

    def test_getitem_original_name(self, sample_row: PipelineRow) -> None:
        """Access by original name via bracket notation."""
        assert sample_row["'Amount USD'"] == 100

    def test_getattr_normalized_name(self, sample_row: PipelineRow) -> None:
        """Access by normalized name via dot notation."""
        assert sample_row.amount_usd == 100

    def test_getattr_unknown_raises(self, sample_row: PipelineRow) -> None:
        """Unknown attribute raises AttributeError."""
        with pytest.raises(AttributeError):
            _ = sample_row.nonexistent

    def test_getitem_unknown_raises(self, sample_row: PipelineRow) -> None:
        """Unknown key raises KeyError."""
        with pytest.raises(KeyError):
            _ = sample_row["nonexistent"]

    def test_contains_normalized(self, sample_row: PipelineRow) -> None:
        """'in' operator works with normalized name."""
        assert "amount_usd" in sample_row

    def test_contains_original(self, sample_row: PipelineRow) -> None:
        """'in' operator works with original name."""
        assert "'Amount USD'" in sample_row

    def test_contains_unknown_false(self, sample_row: PipelineRow) -> None:
        """'in' returns False for unknown fields."""
        assert "nonexistent" not in sample_row

    def test_contains_checks_actual_data_not_just_contract(self) -> None:
        """'in' checks actual data presence, not just contract membership.

        P2 fix: For optional fields that exist in contract but are missing from
        actual data, 'in' should return False so guard patterns work correctly.
        """
        field_req = make_field("required_field", str, original_name="Required", required=True, source="declared")
        field_opt = make_field("optional_field", str, original_name="Optional", required=False, source="declared")
        contract = SchemaContract(
            mode="FLEXIBLE",
            fields=(field_req, field_opt),
            locked=True,
        )
        # Data only has the required field, not the optional one
        row = make_row({"required_field": "present"}, contract=contract)

        # Required field present in both contract and data -> True
        assert "required_field" in row

        # Optional field in contract but NOT in data -> False
        # This enables guard patterns like: if "optional" in row: use(row["optional"])
        assert "optional_field" not in row

        # Unknown field not in contract or data -> False
        assert "unknown" not in row

    def test_contains_with_extra_fields_in_flexible_mode(self) -> None:
        """'in' returns True for extra fields in FLEXIBLE mode (aligns with __getitem__).

        P2 fix: __contains__ must align with __getitem__ semantics. For FLEXIBLE/OBSERVED
        contracts, __getitem__ allows access to extra fields, so __contains__ must return
        True for those fields. This consistency is critical for gate expressions that check
        membership before accessing fields ('field' in row then row['field']).
        """
        field_declared = make_field("declared", int, original_name="Declared", required=True, source="declared")
        contract = SchemaContract(
            mode="FLEXIBLE",
            fields=(field_declared,),
            locked=True,
        )
        # Data has extra field not in contract
        row = make_row({"declared": 1, "extra": "value"}, contract=contract)

        # Declared field is accessible
        assert "declared" in row
        assert row["declared"] == 1

        # Extra field IS accessible via PipelineRow in FLEXIBLE mode (aligns with __getitem__)
        assert "extra" in row
        assert row["extra"] == "value"  # __getitem__ allows this, so __contains__ must align

        # Extras are still in underlying data (via to_dict)
        assert "extra" in row.to_dict()
        assert row.to_dict()["extra"] == "value"

    def test_to_dict_returns_raw_data(self, sample_row: PipelineRow) -> None:
        """to_dict() returns raw data with normalized keys."""
        d = sample_row.to_dict()
        assert d == {"amount_usd": 100, "customer_id": "C123"}
        assert isinstance(d, dict)

    def test_contract_property(self, sample_row: PipelineRow) -> None:
        """contract property provides access to schema."""
        assert sample_row.contract.mode == "FLEXIBLE"
        assert len(sample_row.contract.fields) == 2


class TestPipelineRowCheckpoint:
    """Test PipelineRow checkpoint serialization."""

    def test_to_checkpoint_format(self) -> None:
        """to_checkpoint_format() returns dict with data and contract ref."""
        field_id = make_field("id", int, original_name="ID", required=True, source="declared")
        contract = SchemaContract(
            mode="FIXED",
            fields=(field_id,),
            locked=True,
        )
        row = make_row({"id": 42}, contract=contract)
        checkpoint = row.to_checkpoint_format()

        assert checkpoint["data"] == {"id": 42}
        assert "contract_version" in checkpoint
        assert checkpoint["contract_version"] == contract.version_hash()

    def test_from_checkpoint_round_trip(self) -> None:
        """PipelineRow survives checkpoint round-trip."""
        field_id = make_field("id", int, original_name="ID", required=True, source="declared")
        contract = SchemaContract(
            mode="FIXED",
            fields=(field_id,),
            locked=True,
        )
        original = make_row({"id": 42}, contract=contract)

        # Serialize
        checkpoint = original.to_checkpoint_format()

        # Build registry (in real use, this comes from node checkpoints)
        registry = {contract.version_hash(): contract}

        # Restore
        restored = PipelineRow.from_checkpoint(checkpoint, registry)

        assert restored["id"] == 42
        assert restored.contract.version_hash() == contract.version_hash()

    def test_from_checkpoint_unknown_contract_raises(self) -> None:
        """from_checkpoint() raises AuditIntegrityError if contract not in registry."""
        from elspeth.contracts.errors import AuditIntegrityError

        checkpoint = {
            "data": {"x": 1},
            "contract_version": "unknown_hash_123",
        }
        registry: dict[str, SchemaContract] = {}

        with pytest.raises(AuditIntegrityError, match="not in registry"):
            PipelineRow.from_checkpoint(checkpoint, registry)


class TestPipelineRowSlots:
    """Test PipelineRow memory efficiency."""

    def test_uses_slots(self) -> None:
        """PipelineRow uses __slots__ for memory efficiency."""
        contract = SchemaContract(mode="OBSERVED", fields=(), locked=True)
        row = make_row({}, contract=contract)

        # __slots__ means no __dict__
        assert not hasattr(row, "__dict__")
        assert hasattr(row, "__slots__")

    def test_cannot_add_attributes(self) -> None:
        """Cannot add arbitrary attributes (slots restriction)."""
        contract = SchemaContract(mode="OBSERVED", fields=(), locked=True)
        row = make_row({}, contract=contract)

        with pytest.raises(AttributeError):
            row.new_attr = "value"  # type: ignore[attr-defined]


class TestPipelineRowImmutability:
    """Test PipelineRow data immutability for audit integrity."""

    def test_setitem_raises_typeerror(self) -> None:
        """Cannot mutate row via bracket notation (Tier 1 integrity)."""
        field_amount = make_field("amount", int, original_name="Amount", required=True, source="declared")
        contract = SchemaContract(
            mode="FIXED",
            fields=(field_amount,),
            locked=True,
        )
        row = make_row({"amount": 100}, contract=contract)

        with pytest.raises(TypeError, match=r"immutable.*audit"):
            row["amount"] = 200

    def test_original_dict_mutation_does_not_affect_row(self) -> None:
        """Mutating original dict does not affect PipelineRow (defensive copy)."""
        field_amount = make_field("amount", int, original_name="Amount", required=True, source="declared")
        contract = SchemaContract(
            mode="FIXED",
            fields=(field_amount,),
            locked=True,
        )
        original_data = {"amount": 100}
        row = PipelineRow(original_data, contract)

        # Mutate original dict
        original_data["amount"] = 999

        # Row should still have original value
        assert row["amount"] == 100

    def test_to_dict_mutation_does_not_affect_row(self) -> None:
        """Mutating to_dict() result does not affect PipelineRow."""
        field_amount = make_field("amount", int, original_name="Amount", required=True, source="declared")
        contract = SchemaContract(
            mode="FIXED",
            fields=(field_amount,),
            locked=True,
        )
        row = make_row({"amount": 100}, contract=contract)

        # Get dict and mutate it
        d = row.to_dict()
        d["amount"] = 999

        # Row should still have original value
        assert row["amount"] == 100


class TestPipelineRowJinja2Compatibility:
    """Test Jinja2 template compatibility methods (get, keys, __iter__).

    These methods enable PipelineRow to be used directly in Jinja2 templates,
    eliminating the need for a separate ContractAwareRow wrapper.
    """

    @pytest.fixture
    def sample_row(self) -> PipelineRow:
        """Sample row for Jinja2 compatibility tests."""
        field_a = make_field("amount_usd", int, original_name="'Amount USD'", required=True, source="declared")
        field_b = make_field("customer_id", str, original_name="Customer ID", required=True, source="declared")
        field_c = make_field("simple", str, original_name="simple", required=True, source="declared")
        contract = SchemaContract(
            mode="FLEXIBLE",
            fields=(field_a, field_b, field_c),
            locked=True,
        )
        data = {"amount_usd": 100, "customer_id": "C001", "simple": "value"}
        return make_row(data, contract=contract)

    def test_get_with_normalized_name(self, sample_row: PipelineRow) -> None:
        """get() works with normalized field names."""
        assert sample_row.get("amount_usd") == 100
        assert sample_row.get("customer_id") == "C001"

    def test_get_with_original_name(self, sample_row: PipelineRow) -> None:
        """get() works with original field names (dual-name resolution)."""
        assert sample_row.get("'Amount USD'") == 100
        assert sample_row.get("Customer ID") == "C001"

    def test_get_missing_field_returns_default(self, sample_row: PipelineRow) -> None:
        """get() returns default for missing fields."""
        assert sample_row.get("nonexistent") is None
        assert sample_row.get("nonexistent", "default") == "default"
        assert sample_row.get("nonexistent", 42) == 42

    def test_keys_returns_normalized_names(self, sample_row: PipelineRow) -> None:
        """keys() returns normalized field names."""
        keys = sample_row.keys()
        assert set(keys) == {"amount_usd", "customer_id", "simple"}
        # Should NOT include original names
        assert "'Amount USD'" not in keys
        assert "Customer ID" not in keys

    def test_keys_returns_list(self, sample_row: PipelineRow) -> None:
        """keys() returns a list (not a view) for Jinja2 compatibility."""
        keys = sample_row.keys()
        assert isinstance(keys, list)

    def test_iter_yields_normalized_keys(self, sample_row: PipelineRow) -> None:
        """Iteration yields normalized field names."""
        keys = list(sample_row)
        assert set(keys) == {"amount_usd", "customer_id", "simple"}

    def test_iter_supports_for_loop(self, sample_row: PipelineRow) -> None:
        """Can use for loop over PipelineRow (Jinja2 {% for key in row %})."""
        collected = []
        for key in sample_row:
            collected.append(key)
        assert set(collected) == {"amount_usd", "customer_id", "simple"}

    def test_jinja2_template_pattern_iteration(self, sample_row: PipelineRow) -> None:
        """Simulates Jinja2 pattern: {% for key in row %}{{ row[key] }}{% endfor %}."""
        # This is the exact pattern Jinja2 uses when iterating
        result = {key: sample_row[key] for key in sample_row}
        assert result == {"amount_usd": 100, "customer_id": "C001", "simple": "value"}

    def test_jinja2_template_pattern_conditional(self, sample_row: PipelineRow) -> None:
        """Simulates Jinja2 pattern: {% if "field" in row %}{{ row.get("field") }}{% endif %}."""
        # Combination of __contains__ and get()
        if "amount_usd" in sample_row:
            value = sample_row.get("amount_usd")
            assert value == 100

        if "nonexistent" in sample_row:
            pytest.fail("Should not enter block for missing field")


class TestPipelineRowContainsGetitemConsistency:
    """Test __contains__ / __getitem__ consistency (P2-2026-02-05 bug fix).

    Gate expressions use 'field' in row checks via the 'in' operator. These must
    align with __getitem__ behavior: if 'field in row' returns True, then
    row['field'] must NOT raise KeyError.
    """

    def test_contains_and_getitem_consistency_for_declared_fields(self) -> None:
        """__contains__ and __getitem__ agree on declared fields."""
        field_declared = make_field("declared", int, original_name="Declared", required=True, source="declared")
        contract = SchemaContract(
            mode="FLEXIBLE",
            fields=(field_declared,),
            locked=True,
        )
        row = make_row({"declared": 42}, contract=contract)

        # Declared field present in data
        assert "declared" in row  # __contains__ returns True
        assert row["declared"] == 42  # __getitem__ succeeds

    def test_contains_and_getitem_consistency_for_extra_fields_flexible(self) -> None:
        """__contains__ and __getitem__ agree on extra fields in FLEXIBLE mode."""
        field_declared = make_field("declared", int, original_name="Declared", required=True, source="declared")
        contract = SchemaContract(
            mode="FLEXIBLE",
            fields=(field_declared,),
            locked=True,
        )
        row = make_row({"declared": 1, "extra_field": "extra_value"}, contract=contract)

        # Extra field in FLEXIBLE mode
        assert "extra_field" in row  # __contains__ returns True
        assert row["extra_field"] == "extra_value"  # __getitem__ succeeds

    def test_contains_and_getitem_consistency_for_extra_fields_observed(self) -> None:
        """__contains__ and __getitem__ agree on extra fields in OBSERVED mode."""
        field_observed = make_field("observed", int, original_name="Observed", required=True, source="inferred")
        contract = SchemaContract(
            mode="OBSERVED",
            fields=(field_observed,),
            locked=True,
        )
        row = make_row({"observed": 1, "extra_field": "extra_value"}, contract=contract)

        # Extra field in OBSERVED mode
        assert "extra_field" in row  # __contains__ returns True
        assert row["extra_field"] == "extra_value"  # __getitem__ succeeds

    def test_contains_and_getitem_consistency_for_missing_fields(self) -> None:
        """__contains__ and __getitem__ agree on missing fields."""
        field_declared = make_field("declared", int, original_name="Declared", required=True, source="declared")
        contract = SchemaContract(
            mode="FLEXIBLE",
            fields=(field_declared,),
            locked=True,
        )
        row = make_row({"declared": 1}, contract=contract)

        # Missing field
        assert "missing" not in row  # __contains__ returns False
        with pytest.raises(KeyError):
            _ = row["missing"]  # __getitem__ raises KeyError

    def test_gate_expression_pattern_with_extra_fields(self) -> None:
        """Gate expressions can safely check membership before accessing extra fields.

        This pattern is used in config gate expressions:
            if 'llm_response' in row: return row['llm_response']

        Before the P2 fix, 'llm_response' in row would return False (not in contract)
        even though row['llm_response'] would succeed (FLEXIBLE mode allows extras).
        This caused gates to mis-evaluate and potentially route rows incorrectly.
        """
        field_cid = make_field("customer_id", str, original_name="Customer ID", required=True, source="declared")
        contract = SchemaContract(
            mode="FLEXIBLE",
            fields=(field_cid,),
            locked=True,
        )
        # Simulate LLM transform adding response field not in contract
        row = make_row({"customer_id": "C001", "llm_response": "classified"}, contract=contract)

        # Gate pattern: check membership before access
        if "llm_response" in row:
            # This block MUST execute, and access MUST succeed
            assert row["llm_response"] == "classified"
        else:
            pytest.fail("Gate mis-evaluated: 'llm_response' in row returned False but field is accessible")
