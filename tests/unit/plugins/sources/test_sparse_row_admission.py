"""Rejected sparse records cannot establish accepted schema or naming metadata."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import elspeth.contracts.contract_builder as contract_builder
from elspeth.contracts.plugin_protocols import SourceProtocol
from elspeth.contracts.results import SourceRow
from elspeth.plugins.sources.json_source import JSONSource
from tests.unit.plugins.sources import test_aws_s3_source as s3
from tests.unit.plugins.sources import test_azure_blob_source as azure
from tests.unit.plugins.sources import test_dataverse_source as dataverse

_LAYOUTS = (
    ("json", "json"),
    ("json", "jsonl"),
    ("s3", "json"),
    ("s3", "jsonl"),
    ("azure", "json"),
    ("azure", "jsonl"),
    ("dataverse", "fetchxml"),
    ("dataverse", "odata"),
)


@pytest.fixture(params=_LAYOUTS, ids=["-".join(layout) for layout in _LAYOUTS])
def layout(request: pytest.FixtureRequest) -> tuple[str, str]:
    return request.param


@contextmanager
def _loaded_source(
    layout: tuple[str, str],
    root: Path,
    rows: list[dict[str, Any]],
    *,
    mode: str,
    policy: str,
    fields: list[str] | None = None,
    field_mapping: dict[str, str] | None = None,
) -> Iterator[tuple[SourceProtocol, Iterator[SourceRow]]]:
    adapter, format = layout
    schema: dict[str, Any] = {"mode": mode}
    if mode in {"flexible", "fixed"}:
        schema["fields"] = fields if fields is not None else ["known: int"]
    options = {"schema": schema, "on_validation_failure": policy}
    if field_mapping is not None:
        options["field_mapping"] = field_mapping
    data = (json.dumps(rows) if format == "json" else "\n".join(json.dumps(row) for row in rows) + "\n").encode()
    with ExitStack() as stack:
        if adapter == "json":
            path = root / f"rows.{format}"
            path.write_bytes(data)
            source = JSONSource({"path": str(path), "format": format, **options})
            ctx = azure._SourceContextFake()
        elif adapter == "s3":
            source, _, ctx = s3._source_for(data, format=format, **options)
        elif adapter == "azure":
            source = azure._make_source(azure._base_config(format=format, **options))
            stack.enter_context(patch(azure.PATCH_AUTH, return_value=azure._fake_blob_service(data)))
            ctx = azure._SourceContextFake()
        elif adapter == "dataverse":
            config = dataverse._fetchxml_config(**options) if format == "fetchxml" else dataverse._base_config(**options)
            # Exercise accepted and rejected records across page boundaries.
            source = dataverse._make_source_for_load([dataverse._make_page([row]) for row in rows], config)
            ctx = dataverse._mock_source_context()
        else:
            raise AssertionError(adapter)
        iterator = source.load(ctx)
        try:
            yield source, iterator
        finally:
            iterator.close()
            source.close()


@pytest.mark.parametrize("mode", ("observed", "flexible"))
@pytest.mark.parametrize("policy", ("quarantine", "discard"))
@pytest.mark.parametrize("scenario", ("type", "alias", "budget"))
def test_rejected_sparse_row_does_not_acquire_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layout: tuple[str, str],
    mode: str,
    policy: str,
    scenario: str,
) -> None:
    # First accepted row occupies two slots. An invalid row must neither
    # consume the third slot nor reserve a raw alias or inferred type.
    monkeypatch.setattr(contract_builder, "_MAX_INFERRED_CONTRACT_FIELDS", 3)
    bad_key, good_key, normalized = {
        "type": ("Late Name", "Late Name", "late_name"),
        "alias": ("Full Name", "full_name", "full_name"),
        "budget": ("Poison", "New Field", "new_field"),
    }[scenario]
    rows = [
        {"known": 0, "id": 1},
        {"known": 0, "id": "bad", bad_key: 123},
        {"known": 0, "id": 2, good_key: "Ada"},
    ]
    with _loaded_source(layout, tmp_path, rows, mode=mode, policy=policy) as (source, iterator):
        first = next(iterator)
        assert not first.is_quarantined
        first_contract = source.require_schema_contract()
        first_resolution = source.get_field_resolution()
        assert first_resolution is not None
        assert dict(first_resolution[0]) == {"known": "known", "id": "id"}
        if policy == "quarantine":
            rejected = next(iterator)
            assert rejected.is_quarantined
            assert rejected.source_row_index == 1
            assert source.require_schema_contract() == first_contract
            assert source.get_field_resolution() == first_resolution
            assert dict(first_resolution[0]) == {"known": "known", "id": "id"}
        accepted = next(iterator)
        assert not accepted.is_quarantined
        assert accepted.source_row_index == 2
        assert accepted.row == {"known": 0, "id": 2, normalized: "Ada"}
        assert list(iterator) == []
        contract = source.require_schema_contract()
        assert contract.locked
        assert {field.normalized_name for field in contract.fields} == {"known", "id", normalized}
        field = contract.get_field(normalized)
        assert field.original_name == good_key
        assert field.python_type is str
        resolution = source.get_field_resolution()
        assert resolution is not None
        assert dict(resolution[0]) == {"known": "known", "id": "id", good_key: normalized}
        assert accepted.contract == contract
        assert {field.normalized_name for field in first_contract.fields} == {"known", "id"}


@pytest.mark.parametrize("mode", ("observed", "flexible"))
@pytest.mark.parametrize("policy", ("quarantine", "discard"))
def test_accepted_raw_alias_retains_authority(tmp_path: Path, layout: tuple[str, str], mode: str, policy: str) -> None:
    rows = [
        {"known": 0, "id": 1, "Full Name": "Ada"},
        {"known": 0, "id": 2, "full_name": "Grace"},
    ]
    with _loaded_source(layout, tmp_path, rows, mode=mode, policy=policy) as (source, iterator):
        first = next(iterator)
        assert not first.is_quarantined
        contract = source.require_schema_contract()
        emitted = list(iterator)
        assert [row.is_quarantined for row in emitted] == ([True] if policy == "quarantine" else [])
        assert source.require_schema_contract() == contract
        resolution = source.get_field_resolution()
        assert resolution is not None
        assert dict(resolution[0]) == {"known": "known", "id": "id", "Full Name": "full_name"}
        assert contract.get_field("full_name").original_name == "Full Name"


@pytest.mark.parametrize("mode", ("observed", "flexible"))
@pytest.mark.parametrize("policy", ("quarantine", "discard"))
def test_accepted_sparse_field_acquires_authority(tmp_path: Path, layout: tuple[str, str], mode: str, policy: str) -> None:
    rows = [{"known": 0, "id": 1}, {"known": 0, "id": 2, "Full Name": "Ada"}]
    with _loaded_source(layout, tmp_path, rows, mode=mode, policy=policy) as (source, iterator):
        emitted = list(iterator)
        assert [row.is_quarantined for row in emitted] == [False, False]
        assert emitted[1].row == {"known": 0, "id": 2, "full_name": "Ada"}
        contract = source.require_schema_contract()
        field = contract.get_field("full_name")
        assert field.original_name == "Full Name"
        assert field.python_type is str
        resolution = source.get_field_resolution()
        assert resolution is not None
        assert dict(resolution[0]) == {"known": "known", "id": "id", "Full Name": "full_name"}


@pytest.mark.parametrize("policy", ("quarantine", "discard"))
def test_first_valid_declared_metadata_uses_accepted_resolution(tmp_path: Path, layout: tuple[str, str], policy: str) -> None:
    rows = [{"known": "bad", "Full Name": "Ada"}, {"known": 0, "full_name": "Ada"}]
    with _loaded_source(layout, tmp_path, rows, mode="flexible", policy=policy, fields=["known: int", "full_name: str"]) as (
        source,
        iterator,
    ):
        emitted = list(iterator)
        assert [row.is_quarantined for row in emitted] == ([True, False] if policy == "quarantine" else [False])
        accepted = emitted[-1]
        assert accepted.source_row_index == 1
        assert accepted.row == {"known": 0, "full_name": "Ada"}
        resolution = source.get_field_resolution()
        assert resolution is not None
        assert dict(resolution[0]) == {"known": "known", "full_name": "full_name"}
        contract = source.require_schema_contract()
        assert accepted.contract == contract
        assert contract.locked
        field = contract.get_field("full_name")
        assert field.original_name == "full_name"
        assert field.python_type is str


@pytest.mark.parametrize("policy", ("quarantine", "discard"))
def test_first_inference_cap_rejection_does_not_contaminate_accepted_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: tuple[str, str], policy: str
) -> None:
    monkeypatch.setattr(contract_builder, "_MAX_INFERRED_CONTRACT_FIELDS", 2)
    rows = [{"known": 0, "Full Name": "Ada", "Extra": "too wide"}, {"known": 1, "full_name": "Grace"}]
    with _loaded_source(layout, tmp_path, rows, mode="flexible", policy=policy, fields=["known: int", "full_name: str"]) as (
        source,
        iterator,
    ):
        if policy == "quarantine":
            rejected = next(iterator)
            assert rejected.is_quarantined
            assert rejected.source_row_index == 0
            assert source.get_schema_contract() is None
            assert source.get_field_resolution() is None
        accepted = next(iterator)
        assert not accepted.is_quarantined
        assert accepted.source_row_index == 1
        assert accepted.row == {"known": 1, "full_name": "Grace"}
        assert list(iterator) == []
        assert accepted.contract == source.require_schema_contract()
        assert source.require_schema_contract().get_field("full_name").original_name == "full_name"
        resolution = source.get_field_resolution()
        assert resolution is not None
        assert dict(resolution[0]) == {"known": "known", "full_name": "full_name"}


@pytest.mark.parametrize("mode", ("observed", "flexible"))
def test_cap_rejection_leaves_no_partial_sparse_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: tuple[str, str], mode: str
) -> None:
    monkeypatch.setattr(contract_builder, "_MAX_INFERRED_CONTRACT_FIELDS", 3)
    rows = [
        {"known": 0, "id": 1},
        {"known": 0, "id": 2, "Extra A": "a", "Extra B": "b"},
        {"known": 0, "id": 3, "Good": "ok"},
    ]
    with _loaded_source(layout, tmp_path, rows, mode=mode, policy="quarantine") as (source, iterator):
        first = next(iterator)
        resolution = source.get_field_resolution()
        rejected = next(iterator)
        assert rejected.is_quarantined
        assert source.require_schema_contract() == first.contract
        assert source.get_field_resolution() == resolution
        accepted = next(iterator)
        assert accepted.row == {"known": 0, "id": 3, "good": "ok"}
        assert not accepted.is_quarantined
        assert list(iterator) == []
        assert {field.normalized_name for field in source.require_schema_contract().fields} == {"known", "id", "good"}


def test_fixed_contract_still_refuses_new_keys(tmp_path: Path, layout: tuple[str, str]) -> None:
    rows = [{"known": 0}, {"known": 1, "Extra": "bad"}, {"known": 2}]
    with _loaded_source(layout, tmp_path, rows, mode="fixed", policy="quarantine") as (source, iterator):
        emitted = list(iterator)
        assert [row.is_quarantined for row in emitted] == [False, True, False]
        assert emitted[-1].row == {"known": 2}
        assert {field.normalized_name for field in source.require_schema_contract().fields} == {"known"}
        resolution = source.get_field_resolution()
        assert resolution is not None
        assert dict(resolution[0]) == {"known": "known"}


@pytest.mark.parametrize("mode", ("observed", "flexible"))
def test_optional_null_and_sparse_field_mapping_remain_supported(tmp_path: Path, layout: tuple[str, str], mode: str) -> None:
    rows = [{"known": 0, "id": 1}, {"known": 0, "id": None, "Display Name": "Ada"}]
    with _loaded_source(layout, tmp_path, rows, mode=mode, policy="quarantine", field_mapping={"display_name": "label"}) as (
        source,
        iterator,
    ):
        emitted = list(iterator)
        assert [row.is_quarantined for row in emitted] == [False, False]
        assert emitted[-1].row == {"known": 0, "id": None, "label": "Ada"}
        field = source.require_schema_contract().get_field("label")
        assert (field.original_name, field.python_type) == ("Display Name", str)


@pytest.mark.parametrize("format", ["json", "jsonl"])
@pytest.mark.parametrize("mode", ["observed", "flexible"])
def test_repeated_load_keeps_previously_accepted_inferred_types(tmp_path: Path, format: str, mode: str) -> None:
    path = tmp_path / f"rows.{format}"

    def write(row: dict[str, Any]) -> None:
        path.write_text(json.dumps([row]) if format == "json" else json.dumps(row) + "\n")

    write({"known": 0, "id": 1})
    schema: dict[str, Any] = {"mode": mode}
    if mode == "flexible":
        schema["fields"] = ["known: int"]
    source = JSONSource({"path": str(path), "format": format, "schema": schema, "on_validation_failure": "quarantine"})
    try:
        first = list(source.load(azure._SourceContextFake()))
        assert len(first) == 1 and not first[0].is_quarantined
        contract = source.require_schema_contract()
        resolution = source.get_field_resolution()
        write({"known": 0, "id": "bad"})
        second = list(source.load(azure._SourceContextFake()))
        assert len(second) == 1 and second[0].is_quarantined
        assert source.require_schema_contract() == contract
        assert source.get_field_resolution() == resolution
    finally:
        source.close()
