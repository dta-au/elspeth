"""Compartment marking format and exact text ingress evidence."""

from __future__ import annotations

import hashlib

import pytest
import yaml

from elspeth.web.compartments import compartment_ingress_record, compartment_marking_header, is_compartment_id


@pytest.mark.parametrize("value", ["a", "0", "alpha", "compartment-a", "a" * 63])
def test_valid_compartment_id(value: str) -> None:
    assert is_compartment_id(value)


@pytest.mark.parametrize("value", ["", "Alpha", "-alpha", "alpha_beta", "alpha beta", "alpha\n", "é", "a" * 64])
def test_invalid_compartment_id(value: str) -> None:
    assert not is_compartment_id(value)


def test_marking_line_round_trips_as_a_yaml_comment() -> None:
    body = "sources:\n  source:\n    plugin: csv\n"
    line = compartment_marking_header("alpha")
    assert line == "# compartment_id: alpha\n"
    assert compartment_marking_header(None) == ""
    assert yaml.safe_load(line + body) == yaml.safe_load(body)
    assert compartment_ingress_record(line + body, own_compartment_id="beta")["foreign_compartment_ids"] == ["alpha"]
    with pytest.raises(ValueError, match="compartment_id"):
        compartment_marking_header("Alpha")


def test_ingress_uses_exact_text_and_whole_marking_lines() -> None:
    text = (
        "# compartment_id: other\r\n"
        "  compartment_id: third\n"
        "# compartment_id: home\n"
        "# compartment_id: other\n"
        "note: compartment_id: ignored\n"
        "# compartment_id: Other\n"
        "# compartment_id: other trailing\n"
    )
    assert compartment_ingress_record(text, own_compartment_id="home") == {
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "foreign_compartment_ids": ["other", "third"],
    }
    assert compartment_ingress_record(text, own_compartment_id="other")["foreign_compartment_ids"] == ["home", "third"]
