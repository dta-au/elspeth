"""The shipped multi-CSV example preserves defaults and atomic quarantine."""

import csv
import json
import shutil
from pathlib import Path

import pytest
import yaml

from tests.integration.pipeline.test_declared_input_miss import _cli, _query, _routed_reasons, _settings, _terminal_outcomes
from tests.unit.plugins.transforms.test_reference_join_multi import chained_options


def test_chained_csv_example_reuses_shared_key_and_quarantines_original(tmp_path: Path) -> None:
    example = Path("examples/reference_join")
    config = yaml.safe_load((example / "settings_chained.yaml").read_text())
    for filename in ("orders_chained.csv", "product_aliases.csv", "products.csv", "fulfilment.csv"):
        shutil.copyfile(example / filename, tmp_path / filename)
    config["sources"]["primary"]["options"]["path"] = str(tmp_path / "orders_chained.csv")
    config["sinks"]["output"]["options"]["path"] = str(tmp_path / "out.csv")
    config["sinks"]["quarantine"]["options"]["path"] = str(tmp_path / "quarantine.csv")
    config["landscape"]["url"] = f"sqlite:///{tmp_path / 'audit.db'}"
    config["payload_store"] = {"backend": "filesystem", "base_path": str(tmp_path / "payloads")}
    settings = tmp_path / "settings.yaml"
    settings.write_text(yaml.safe_dump(config, sort_keys=False))

    result = _cli("run", "--settings", str(settings), "--execute")

    assert result.exit_code == 1, result.output
    with (tmp_path / "out.csv").open(newline="") as stream:
        enriched = list(csv.DictReader(stream))
    assert [(row["order_id"], row["product_code"], row["unit_price"], row["warehouse"]) for row in enriched] == [
        ("C-1001", "hats", "25.00", "Canberra"),
        ("C-1002", "socks", "18.50", "Melbourne"),
        ("C-1004", "coats", "180.00", "Sydney"),
    ]
    with (tmp_path / "orders_chained.csv").open(newline="") as stream:
        original = list(csv.DictReader(stream))
    with (tmp_path / "quarantine.csv").open(newline="") as stream:
        assert list(csv.DictReader(stream)) == [original[2]]
    assert _terminal_outcomes(tmp_path) == {"success/default_flow": 3, "failure/on_error_routed": 1}
    (reason,) = _routed_reasons(tmp_path)
    assert (reason["reason"], reason["lookup_index"], reason["table"]) == ("reference_miss", 2, "fulfilment")
    reasons = [
        json.loads(text)
        for (text,) in _query(tmp_path, "select success_reason_json from node_states where success_reason_json is not null")
    ]
    lookups = [reason["metadata"]["lookups"] for reason in reasons if reason["action"] == "enriched"]
    assert len(lookups) == 3
    assert sum(not row[0]["matched"] for row in lookups) == 1
    assert all(row[1]["matched"] and row[2]["matched"] for row in lookups)


def test_chained_typed_intermediate_is_not_required_on_arrival(tmp_path: Path) -> None:
    options = chained_options()
    options["schema"] = {"mode": "flexible", "fields": ["product: str", "shared_key: str"]}
    settings = _settings(
        tmp_path,
        [{"product": "hat"}],
        plugin="reference_join",
        options=options,
        source_schema={"mode": "fixed", "fields": ["product: str"]},
    )
    result = _cli("run", "--settings", str(settings), "--execute")
    assert result.exit_code == 0, result.output
    assert json.loads((tmp_path / "out.jsonl").read_text()) == {
        "product": "hat",
        "shared_key": "H",
        "region": "Sydney",
        "tax_rate": "0.10",
    }


@pytest.mark.parametrize("case", ["missing_external", "output_collision"])
def test_chained_closed_schema_rejects_unavailable_keys_and_arriving_collisions(tmp_path: Path, case: str) -> None:
    options = chained_options()
    fields = ["product: str"]
    if case == "missing_external":
        options["lookups"][2]["key_field"] = "missing_key"
    else:
        fields.append("shared_key: str")
    settings = _settings(
        tmp_path,
        [{"product": "hat"}],
        plugin="reference_join",
        options=options,
        source_schema={"mode": "fixed", "fields": fields},
    )
    result = _cli("validate", "--settings", str(settings))
    assert result.exit_code != 0, result.output
    assert ("missing_key" if case == "missing_external" else "shared_key") in result.output


def test_chained_observed_missing_external_input_is_admitted_before_lookup_processing(tmp_path: Path) -> None:
    options = chained_options()
    options["lookups"][2]["key_field"] = "missing_key"
    original = {"product": "unmatched-private-key"}
    settings = _settings(tmp_path, [original], plugin="reference_join", options=options)
    result = _cli("run", "--settings", str(settings), "--execute")
    assert result.exit_code == 2, result.output
    assert json.loads((tmp_path / "q.jsonl").read_text()) == original
    (reason,) = _routed_reasons(tmp_path)
    assert reason["reason"] == "missing_field"
    assert "missing_key" in repr(reason)
    assert "unmatched-private-key" not in repr(reason)


def test_multi_csv_example_defaults_and_whole_row_quarantine(tmp_path: Path) -> None:
    example = Path("examples/reference_join")
    config = yaml.safe_load((example / "settings_multi.yaml").read_text())
    for filename in ("orders_multi.csv", "products.csv", "regions.csv", "tax_rates.csv"):
        shutil.copyfile(example / filename, tmp_path / filename)
    config["sources"]["primary"]["options"]["path"] = str(tmp_path / "orders_multi.csv")
    config["sinks"]["output"]["options"]["path"] = str(tmp_path / "out.csv")
    config["sinks"]["quarantine"]["options"]["path"] = str(tmp_path / "quarantine.csv")
    config["landscape"]["url"] = f"sqlite:///{tmp_path / 'audit.db'}"
    config["payload_store"] = {"backend": "filesystem", "base_path": str(tmp_path / "payloads")}
    settings = tmp_path / "settings.yaml"
    settings.write_text(yaml.safe_dump(config, sort_keys=False))

    result = _cli("run", "--settings", str(settings), "--execute")

    assert result.exit_code == 1, result.output
    with (tmp_path / "out.csv").open(newline="") as stream:
        enriched = {row["order_id"]: row for row in csv.DictReader(stream)}
    assert set(enriched) == {"M-1001", "M-1002", "M-1003", "M-1005"}
    assert [
        (enriched[key]["product_description"], enriched[key]["unit_price"], enriched[key]["region"], enriched[key]["tax_rate"])
        for key in ("M-1001", "M-1002", "M-1003", "M-1005")
    ] == [
        ("Wide-brimmed sun hat", "25.00", "Metropolitan", "0.10"),
        ("Unlisted product", "0.00", "Metropolitan", "0.10"),
        ("Merino wool walking socks", "18.50", "Unmapped region", "0.00"),
        ("Unlisted product", "0.00", "Unmapped region", "0.10"),
    ]
    with (tmp_path / "orders_multi.csv").open(newline="") as stream:
        original = list(csv.DictReader(stream))
    with (tmp_path / "quarantine.csv").open(newline="") as stream:
        quarantined = list(csv.DictReader(stream))
    assert len(original) == 5
    assert quarantined == [original[3]]
    assert set(quarantined[0]) == {"order_id", "product", "region_code", "tax_code", "quantity"}
    assert _terminal_outcomes(tmp_path) == {"success/default_flow": 4, "failure/on_error_routed": 1}
    (reason,) = _routed_reasons(tmp_path)
    assert reason["reason"] == "reference_miss"
    assert reason["lookup_index"] == 2
    assert reason["table"] == "taxes"
    reasons = [
        json.loads(text)
        for (text,) in _query(tmp_path, "select success_reason_json from node_states where success_reason_json is not null")
    ]
    lookups = [reason["metadata"]["lookups"] for reason in reasons if reason["action"] == "enriched"]
    assert len(lookups) == 4
    assert sum(not row[0]["matched"] for row in lookups) == 2
    assert sum(not row[1]["matched"] for row in lookups) == 2
    assert all(row[2]["matched"] for row in lookups)
