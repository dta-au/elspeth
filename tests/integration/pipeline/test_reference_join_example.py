"""The shipped multi-CSV example preserves defaults and atomic quarantine."""

import csv
import json
import shutil
from pathlib import Path

import yaml

from tests.integration.pipeline.test_declared_input_miss import _cli, _query, _routed_reasons, _terminal_outcomes


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
