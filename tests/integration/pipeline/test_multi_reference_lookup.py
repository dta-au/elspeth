"""Multiple lookup outcomes remain atomic at the actual execution boundary."""

import json
from pathlib import Path

from tests.integration.pipeline.test_declared_input_miss import _cli, _query, _routed_reasons, _settings, _terminal_outcomes


def test_multi_lookup_quarantines_original_row_and_records_failed_lookup(tmp_path: Path) -> None:
    products = tmp_path / "products.csv"
    products.write_text("sku,description\nhat,Fine hat\n", encoding="utf-8")
    regions = tmp_path / "regions.csv"
    regions.write_text("code,region\n2000,Sydney\n", encoding="utf-8")
    original = {"product": "hat", "postcode": "unknown"}
    settings = _settings(
        tmp_path,
        [original],
        plugin="reference_join",
        options={
            "tables": {
                "products": {"reference_file": "products.csv", "reference_format": "csv"},
                "regions": {"reference_file": "regions.csv", "reference_format": "csv"},
            },
            "lookups": [
                {"table": "products", "key_field": "product", "reference_key_name": "sku", "output": {"description": "ref['description']"}},
                {"table": "regions", "key_field": "postcode", "reference_key_name": "code", "output": {"region": "ref['region']"}},
            ],
        },
    )

    result = _cli("run", "-s", str(settings), "--execute")

    assert result.exit_code == 2, result.output
    quarantined = [json.loads(line) for line in (tmp_path / "q.jsonl").read_text().splitlines()]
    assert quarantined == [original]
    output_path = tmp_path / "out.jsonl"
    assert not output_path.exists() or output_path.read_text() == ""
    assert _terminal_outcomes(tmp_path) == {"failure/on_error_routed": 1}
    reason = _routed_reasons(tmp_path)[0]
    assert reason["reason"] == "reference_miss"
    assert reason["lookup_index"] == 1
    assert reason["table"] == "regions"
    assert "unknown" not in json.dumps(reason)


def test_multi_lookup_defaults_emit_complete_row_with_persisted_attribution(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        [{"product": "hat", "postcode": "unknown"}],
        plugin="reference_join",
        options={
            "tables": {
                "products": {"reference_content": "sku,description\nhat,Fine hat\n", "reference_format": "csv"},
                "regions": {"reference_content": "code,region\n2000,Sydney\n", "reference_format": "csv"},
            },
            "lookups": [
                {"table": "products", "key_field": "product", "reference_key_name": "sku", "output": {"description": "ref['description']"}},
                {
                    "table": "regions",
                    "key_field": "postcode",
                    "reference_key_name": "code",
                    "output": {"region": "ref['region']"},
                    "on_miss": "default",
                    "default_values": {"region": "Unmapped"},
                },
            ],
        },
    )

    result = _cli("run", "-s", str(settings), "--execute")

    assert result.exit_code == 0, result.output
    assert [json.loads(line) for line in (tmp_path / "out.jsonl").read_text().splitlines()] == [
        {"product": "hat", "postcode": "unknown", "description": "Fine hat", "region": "Unmapped"}
    ]
    reasons = [
        json.loads(text)
        for (text,) in _query(tmp_path, "select success_reason_json from node_states where success_reason_json is not null")
    ]
    enriched = next(reason for reason in reasons if reason["action"] == "enriched")
    lookups = enriched["metadata"]["lookups"]
    assert [(lookup["table"], lookup["matched"], lookup["unresolved_fields"]) for lookup in lookups] == [
        ("products", True, []),
        ("regions", False, ["region"]),
    ]
