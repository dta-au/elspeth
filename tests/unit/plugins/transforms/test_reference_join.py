"""Tests for ReferenceJoin — behavioral unit tests.

The load-time assertions here carry most of the weight. ``reference_join``
resolves its whole table at construction, so almost everything that can be
wrong with a configuration is detectable before a single row moves; a test that
only exercised ``process`` would leave that guarantee unasserted.
"""

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from elspeth.core.template_materialization import TemplateOptionMaterializer
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.transforms.reference_join import ReferenceJoin
from elspeth.testing import make_pipeline_row
from tests.fixtures.factories import make_source_context

if TYPE_CHECKING:
    from elspeth.contracts.plugin_context import PluginContext

OBSERVED = {"mode": "observed"}

PRODUCTS_CSV = "sku,description,price\nhats,A fine hat,2.00\ncoats,A warm coat,40.00\n"

PRODUCTS_JSON = json.dumps(
    [
        {"sku": "hats", "description": "A fine hat", "tax": {"rate": 0.1, "code": "STD"}},
        {"sku": "coats", "description": "A warm coat", "tax": {"rate": 0.2, "code": "STD"}},
        {"sku": "socks", "description": "Plain socks"},
    ]
)


def build(**overrides: Any) -> ReferenceJoin:
    options: dict[str, Any] = {
        "schema": OBSERVED,
        "reference_content": PRODUCTS_CSV,
        "reference_format": "csv",
        "key_field": "product",
        "reference_key": "sku",
        "output": {"product_description": "ref['description']"},
    }
    options.update(overrides)
    return ReferenceJoin(options)


@pytest.fixture
def ctx() -> "PluginContext":
    return make_source_context()


class TestJoinSemantics:
    def test_csv_flat_hit_adds_named_field(self, ctx: "PluginContext") -> None:
        transform = build()
        result = transform.process(make_pipeline_row({"order_id": "a", "product": "hats"}), ctx)

        assert result.status == "success"
        assert result.row is not None
        assert result.row["product_description"] == "A fine hat"
        # The join enriches; it never drops the fields it arrived with.
        assert result.row["order_id"] == "a"
        assert result.row["product"] == "hats"

    def test_json_nested_hit_resolves_a_subscript_path(self, ctx: "PluginContext") -> None:
        transform = build(
            reference_content=PRODUCTS_JSON,
            reference_format="json",
            output={"tax_rate": "ref['tax']['rate']"},
        )
        result = transform.process(make_pipeline_row({"product": "coats"}), ctx)

        assert result.status == "success"
        assert result.row is not None
        assert result.row["tax_rate"] == 0.2

    def test_multiple_outputs_from_one_match(self, ctx: "PluginContext") -> None:
        transform = build(output={"desc": "ref['description']", "list_price": "ref['price']"})
        result = transform.process(make_pipeline_row({"product": "coats"}), ctx)

        assert result.status == "success"
        assert result.row is not None
        assert result.row["desc"] == "A warm coat"
        assert result.row["list_price"] == "40.00"

    def test_declared_output_fields_are_the_output_map_keys(self) -> None:
        transform = build(output={"desc": "ref['description']", "list_price": "ref['price']"})
        assert transform.declared_output_fields == frozenset({"desc", "list_price"})

    def test_numeric_reference_key_matches_a_string_row_value(self, ctx: "PluginContext") -> None:
        """A CSV source gives "42"; a JSON table may hold 42. They must join."""
        transform = build(
            reference_content=json.dumps([{"sku": 42, "description": "Answer"}]),
            reference_format="json",
            output={"desc": "ref['description']"},
        )
        result = transform.process(make_pipeline_row({"product": "42"}), ctx)

        assert result.status == "success"
        assert result.row is not None
        assert result.row["desc"] == "Answer"


class TestMissPolicy:
    """A key miss and an unresolvable path are governed by ONE policy.

    Both branches are asserted for every mode, because the two cases take
    different routes through ``process`` and a policy that covered only the
    first would still look correct from the outside.
    """

    def test_key_miss_fails_the_row_by_default(self, ctx: "PluginContext") -> None:
        transform = build()
        result = transform.process(make_pipeline_row({"product": "gloves"}), ctx)

        assert result.status == "error"
        assert result.reason is not None
        assert result.reason["reason"] == "reference_miss"
        assert result.reason["reference_key_value"] == "gloves"
        assert result.reason["unresolved_fields"] == ["product_description"]

    def test_key_miss_writes_null(self, ctx: "PluginContext") -> None:
        transform = build(on_miss="null")
        result = transform.process(make_pipeline_row({"product": "gloves"}), ctx)

        assert result.status == "success"
        assert result.row is not None
        assert result.row["product_description"] is None

    def test_key_miss_writes_the_default(self, ctx: "PluginContext") -> None:
        transform = build(on_miss="default", default_values={"product_description": "Unknown product"})
        result = transform.process(make_pipeline_row({"product": "gloves"}), ctx)

        assert result.status == "success"
        assert result.row is not None
        assert result.row["product_description"] == "Unknown product"

    def test_unresolved_path_in_a_matched_entry_fails_the_row(self, ctx: "PluginContext") -> None:
        transform = build(
            reference_content=PRODUCTS_JSON,
            reference_format="json",
            output={"tax_rate": "ref['tax']['rate']"},
        )
        # "socks" matches, but carries no tax member.
        result = transform.process(make_pipeline_row({"product": "socks"}), ctx)

        assert result.status == "error"
        assert result.reason is not None
        assert result.reason["reason"] == "reference_miss"
        assert result.reason["unresolved_fields"] == ["tax_rate"]
        assert "did not resolve" in result.reason["error"]

    def test_unresolved_path_nulls_only_that_field(self, ctx: "PluginContext") -> None:
        transform = build(
            reference_content=PRODUCTS_JSON,
            reference_format="json",
            on_miss="null",
            output={"desc": "ref['description']", "tax_rate": "ref['tax']['rate']"},
        )
        result = transform.process(make_pipeline_row({"product": "socks"}), ctx)

        assert result.status == "success"
        assert result.row is not None
        assert result.row["tax_rate"] is None
        # The sibling output still resolved, so nulling must not be all-or-nothing.
        assert result.row["desc"] == "Plain socks"

    def test_unresolved_path_takes_the_default(self, ctx: "PluginContext") -> None:
        transform = build(
            reference_content=PRODUCTS_JSON,
            reference_format="json",
            on_miss="default",
            output={"desc": "ref['description']", "tax_rate": "ref['tax']['rate']"},
            default_values={"desc": "?", "tax_rate": 0.0},
        )
        result = transform.process(make_pipeline_row({"product": "socks"}), ctx)

        assert result.status == "success"
        assert result.row is not None
        assert result.row["tax_rate"] == 0.0
        assert result.row["desc"] == "Plain socks"

    def test_missing_join_column_is_a_row_error_not_a_crash(self, ctx: "PluginContext") -> None:
        transform = build()
        result = transform.process(make_pipeline_row({"order_id": "a"}), ctx)

        assert result.status == "error"
        assert result.reason is not None
        assert result.reason["field"] == "product"


class TestLoadTimeRejection:
    def test_duplicate_reference_keys_are_refused(self) -> None:
        with pytest.raises(PluginConfigError) as exc:
            build(reference_content="sku,description\nhats,One\nhats,Two\n")

        message = str(exc.value)
        assert "duplicate reference key" in message
        assert "'hats'" in message
        # The positions are the actionable half — say WHICH entries collided.
        assert "0" in message and "1" in message

    def test_key_field_naming_an_output_field_is_refused(self) -> None:
        """The live invariant gate mutates key_field this way and requires refusal."""
        with pytest.raises(PluginConfigError) as exc:
            build(key_field="product_description")

        assert "key_field" in str(exc.value)

    def test_empty_output_map_is_refused(self) -> None:
        with pytest.raises(PluginConfigError):
            build(output={})

    def test_default_values_naming_an_unknown_field_is_refused(self) -> None:
        with pytest.raises(PluginConfigError) as exc:
            build(on_miss="default", default_values={"product_description": "x", "typo_field": "y"})

        assert "typo_field" in str(exc.value)

    def test_on_miss_default_without_a_default_is_refused(self) -> None:
        with pytest.raises(PluginConfigError) as exc:
            build(on_miss="default")

        assert "product_description" in str(exc.value)

    def test_unparseable_json_is_refused_at_load(self) -> None:
        with pytest.raises(PluginConfigError) as exc:
            build(reference_content="{not json", reference_format="json")

        assert "not valid JSON" in str(exc.value)

    def test_a_top_level_json_object_is_refused(self) -> None:
        with pytest.raises(PluginConfigError) as exc:
            build(reference_content=json.dumps({"hats": {"description": "A fine hat"}}), reference_format="json")

        assert "array of objects" in str(exc.value)

    def test_a_missing_reference_key_column_is_refused(self) -> None:
        with pytest.raises(PluginConfigError) as exc:
            build(reference_content="code,description\nhats,A fine hat\n")

        assert "sku" in str(exc.value)

    def test_a_path_matching_no_entry_at_all_is_refused(self) -> None:
        """Sparse data resolves somewhere; a typo resolves nowhere."""
        with pytest.raises(PluginConfigError) as exc:
            build(output={"desc": "ref['descriptoin']"})

        assert "resolved against none" in str(exc.value)

    def test_an_invalid_expression_names_the_output_field(self) -> None:
        with pytest.raises(PluginConfigError) as exc:
            build(output={"desc": "__import__('os').system('x')"})

        assert "desc" in str(exc.value)

    def test_an_expression_addressing_row_is_refused(self) -> None:
        """Only the matched entry is in scope; 'row' is not a second grammar."""
        with pytest.raises(PluginConfigError):
            build(output={"desc": "row['description']"})


class TestDeliveryPathsAgree:
    """The CLI file path and the web inline-content path must be indistinguishable.

    This is the regression test for the type mismatch the design exists to
    avoid: the loader used to be able to hand this option a PARSED value while
    blob substitution hands it a STRING.
    """

    def test_materialized_file_and_inline_string_produce_identical_rows(self, tmp_path: Path, ctx: "PluginContext") -> None:
        settings_path = tmp_path / "settings.yaml"
        settings_path.write_text("transforms: []\n", encoding="utf-8")
        (tmp_path / "products.csv").write_text(PRODUCTS_CSV, encoding="utf-8")

        materialized = TemplateOptionMaterializer(settings_path).materialize_options(
            {
                "schema": OBSERVED,
                "reference_file": "products.csv",
                "reference_format": "csv",
                "key_field": "product",
                "reference_key": "sku",
                "output": {"product_description": "ref['description']"},
            }
        )

        # The loader must hand over TEXT, exactly as blob substitution does.
        assert isinstance(materialized["reference_content"], str)
        assert materialized["reference_source"] == "products.csv"
        assert "reference_file" not in materialized

        row = {"order_id": "a", "product": "hats"}
        from_file = ReferenceJoin(materialized).process(make_pipeline_row(row), ctx)
        from_inline = build().process(make_pipeline_row(row), ctx)

        assert from_file.status == from_inline.status == "success"
        assert from_file.row is not None and from_inline.row is not None
        assert from_file.row.to_dict() == from_inline.row.to_dict()

    def test_a_reference_file_outside_the_config_directory_is_blocked(self, tmp_path: Path) -> None:
        from elspeth.core.template_materialization import TemplateFileError

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "settings.yaml").write_text("transforms: []\n", encoding="utf-8")
        (tmp_path / "outside.csv").write_text(PRODUCTS_CSV, encoding="utf-8")

        with pytest.raises(TemplateFileError) as exc:
            TemplateOptionMaterializer(config_dir / "settings.yaml").materialize_options({"reference_file": "../outside.csv"})

        assert "traversal" in str(exc.value)

    def test_the_web_loader_refuses_reference_file_and_says_what_to_inline(self) -> None:
        """Registration is what closes web/paths.py's key-name allowlist hole."""
        with pytest.raises(ValueError) as exc:
            TemplateOptionMaterializer.reject_file_backed_options(
                {"transforms": [{"name": "enrich", "options": {"reference_file": "products.csv"}}]}
            )

        message = str(exc.value)
        assert "reference_file" in message
        # The remediation list is derived from the registry, so our key is in it.
        assert "reference_content" in message
