"""Tests for the blob_rows source plugin (elspeth-0c6a343921).

blob_rows turns managed-blob references into custody rows: exactly one row
per configured entry, five fixed fields, authoring order preserved, no
content retrieval and no bytes/base64 anywhere in row data.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.contracts.plugin_context import PluginContext
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from tests.fixtures.factories import make_source_context

DYNAMIC_SCHEMA = {"mode": "observed"}


def _entry(index: int = 1, **overrides: Any) -> dict[str, Any]:
    entry = {
        "blob_id": f"{index:08d}-1111-1111-1111-111111111111",
        "payload_ref": f"{index:x}".rjust(64, "a"),
        "filename": f"page-{index}.png",
        "mime_type": "image/png",
        "size_bytes": 100 + index,
    }
    entry.update(overrides)
    return entry


def _config(entries: list[dict[str, Any]] | None = None, **overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "blobs": entries if entries is not None else [_entry(1), _entry(2)],
        "schema": DYNAMIC_SCHEMA,
        "on_validation_failure": "discard",
    }
    config.update(overrides)
    return config


class _FakePayloadStore:
    """Existence-only payload store fake; retrieval is deliberately fatal."""

    def __init__(self, present: set[str]) -> None:
        self._present = present
        self.exists_calls: list[str] = []
        self.retrieve_calls: list[str] = []

    def store(self, content: bytes) -> str:
        raise AssertionError("blob_rows must never store content")

    def retrieve(self, content_hash: str) -> bytes:
        self.retrieve_calls.append(content_hash)
        raise AssertionError("blob_rows must never retrieve content")

    def exists(self, content_hash: str) -> bool:
        self.exists_calls.append(content_hash)
        return content_hash in self._present


class _FakeLifecycleContext:
    def __init__(self, payload_store: Any) -> None:
        self.payload_store = payload_store


def _make_source(config: dict[str, Any], *, present: set[str] | None = None):
    from elspeth.plugins.sources.blob_rows import BlobRowsSource

    source = BlobRowsSource(config)
    refs = {entry["payload_ref"] for entry in config["blobs"]}
    store = _FakePayloadStore(refs if present is None else present)
    source.on_start(_FakeLifecycleContext(store))
    return source, store


@pytest.fixture
def ctx() -> PluginContext:
    return make_source_context(plugin_name="blob_rows")


class TestBlobRowsConfig:
    def test_metadata(self) -> None:
        from elspeth.plugins.sources.blob_rows import BlobRowsSource

        assert BlobRowsSource.name == "blob_rows"
        assert BlobRowsSource.plugin_version == "1.0.0"
        # creates_tokens is deliberately NOT declared: it is a transform-only
        # contract attribute; sources tokenize implicitly per SourceRow.
        assert "creates_tokens" not in vars(BlobRowsSource)
        assert BlobRowsSource.source_file_hash is not None
        assert BlobRowsSource.source_file_hash.startswith("sha256:")
        for tag in ("blob", "payload", "binary", "source"):
            assert tag in BlobRowsSource.capability_tags

    def test_empty_blobs_rejected(self) -> None:
        with pytest.raises(PluginConfigError, match="at least 1"):
            _make_source(_config([]))

    def test_over_bound_rejected(self) -> None:
        entries = [_entry(i) for i in range(1, 1002)]
        with pytest.raises(PluginConfigError, match="at most 1000"):
            _make_source(_config(entries))

    def test_duplicate_blob_id_rejected(self) -> None:
        entries = [_entry(1), _entry(2, blob_id=_entry(1)["blob_id"])]
        with pytest.raises(PluginConfigError, match="unique blob_id"):
            _make_source(_config(entries))

    def test_duplicate_payload_ref_rejected(self) -> None:
        entries = [_entry(1), _entry(2, payload_ref=_entry(1)["payload_ref"])]
        with pytest.raises(PluginConfigError, match="unique payload_ref"):
            _make_source(_config(entries))

    @pytest.mark.parametrize(
        ("field", "value", "match"),
        [
            ("blob_id", "not-a-uuid", "UUID"),
            ("payload_ref", "A" * 64, "lowercase hex"),
            ("payload_ref", "a" * 63, "lowercase hex"),
            ("mime_type", "image/gif", "mime_type must be one of"),
            ("filename", "", "at least 1 character"),
            ("size_bytes", -1, "greater than or equal"),
        ],
    )
    def test_malformed_entry_rejected(self, field: str, value: Any, match: str) -> None:
        with pytest.raises(PluginConfigError, match=match):
            _make_source(_config([_entry(1, **{field: value})]))

    def test_unknown_entry_field_rejected(self) -> None:
        with pytest.raises(PluginConfigError):
            _make_source(_config([_entry(1, content="sneaky bytes")]))


class TestBlobRowsLoad:
    def test_emits_one_five_field_row_per_entry_in_order(self, ctx: PluginContext) -> None:
        entries = [_entry(3), _entry(1), _entry(2)]
        source, store = _make_source(_config(entries))

        rows = list(source.load(ctx))

        assert len(rows) == 3
        for index, (row, entry) in enumerate(zip(rows, entries, strict=True)):
            assert not row.is_quarantined
            assert row.source_row_index == index
            assert row.row == {
                "blob_id": entry["blob_id"],
                "blob_ref": entry["payload_ref"],
                "blob_filename": entry["filename"],
                "blob_mime_type": entry["mime_type"],
                "blob_size_bytes": entry["size_bytes"],
            }
        # Existence was validated, content never touched.
        assert sorted(store.exists_calls) == sorted(e["payload_ref"] for e in entries)
        assert store.retrieve_calls == []

    def test_missing_payload_ref_fails_before_any_row(self, ctx: PluginContext) -> None:
        entries = [_entry(1), _entry(2)]
        source, _ = _make_source(_config(entries), present={entries[0]["payload_ref"]})

        with pytest.raises(FileNotFoundError, match=entries[1]["payload_ref"]):
            list(source.load(ctx))

    def test_missing_payload_store_is_an_infrastructure_failure(self, ctx: PluginContext) -> None:
        from elspeth.plugins.sources.blob_rows import BlobRowsSource

        source = BlobRowsSource(_config())
        source.on_start(_FakeLifecycleContext(None))

        with pytest.raises(RuntimeError, match="payload store"):
            list(source.load(ctx))

    def test_rows_never_contain_bytes_or_base64(self, ctx: PluginContext) -> None:
        source, _ = _make_source(_config([_entry(1)]))
        rows = list(source.load(ctx))
        assert set(rows[0].row.keys()) == {"blob_id", "blob_ref", "blob_filename", "blob_mime_type", "blob_size_bytes"}
        assert all(not isinstance(value, (bytes, bytearray)) for value in rows[0].row.values())


class TestBlobRowsDiscovery:
    def test_manager_discovers_blob_rows(self) -> None:
        from elspeth.plugins.infrastructure.manager import PluginManager

        manager = PluginManager()
        manager.register_builtin_plugins()
        source = manager.get_source_by_name("blob_rows")
        assert source is not None
        assert source.name == "blob_rows"
