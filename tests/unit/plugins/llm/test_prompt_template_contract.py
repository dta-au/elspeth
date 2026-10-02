# tests/plugins/llm/test_prompt_template_contract.py
"""Tests for PromptTemplate with SchemaContract support."""

import pytest

from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.plugins.infrastructure.templates import DeclaredFields, TemplateRow
from elspeth.plugins.transforms.llm.templates import PromptTemplate, RenderedPrompt
from elspeth.testing import make_field


def _template_row(data: dict[str, object], contract: SchemaContract) -> TemplateRow:
    """The row as a node declaring every contract field sees it: both spellings of each (ADR-051)."""
    return TemplateRow.project(PipelineRow(data, contract), DeclaredFields(frozenset(fc.normalized_name for fc in contract.fields)))


class TestPromptTemplateWithContract:
    """Test PromptTemplate with contract-aware rendering."""

    @pytest.fixture
    def contract(self) -> SchemaContract:
        """Contract with original name mappings."""
        return SchemaContract(
            mode="FLEXIBLE",
            fields=(
                make_field("amount_usd", int, original_name="'Amount USD'", required=True, source="declared"),
                make_field("customer_name", str, original_name="Customer Name", required=True, source="declared"),
            ),
            locked=True,
        )

    @pytest.fixture
    def data(self) -> dict[str, object]:
        """Sample row data."""
        return {
            "amount_usd": 100,
            "customer_name": "Alice",
        }

    def test_render_with_contract_normalized(self, data: dict[str, object], contract: SchemaContract) -> None:
        """Contract-aware render works with normalized names."""
        template = PromptTemplate("Amount: {{ row.amount_usd }}")

        result = template.render(_template_row(data, contract))

        assert result == "Amount: 100"

    def test_render_with_contract_original(self, data: dict[str, object], contract: SchemaContract) -> None:
        """Contract-aware render works with original names."""
        template = PromptTemplate("Amount: {{ row[\"'Amount USD'\"] }}")

        result = template.render(_template_row(data, contract))

        assert result == "Amount: 100"

    def test_render_without_contract_still_works(self, data: dict[str, object]) -> None:
        """Render without contract works (backwards compatible)."""
        template = PromptTemplate("Amount: {{ row.amount_usd }}")

        result = template.render(data)  # No contract

        assert result == "Amount: 100"

    def test_render_with_metadata_preserves_hash_stability(self, data: dict[str, object], contract: SchemaContract) -> None:
        """Hash is computed from normalized data (deterministic)."""
        template = PromptTemplate("{{ row.amount_usd }}")

        # Render with original name access
        result1 = template.render_with_metadata(_template_row(data, contract), contract=contract)

        # Render with normalized name access (same template different style)
        template2 = PromptTemplate("{{ row['amount_usd'] }}")
        result2 = template2.render_with_metadata(_template_row(data, contract), contract=contract)

        # Same data = same variables_hash
        assert result1.variables_hash == result2.variables_hash

    def test_render_with_metadata_includes_contract_hash(self, data: dict[str, object], contract: SchemaContract) -> None:
        """Rendered metadata includes contract hash when provided."""
        template = PromptTemplate("{{ row.amount_usd }}")

        result = template.render_with_metadata(_template_row(data, contract), contract=contract)

        assert isinstance(result, RenderedPrompt)
        assert result.contract_hash is not None

    def test_render_with_metadata_no_contract_hash_when_none(self, data: dict[str, object]) -> None:
        """Rendered metadata has no contract hash when contract not provided."""
        template = PromptTemplate("{{ row.amount_usd }}")

        result = template.render_with_metadata(data)  # No contract

        assert result.contract_hash is None

    def test_mixed_access_in_complex_template(self, data: dict[str, object], contract: SchemaContract) -> None:
        """Complex template with mixed access styles works."""
        template = PromptTemplate("""
Customer: {{ row['Customer Name'] }}
Amount: {{ row.amount_usd }}
High value: {% if row["'Amount USD'"] > 50 %}YES{% else %}NO{% endif %}
""")

        result = template.render(_template_row(data, contract))

        assert "Customer: Alice" in result
        assert "Amount: 100" in result
        assert "High value: YES" in result

    def test_original_name_not_in_data_uses_contract(self, contract: SchemaContract) -> None:
        """Original name access works even though data has normalized keys."""
        # Data has normalized keys only
        data = {"amount_usd": 100, "customer_name": "Bob"}

        template = PromptTemplate("{{ row[\"'Amount USD'\"] }}")

        # Without contract, this would fail
        result = template.render(_template_row(data, contract))

        assert result == "100"


class TestContractHashStability:
    """Tests for contract hash computation."""

    @pytest.fixture
    def contract(self) -> SchemaContract:
        """Contract for testing."""
        return SchemaContract(
            mode="FLEXIBLE",
            fields=(
                make_field("amount_usd", int, original_name="'Amount USD'", required=True, source="declared"),
                make_field("customer_name", str, original_name="Customer Name", required=True, source="declared"),
            ),
            locked=True,
        )

    def test_contract_hash_is_deterministic(self, contract: SchemaContract) -> None:
        """Same contract produces same hash across renders."""
        template = PromptTemplate("{{ row.amount_usd }}")
        data = {"amount_usd": 100, "customer_name": "Alice"}

        result1 = template.render_with_metadata(_template_row(data, contract), contract=contract)
        result2 = template.render_with_metadata(_template_row(data, contract), contract=contract)

        assert result1.contract_hash == result2.contract_hash

    def test_different_contracts_have_different_hashes(self) -> None:
        """Different contracts produce different hashes."""
        contract1 = SchemaContract(
            mode="FLEXIBLE",
            fields=(make_field("amount_usd", int, original_name="'Amount USD'", required=True, source="declared"),),
            locked=True,
        )
        contract2 = SchemaContract(
            mode="FLEXIBLE",
            fields=(
                make_field("amount_usd", str, original_name="'Amount USD'", required=True, source="declared"),  # Different type
            ),
            locked=True,
        )

        template = PromptTemplate("{{ row.amount_usd }}")
        data = {"amount_usd": 100}

        result1 = template.render_with_metadata(data, contract=contract1)
        result2 = template.render_with_metadata(data, contract=contract2)

        assert result1.contract_hash != result2.contract_hash

    def test_contract_hash_field_order_independent(self) -> None:
        """Contract hash is independent of field declaration order."""
        contract1 = SchemaContract(
            mode="FLEXIBLE",
            fields=(
                make_field("a", int, original_name="A", required=True, source="declared"),
                make_field("b", str, original_name="B", required=True, source="declared"),
            ),
            locked=True,
        )
        # Same fields, different order
        contract2 = SchemaContract(
            mode="FLEXIBLE",
            fields=(
                make_field("b", str, original_name="B", required=True, source="declared"),
                make_field("a", int, original_name="A", required=True, source="declared"),
            ),
            locked=True,
        )

        template = PromptTemplate("{{ row.a }}")
        data = {"a": 100, "b": "test"}

        result1 = template.render_with_metadata(data, contract=contract1)
        result2 = template.render_with_metadata(data, contract=contract2)

        # Hash should be the same regardless of field order
        assert result1.contract_hash == result2.contract_hash
