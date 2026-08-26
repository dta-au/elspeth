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
        "reference_key_name": "sku",
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
                "reference_key_name": "sku",
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


class TestTheTableIsWholeOrItIsRefused:
    """Every defect below used to LOAD, then misbehave one row at a time.

    The unifying rule: a reference table that cannot answer a lookup honestly is
    a configuration error, and configuration errors are reported at load. The
    alternative each of these had before is worse than a crash — a value that
    reads as resolved, or a miss on every row that on_miss then papers over.
    """

    def test_a_short_csv_row_is_refused_rather_than_joining_as_a_null(self) -> None:
        """csv.DictReader pads a short row with restval; the pad must not become data.

        This is the sentinel's whole reason for existing, defeated one layer
        earlier: a None written by restval is an ordinary value, so it resolves,
        and on_miss: fail — the strictest policy there is — never sees it.
        """
        with pytest.raises(PluginConfigError) as exc:
            build(reference_content="sku,description,price\nhats,A fine hat\n", on_miss="fail")

        message = str(exc.value)
        assert "data row 1" in message
        assert "price" in message

    def test_an_empty_trailing_cell_is_a_value_not_a_short_row(self) -> None:
        """The refusal above must not swallow the ordinary case it sits next to.

        An empty cell reads as "" and is data the table chose to leave blank; a
        MISSING cell reads as None and is a row that ran out. Refusing the first
        would reject most real exports.
        """
        transform = build(reference_content="sku,description\nhats,\n", output={"d": "ref['description']"})

        result = transform.process(make_pipeline_row({"product": "hats"}), make_source_context())

        assert result.status == "success"
        assert result.row is not None
        assert result.row.to_dict()["d"] == ""

    def test_surplus_cells_are_refused_and_name_the_orphan_value(self) -> None:
        """DictReader buckets extra cells under restkey, where no expression can read them."""
        with pytest.raises(PluginConfigError) as exc:
            build(reference_content="sku,description\nhats,A fine hat,extra\n")

        assert "belongs to no column" in str(exc.value)

    def test_surplus_cells_are_refused_before_the_missing_key_column_is_sorted(self) -> None:
        """Regression: this raised a bare TypeError out of config validation.

        With the key column absent AND a surplus cell, the 'available keys'
        diagnostic sorted a dict keyed by {str, ..., None}, so the author got
        `'<' not supported between NoneType and str` where a written message
        was already waiting.
        """
        with pytest.raises(PluginConfigError) as exc:
            build(reference_content="code,description\nhats,A fine hat,extra\n")

        assert "belongs to no column" in str(exc.value)

    @pytest.mark.parametrize(
        ("label", "content", "reference_format"),
        [
            ("csv header with no data rows", "sku,description\n", "csv"),
            ("json empty array", "[]", "json"),
        ],
    )
    def test_an_empty_table_is_refused_like_any_other_empty_container(self, label: str, content: str, reference_format: str) -> None:
        """A blank file already refused; these two did not. The inconsistency was the tell.

        An empty table cannot answer any lookup, so every row misses: under the
        default on_miss: fail the run yields nothing, and under null it silently
        nulls the enrichment for the whole run.
        """
        del label
        with pytest.raises(PluginConfigError) as exc:
            build(reference_content=content, reference_format=reference_format, output={"d": "ref['description']"})

        assert "no entries" in str(exc.value)

    def test_a_broken_expression_is_refused_rather_than_becoming_a_miss(self) -> None:
        """Sparse data and a broken expression are different facts.

        Both used to arrive as _UNRESOLVED, and on_miss cannot tell them apart —
        so a division by zero on one entry was indistinguishable from a table
        that legitimately omits that path, which is what the examples teach
        operators to expect.
        """
        table = json.dumps([{"sku": "hats", "n": 1, "q": 2}, {"sku": "coats", "n": 1, "q": 0}])
        with pytest.raises(PluginConfigError) as exc:
            build(reference_content=table, reference_format="json", output={"p": "ref['n'] / ref['q']"})

        message = str(exc.value)
        assert "broken expression" in message
        assert "'coats'" in message

    def test_a_sparse_entry_is_still_governed_by_on_miss(self) -> None:
        """The other side of the discrimination above: absence stays a miss."""
        table = json.dumps([{"sku": "hats", "description": "A fine hat"}, {"sku": "coats"}])
        transform = build(reference_content=table, reference_format="json", output={"d": "ref['description']"}, on_miss="null")
        ctx_local = make_source_context()

        result = transform.process(make_pipeline_row({"product": "coats"}), ctx_local)

        assert result.status == "success"
        assert result.row is not None
        assert result.row.to_dict()["d"] is None


class TestANullIsAValueAndAMissIsNot:
    """The sentinel's stated purpose, which nothing asserted.

    ``_UNRESOLVED`` exists because ``None`` is a legitimate value a reference
    table may hold. No fixture in this file carried a table-authored null, so
    replacing ``_UNRESOLVED = object()`` with ``_UNRESOLVED = None`` left the
    whole suite green — the design's central distinction was documented and
    unverified.
    """

    def test_a_table_authored_null_is_a_hit_under_the_strictest_miss_policy(self) -> None:
        table = json.dumps([{"sku": "hats", "description": None}])
        transform = build(reference_content=table, reference_format="json", output={"d": "ref['description']"}, on_miss="fail")
        ctx_local = make_source_context()

        result = transform.process(make_pipeline_row({"product": "hats"}), ctx_local)

        assert result.status == "success", "a JSON null is a value the table holds, not a failure to resolve"
        assert result.row is not None
        assert result.row.to_dict()["d"] is None
        assert result.success_reason is not None
        assert result.success_reason["metadata"]["matched"] is True
        # The discriminator: a null that RESOLVED leaves this list empty, where
        # a path that did not fit would name the field.
        assert result.success_reason["metadata"]["unresolved_fields"] == []

    def test_an_absent_path_and_an_authored_null_are_told_apart_in_one_table(self) -> None:
        """Same field, same run: one entry holds null, the other omits the key."""
        table = json.dumps([{"sku": "hats", "description": None}, {"sku": "coats"}])
        transform = build(reference_content=table, reference_format="json", output={"d": "ref['description']"}, on_miss="null")
        ctx_local = make_source_context()

        authored_null = transform.process(make_pipeline_row({"product": "hats"}), ctx_local)
        absent_path = transform.process(make_pipeline_row({"product": "coats"}), ctx_local)

        assert authored_null.success_reason is not None
        assert absent_path.success_reason is not None
        # Both rows carry d=None. Only the audit trail distinguishes them, which
        # is exactly what the sentinel buys.
        assert authored_null.row is not None and authored_null.row.to_dict()["d"] is None
        assert absent_path.row is not None and absent_path.row.to_dict()["d"] is None
        assert authored_null.success_reason["metadata"]["unresolved_fields"] == []
        assert absent_path.success_reason["metadata"]["unresolved_fields"] == ["d"]


class TestJoinKeySpelling:
    def test_an_integral_float_joins_against_an_integer_key(self) -> None:
        """A JSON source or an upstream numeric transform can carry 42.0 for 42.

        Spelling those differently missed the join silently: under on_miss: fail
        it quarantined correct data, and under null it nulled it.
        """
        table = json.dumps([{"sku": 42, "description": "A fine hat"}])
        transform = build(reference_content=table, reference_format="json", output={"d": "ref['description']"}, on_miss="fail")
        ctx_local = make_source_context()

        for spelling in (42, "42", 42.0):
            result = transform.process(make_pipeline_row({"product": spelling}), ctx_local)
            assert result.status == "success", f"{spelling!r} should reach the same entry as 42"

    def test_a_non_integral_float_keeps_its_own_spelling(self) -> None:
        table = json.dumps([{"sku": 7.5, "description": "Half a hat"}])
        transform = build(reference_content=table, reference_format="json", output={"d": "ref['description']"}, on_miss="fail")
        ctx_local = make_source_context()

        assert transform.process(make_pipeline_row({"product": 7.5}), ctx_local).status == "success"
        assert transform.process(make_pipeline_row({"product": "7.5"}), ctx_local).status == "success"

    def test_a_bom_does_not_hide_the_key_column(self) -> None:
        """An Excel-exported CSV keeps the BOM inside its first header name.

        The refusal it produced named ['description', '﻿sku'] — which in a
        terminal reads as though 'sku' is present and was rejected anyway.
        """
        transform = build(reference_content="﻿sku,description\nhats,A fine hat\n", output={"d": "ref['description']"})
        ctx_local = make_source_context()

        result = transform.process(make_pipeline_row({"product": "hats"}), ctx_local)

        assert result.status == "success"
        assert result.row is not None
        assert result.row.to_dict()["d"] == "A fine hat"
