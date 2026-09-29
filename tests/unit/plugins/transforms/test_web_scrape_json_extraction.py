"""Contract tests for bounded JSON response extraction."""

import pytest
from pydantic import ValidationError

from elspeth.core.canonical import stable_hash
from elspeth.plugins.infrastructure.clients.fingerprinting import fingerprint_url
from elspeth.plugins.transforms.web_scrape_json_extraction import (
    JSONRecordColumn,
    JSONRecordsConfig,
    extract_json_records_with_provenance,
)


def _config(**overrides: object) -> JSONRecordsConfig:
    values = {
        "field": "candidates",
        "records_path": ["results"],
        "columns": [
            {"field": "name", "path": ["name"], "required": True},
            {"field": "ids", "path": ["ids"], "multiple": "all"},
        ],
    }
    values.update(overrides)
    return JSONRecordsConfig.model_validate(values)


def test_extract_json_records_retains_values_and_sanitized_field_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "json-extraction-test-key")
    safe_url = fingerprint_url("https://user:secret@example.gov.au/search?token=secret&q=acme#fragment")
    result = extract_json_records_with_provenance(
        '{"results":[{"name":"  Acme  ","ids":[1,2]}]}',
        _config(),
        source_url=safe_url,
    )

    assert result.records == [{"name": "Acme", "ids": ["1", "2"]}]
    assert result.content_trust == "untrusted"
    assert result.provenance[0]["name"].source_url == safe_url
    assert "secret" not in safe_url
    assert "q=acme" in safe_url
    assert result.provenance[0]["name"].records_path == ("results",)
    assert result.provenance[0]["name"].value_path == ("name",)
    assert result.provenance[0]["name"].record_index == 0
    assert result.provenance[0]["ids"].selected_count == 2
    assert result.provenance[0]["ids"].match_policy == "all"


def test_typed_path_selects_array_index_and_dictionary_numeric_key_distinctly() -> None:
    config = _config(
        records_path=["results", 0, "records"],
        columns=[
            {"field": "indexed", "path": ["values", 0]},
            {"field": "keyed", "path": ["0"]},
        ],
    )
    result = extract_json_records_with_provenance(
        '{"results":[{"records":[{"values":["first"],"0":"key"}]}]}',
        config,
        source_url="https://example.gov.au/api",
    )
    assert result.records == [{"indexed": "first", "keyed": "key"}]


def test_missing_optional_and_all_fields_have_explicit_shapes() -> None:
    config = _config(columns=[{"field": "one", "path": ["absent"]}, {"field": "many", "path": ["absent"], "multiple": "all"}])
    result = extract_json_records_with_provenance('{"results":[{}]}', config, source_url="https://example.gov.au/")
    assert result.records == [{"one": None, "many": []}]
    assert result.provenance[0]["one"].selected_count == 0
    assert result.provenance[0]["many"].selected_count == 0


def test_required_missing_or_blank_fails_closed() -> None:
    config = _config(columns=[{"field": "name", "path": ["name"], "required": True}])
    for document in ('{"results":[{}]}', '{"results":[{"name":"  "}]}'):
        with pytest.raises(ValueError, match="required JSON record column"):
            extract_json_records_with_provenance(document, config, source_url="https://example.gov.au/")


def test_one_rejects_ambiguous_multiple_values() -> None:
    config = _config(columns=[{"field": "name", "path": ["names"], "multiple": "one"}])
    with pytest.raises(ValueError, match="multiple values"):
        extract_json_records_with_provenance('{"results":[{"names":["A","B"]}]}', config, source_url="https://example.gov.au/")


def test_first_takes_one_value_without_truncating_all_mode() -> None:
    config = _config(columns=[{"field": "name", "path": ["names"], "multiple": "first"}])
    result = extract_json_records_with_provenance('{"results":[{"names":["A","B"]}]}', config, source_url="https://example.gov.au/")
    assert result.records == [{"name": "A"}]
    assert result.provenance[0]["name"].selected_count == 1


@pytest.mark.parametrize(
    ("overrides", "document", "error"),
    [
        ({"max_records": 1}, '{"results":[{},{}]}', "max_records"),
        ({"columns": [{"field": "ids", "path": ["ids"], "multiple": "all", "max_values": 1}]}, '{"results":[{"ids":[1,2]}]}', "max_values"),
        ({"max_total_values": 1}, '{"results":[{"name":"A","ids":[1]}]}', "max_total_values"),
        ({"max_values_per_record": 1}, '{"results":[{"name":"A","ids":[1]}]}', "max_values_per_record"),
        ({"max_value_chars": 2}, '{"results":[{"name":"Long"}]}', "max_value_chars"),
        ({"max_output_chars": 10}, '{"results":[{"name":"A"}]}', "max_output_chars"),
        ({"max_json_chars": 5}, '{"results":[]}', "max_json_chars"),
    ],
)
def test_limits_reject_instead_of_truncate(overrides: dict[str, object], document: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        extract_json_records_with_provenance(document, _config(**overrides), source_url="https://example.gov.au/")


@pytest.mark.parametrize(
    "document", ['{"results":{}}', '{"results":[{"name":{"nested":"value"}}]}', '{"results":[{"name":1,"name":2}]}', '{"results":[NaN]}']
)
def test_untrusted_json_shape_and_ambiguity_fail_closed(document: str) -> None:
    with pytest.raises(ValueError):
        extract_json_records_with_provenance(document, _config(), source_url="https://example.gov.au/")


def test_invalid_config_rejected_before_extraction() -> None:
    with pytest.raises(ValidationError):
        JSONRecordColumn(field="x", path=["x", -1])
    with pytest.raises(ValidationError):
        JSONRecordColumn(field="x", path=[True])
    with pytest.raises(ValidationError):
        _config(columns=[{"field": "x", "path": ["x"]}, {"field": "x", "path": ["y"]}])
    with pytest.raises(ValidationError):
        _config(provenance_field="candidates")


def test_root_array_and_json_scalar_normalization() -> None:
    config = _config(records_path=[], columns=[{"field": "v", "path": ["value"], "multiple": "all"}])
    result = extract_json_records_with_provenance(
        '[{"value":[true,false,1.25,"A  B"]}]', config, source_url="https://example.gov.au/api?q=acme"
    )
    assert result.records == [{"v": ["true", "false", "1.25", "A B"]}]
    assert result.provenance[0]["v"].source_url.endswith("?q=acme")


def test_url_with_unfingerprinted_sensitive_query_is_rejected() -> None:
    with pytest.raises(ValueError, match="unfingerprinted"):
        extract_json_records_with_provenance('{"results":[]}', _config(), source_url="https://example.gov.au/api?token=raw")


def test_source_url_must_be_http_and_bounded() -> None:
    for url in (
        "file:///etc/passwd",
        "https://user:secret@example.gov.au/",
        "https://example.gov.au/#secret",
        "https://example.gov.au/\r\nHeader: secret",
        "https://example.gov.au/" + "x" * 2048,
    ):
        with pytest.raises(ValueError, match="source_url"):
            extract_json_records_with_provenance('{"results":[]}', _config(), source_url=url)


@pytest.mark.parametrize("escaped", [r"\ud800", r"\udfff", r"prefix\ud800suffix"])
def test_json_selected_scalar_rejects_unpaired_surrogate(escaped: str) -> None:
    with pytest.raises(ValueError, match="invalid Unicode scalar"):
        extract_json_records_with_provenance('{"results":[{"name":"' + escaped + '"}]}', _config(), source_url="https://example.gov.au/")


@pytest.mark.parametrize(("encoded", "expected"), [(r"\ud83d\ude00", "😀"), ("café 東京", "café 東京")])
def test_json_selected_unicode_remains_canonicalizable(encoded: str, expected: str) -> None:
    result = extract_json_records_with_provenance(
        '{"results":[{"name":"' + encoded + '"}]}', _config(), source_url="https://example.gov.au/"
    )
    assert result.records == [{"name": expected, "ids": []}]
    assert stable_hash(result.records) == stable_hash([{"name": expected, "ids": []}])
