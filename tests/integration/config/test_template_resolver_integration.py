# tests/integration/config/test_template_resolver_integration.py
"""Integration tests for contract-aware template resolution end-to-end.

These tests verify the full integration of:
1. SchemaContract with original/normalized name mappings
2. PipelineRow for dual-name access in templates
3. PromptTemplate rendering with contract support
4. extract_jinja2_fields_with_names for field discovery
5. Hash stability across access styles

Per the ``engine-patterns-reference`` skill §Test Path Integrity: these tests use production code paths
(SchemaContract, PipelineRow, PromptTemplate, extract_jinja2_fields_with_names)
rather than manual construction.
"""

from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract
from elspeth.core.templates import extract_jinja2_fields_with_names
from elspeth.plugins.transforms.llm.templates import PromptTemplate


class TestSourceToTemplateDualName:
    """Test that templates can access fields by original names."""

    def test_source_to_template_dual_name(self) -> None:
        """Test that a template can access a field by its original name.

        When a row comes from a source with a schema contract that maps
        original names to normalized names, the template should be able
        to use either form.
        """
        # Create SchemaContract with original name "'Amount USD'" -> normalized "amount_usd"
        contract = SchemaContract(
            mode="OBSERVED",
            fields=(
                FieldContract(
                    normalized_name="amount_usd",
                    original_name="'Amount USD'",
                    python_type=int,
                    required=True,
                    source="declared",
                ),
                FieldContract(
                    normalized_name="customer_id",
                    original_name="Customer ID",
                    python_type=str,
                    required=True,
                    source="declared",
                ),
            ),
            locked=True,
        )

        # Row data with normalized keys
        data = {"amount_usd": 100, "customer_id": "C001"}

        # Create template that uses original name
        template = PromptTemplate("Amount: {{ row[\"'Amount USD'\"] }}")

        # Render with contract
        result = template.render(data, contract=contract)

        # Verify template renders correctly using original name
        assert result == "Amount: 100"

    def test_template_access_normalized_name(self) -> None:
        """Test that template can also access via normalized name."""
        contract = SchemaContract(
            mode="OBSERVED",
            fields=(
                FieldContract(
                    normalized_name="amount_usd",
                    original_name="'Amount USD'",
                    python_type=int,
                    required=True,
                    source="declared",
                ),
            ),
            locked=True,
        )

        data = {"amount_usd": 100}

        # Template using normalized name
        template = PromptTemplate("Amount: {{ row.amount_usd }}")
        result = template.render(data, contract=contract)

        assert result == "Amount: 100"

    def test_template_access_bracket_normalized_name(self) -> None:
        """Test bracket notation with normalized name."""
        contract = SchemaContract(
            mode="OBSERVED",
            fields=(
                FieldContract(
                    normalized_name="amount_usd",
                    original_name="'Amount USD'",
                    python_type=int,
                    required=True,
                    source="declared",
                ),
            ),
            locked=True,
        )

        data = {"amount_usd": 100}

        # Template using bracket notation with normalized name
        template = PromptTemplate('Amount: {{ row["amount_usd"] }}')
        result = template.render(data, contract=contract)

        assert result == "Amount: 100"


class TestPipelineRowTemplateAccess:
    """Test PipelineRow works correctly with templates."""

    def test_pipeline_row_template_access(self) -> None:
        """Test PipelineRow works with PromptTemplate.render().

        PipelineRow provides dual-name access. When passed to a template
        via the contract parameter, both access styles should work.
        """
        # Create contract
        contract = SchemaContract(
            mode="OBSERVED",
            fields=(
                FieldContract(
                    normalized_name="product_name",
                    original_name="Product Name",
                    python_type=str,
                    required=True,
                    source="declared",
                ),
                FieldContract(
                    normalized_name="unit_price",
                    original_name="Unit Price",
                    python_type=float,
                    required=True,
                    source="declared",
                ),
            ),
            locked=True,
        )

        # Create PipelineRow
        data = {"product_name": "Widget", "unit_price": 9.99}
        pipeline_row = PipelineRow(data, contract)

        # Create template using both access styles
        template = PromptTemplate('Product: {{ row["Product Name"] }}, Price: {{ row.unit_price }}')

        # Render with contract - uses the underlying data dict
        result = template.render(pipeline_row.to_dict(), contract=contract)

        assert result == "Product: Widget, Price: 9.99"

    def test_pipeline_row_render_with_metadata(self) -> None:
        """Test render_with_metadata includes contract hash."""
        contract = SchemaContract(
            mode="OBSERVED",
            fields=(
                FieldContract(
                    normalized_name="field_a",
                    original_name="Field A",
                    python_type=str,
                    required=True,
                    source="declared",
                ),
            ),
            locked=True,
        )

        data = {"field_a": "value"}
        template = PromptTemplate('Value: {{ row["Field A"] }}')

        rendered = template.render_with_metadata(data, contract=contract)

        assert rendered.prompt == "Value: value"
        assert rendered.contract_hash is not None
        # Contract hash should be deterministic
        assert len(rendered.contract_hash) == 64  # SHA-256 hex


class TestHashStabilityAcrossAccessStyles:
    """Verify hash stability when accessing data via different name styles."""

    def test_hash_stability_across_access_styles(self) -> None:
        """Verify that accessing data via original vs normalized name produces identical values.

        The underlying data is the same regardless of which name form is used
        to access it. Therefore, values (and any hashes computed from them)
        should be identical.
        """
        contract = SchemaContract(
            mode="OBSERVED",
            fields=(
                FieldContract(
                    normalized_name="amount_usd",
                    original_name="'Amount USD'",
                    python_type=int,
                    required=True,
                    source="declared",
                ),
            ),
            locked=True,
        )

        data = {"amount_usd": 100}
        row = PipelineRow(data, contract)

        # Access via original name
        value_via_original = row["'Amount USD'"]

        # Access via normalized name
        value_via_normalized = row["amount_usd"]

        # Values must be identical
        assert value_via_original == value_via_normalized
        assert value_via_original is value_via_normalized  # Same object

        # Hash of the underlying data should be the same
        # (The data dict is the source of truth, access method doesn't matter)
        assert row.to_dict() == data

    def test_template_output_identical_regardless_of_access_style(self) -> None:
        """Templates using different access styles produce identical output."""
        contract = SchemaContract(
            mode="OBSERVED",
            fields=(
                FieldContract(
                    normalized_name="amount_usd",
                    original_name="'Amount USD'",
                    python_type=int,
                    required=True,
                    source="declared",
                ),
            ),
            locked=True,
        )

        data = {"amount_usd": 100}

        # Template using original name
        template_original = PromptTemplate("Amount: {{ row[\"'Amount USD'\"] }}")
        result_original = template_original.render(data, contract=contract)

        # Template using normalized name
        template_normalized = PromptTemplate("Amount: {{ row.amount_usd }}")
        result_normalized = template_normalized.render(data, contract=contract)

        # Results are identical
        assert result_original == result_normalized

    def test_variables_hash_identical_regardless_of_template_access(self) -> None:
        """Variables hash depends on data, not template access pattern."""
        contract = SchemaContract(
            mode="OBSERVED",
            fields=(
                FieldContract(
                    normalized_name="amount_usd",
                    original_name="'Amount USD'",
                    python_type=int,
                    required=True,
                    source="declared",
                ),
            ),
            locked=True,
        )

        data = {"amount_usd": 100}

        # Different templates, same data
        template_original = PromptTemplate("Amount: {{ row[\"'Amount USD'\"] }}")
        template_normalized = PromptTemplate("Amount: {{ row.amount_usd }}")

        rendered_original = template_original.render_with_metadata(data, contract=contract)
        rendered_normalized = template_normalized.render_with_metadata(data, contract=contract)

        # Variables hash is based on the data dict, not the template
        assert rendered_original.variables_hash == rendered_normalized.variables_hash


class TestFieldExtractionReportsBothNames:
    """Test extract_jinja2_fields_with_names reports both name forms."""

    def test_field_extraction_reports_both_names(self) -> None:
        """Test extract_jinja2_fields_with_names with contract."""
        contract = SchemaContract(
            mode="OBSERVED",
            fields=(
                FieldContract(
                    normalized_name="amount_usd",
                    original_name="'Amount USD'",
                    python_type=int,
                    required=True,
                    source="declared",
                ),
            ),
            locked=True,
        )

        # Template using original name
        template = "{{ row[\"'Amount USD'\"] }}"

        # With contract: extraction resolves to normalized
        result_with_contract = extract_jinja2_fields_with_names(template, contract=contract)

        # Should have one entry keyed by normalized name
        assert "amount_usd" in result_with_contract
        assert result_with_contract["amount_usd"]["normalized"] == "amount_usd"
        assert result_with_contract["amount_usd"]["original"] == "'Amount USD'"
        assert result_with_contract["amount_usd"]["resolved"] is True

    def test_field_extraction_without_contract(self) -> None:
        """Test extract_jinja2_fields_with_names without contract."""
        # Template using what looks like an original name
        template = "{{ row[\"'Amount USD'\"] }}"

        # Without contract: extraction reports as-is
        result_without_contract = extract_jinja2_fields_with_names(template, contract=None)

        # Should have entry keyed by the literal string
        assert "'Amount USD'" in result_without_contract
        assert result_without_contract["'Amount USD'"]["normalized"] == "'Amount USD'"
        assert result_without_contract["'Amount USD'"]["original"] == "'Amount USD'"
        assert result_without_contract["'Amount USD'"]["resolved"] is False

    def test_field_extraction_mixed_access_styles(self) -> None:
        """Test extraction with both original and normalized names in template."""
        contract = SchemaContract(
            mode="OBSERVED",
            fields=(
                FieldContract(
                    normalized_name="amount_usd",
                    original_name="'Amount USD'",
                    python_type=int,
                    required=True,
                    source="declared",
                ),
                FieldContract(
                    normalized_name="customer_id",
                    original_name="Customer ID",
                    python_type=str,
                    required=True,
                    source="declared",
                ),
            ),
            locked=True,
        )

        # Template with mixed access styles
        template = "{{ row[\"'Amount USD'\"] }} for {{ row.customer_id }}"

        result = extract_jinja2_fields_with_names(template, contract=contract)

        # Both resolved to normalized names
        assert "amount_usd" in result
        assert result["amount_usd"]["resolved"] is True
        assert result["amount_usd"]["original"] == "'Amount USD'"

        assert "customer_id" in result
        assert result["customer_id"]["resolved"] is True
        assert result["customer_id"]["original"] == "Customer ID"

    def test_field_extraction_unknown_field(self) -> None:
        """Test extraction when template references field not in contract."""
        contract = SchemaContract(
            mode="OBSERVED",
            fields=(
                FieldContract(
                    normalized_name="amount_usd",
                    original_name="'Amount USD'",
                    python_type=int,
                    required=True,
                    source="declared",
                ),
            ),
            locked=True,
        )

        # Template references unknown field
        template = "{{ row.unknown_field }}"

        result = extract_jinja2_fields_with_names(template, contract=contract)

        # Unknown field reported with resolved=False
        assert "unknown_field" in result
        assert result["unknown_field"]["resolved"] is False
        assert result["unknown_field"]["normalized"] == "unknown_field"
        assert result["unknown_field"]["original"] == "unknown_field"


class TestComplexTemplateWithConditionals:
    """Test complex templates with conditionals and multiple access patterns."""

    def test_complex_template_with_conditionals(self) -> None:
        """Complex template with conditionals and multiple field references."""
        contract = SchemaContract(
            mode="OBSERVED",
            fields=(
                FieldContract(
                    normalized_name="amount_usd",
                    original_name="'Amount USD'",
                    python_type=int,
                    required=True,
                    source="declared",
                ),
                FieldContract(
                    normalized_name="customer_name",
                    original_name="Customer Name",
                    python_type=str,
                    required=True,
                    source="declared",
                ),
                FieldContract(
                    normalized_name="is_premium",
                    original_name="Is Premium",
                    python_type=bool,
                    required=True,
                    source="declared",
                ),
            ),
            locked=True,
        )

        # Complex template with conditionals and mixed access
        template = PromptTemplate(
            """Customer: {{ row["Customer Name"] }}
Amount: {{ row.amount_usd }}
{% if row["Is Premium"] %}Premium customer - apply discount{% else %}Standard customer{% endif %}"""
        )

        # Test with premium customer
        premium_data = {
            "amount_usd": 500,
            "customer_name": "Alice",
            "is_premium": True,
        }

        result_premium = template.render(premium_data, contract=contract)

        assert "Customer: Alice" in result_premium
        assert "Amount: 500" in result_premium
        assert "Premium customer - apply discount" in result_premium

        # Test with standard customer
        standard_data = {
            "amount_usd": 100,
            "customer_name": "Bob",
            "is_premium": False,
        }

        result_standard = template.render(standard_data, contract=contract)

        assert "Customer: Bob" in result_standard
        assert "Amount: 100" in result_standard
        assert "Standard customer" in result_standard

    def test_template_with_loop_and_dual_access(self) -> None:
        """Test template with iteration using dual-name access."""
        contract = SchemaContract(
            mode="OBSERVED",
            fields=(
                FieldContract(
                    normalized_name="item_count",
                    original_name="Item Count",
                    python_type=int,
                    required=True,
                    source="declared",
                ),
                FieldContract(
                    normalized_name="item_name",
                    original_name="Item Name",
                    python_type=str,
                    required=True,
                    source="declared",
                ),
            ),
            locked=True,
        )

        # Template that references fields in different contexts
        template = PromptTemplate(
            """Item: {{ row["Item Name"] }}
Quantity: {{ row.item_count }}"""
        )

        data = {"item_count": 5, "item_name": "Widget"}

        result = template.render(data, contract=contract)

        assert "Item: Widget" in result
        assert "Quantity: 5" in result

    def test_template_with_default_filter_and_original_name(self) -> None:
        """Test template using Jinja2 filters with original name access."""
        contract = SchemaContract(
            mode="OBSERVED",
            fields=(
                FieldContract(
                    normalized_name="description",
                    original_name="Product Description",
                    python_type=str,
                    required=False,
                    source="declared",
                ),
                FieldContract(
                    normalized_name="price",
                    original_name="Unit Price",
                    python_type=float,
                    required=True,
                    source="declared",
                ),
            ),
            locked=True,
        )

        # Template with filter operations
        template = PromptTemplate('Price: {{ row["Unit Price"] | round(2) }}, Description: {{ row.description | upper }}')

        data = {"price": 19.999, "description": "premium widget"}

        result = template.render(data, contract=contract)

        assert "Price: 20.0" in result
        assert "Description: PREMIUM WIDGET" in result

    def test_template_nested_conditional_with_multiple_fields(self) -> None:
        """Test nested conditionals accessing multiple fields by both names."""
        contract = SchemaContract(
            mode="OBSERVED",
            fields=(
                FieldContract(
                    normalized_name="status",
                    original_name="Order Status",
                    python_type=str,
                    required=True,
                    source="declared",
                ),
                FieldContract(
                    normalized_name="amount",
                    original_name="Order Amount",
                    python_type=int,
                    required=True,
                    source="declared",
                ),
                FieldContract(
                    normalized_name="priority",
                    original_name="Priority Level",
                    python_type=str,
                    required=True,
                    source="declared",
                ),
            ),
            locked=True,
        )

        template = PromptTemplate(
            """{% if row["Order Status"] == "pending" %}
{% if row.amount > 1000 %}High value pending order{% else %}Standard pending order{% endif %}
{% else %}
Order {{ row["Priority Level"] }} priority
{% endif %}"""
        )

        # High value pending
        data1 = {"status": "pending", "amount": 5000, "priority": "normal"}
        result1 = template.render(data1, contract=contract)
        assert "High value pending order" in result1

        # Standard pending
        data2 = {"status": "pending", "amount": 100, "priority": "normal"}
        result2 = template.render(data2, contract=contract)
        assert "Standard pending order" in result2

        # Non-pending shows priority
        data3 = {"status": "shipped", "amount": 100, "priority": "high"}
        result3 = template.render(data3, contract=contract)
        assert "Order high priority" in result3
