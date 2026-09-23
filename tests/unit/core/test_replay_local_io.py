"""Replay local IO uses retained, bound evidence before current files or blobs."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from elspeth.config_loading import load_settings
from elspeth.contracts.enums import RunMode
from elspeth.contracts.payload_store import IntegrityError
from elspeth.core.config import resolve_config
from elspeth.core.replay_payload_store import SourceBoundPayloadStore
from elspeth.core.template_materialization import TemplateFileError, TemplateOptionMaterializer
from elspeth.plugins.transforms.blob_csv_expand import BlobCSVExpand
from elspeth.plugins.transforms.blob_json_expand import BlobJSONExpand
from elspeth.plugins.transforms.blob_text_expand import BlobTextExpand
from elspeth.testing import make_pipeline_row
from tests.fixtures.factories import make_context


class _MemoryStore:
    def __init__(self, blobs: dict[str, bytes] | None = None) -> None:
        self.blobs = {} if blobs is None else dict(blobs)
        self.reads: list[str] = []

    def retrieve(self, content_hash: str) -> bytes:
        self.reads.append(content_hash)
        return self.blobs[content_hash]

    def store(self, content: bytes) -> str:
        content_hash = hashlib.sha256(content).hexdigest()
        self.blobs[content_hash] = content
        return content_hash

    def exists(self, content_hash: str) -> bool:
        return content_hash in self.blobs

    def delete(self, content_hash: str) -> bool:
        return self.blobs.pop(content_hash, None) is not None


def test_replay_file_option_uses_archived_content_without_live_file(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.yaml"
    options = {"reference_file": "missing.csv"}
    archived = {"reference_source": "missing.csv", "reference_content": "sku,value\nA,old\n"}
    materializer = TemplateOptionMaterializer(settings_path)

    assert materializer.materialize_options(options, run_mode=RunMode.REPLAY, source_options=archived) == {
        "reference_source": "missing.csv",
        "reference_content": "sku,value\nA,old\n",
    }
    with pytest.raises(TemplateFileError, match="no matching materialized content"):
        materializer.materialize_options(options, run_mode=RunMode.REPLAY, source_options=None)


def test_verify_file_option_detects_changed_content(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.yaml"
    reference = tmp_path / "reference.csv"
    reference.write_text("sku,value\nA,current\n", encoding="utf-8")
    archived = {"reference_source": "reference.csv", "reference_content": "sku,value\nA,old\n"}
    materializer = TemplateOptionMaterializer(settings_path)

    with pytest.raises(TemplateFileError, match="differs from source-run"):
        materializer.materialize_options({"reference_file": "reference.csv"}, run_mode=RunMode.VERIFY, source_options=archived)
    reference.write_text(archived["reference_content"], encoding="utf-8")
    assert (
        materializer.materialize_options({"reference_file": "reference.csv"}, run_mode=RunMode.VERIFY, source_options=archived)[
            "reference_content"
        ]
        == archived["reference_content"]
    )


def test_load_settings_replay_binds_reference_file_before_read(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    example = repo_root / "examples/reference_join"
    raw = yaml.safe_load((example / "settings.yaml").read_text(encoding="utf-8"))
    settings_path = tmp_path / "settings.yaml"
    reference_path = tmp_path / "products.csv"
    reference_path.write_bytes((example / "products.csv").read_bytes())
    settings_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    source_settings = resolve_config(load_settings(settings_path))
    reference_path.unlink()
    raw["run_mode"] = "replay"
    raw["replay_from"] = "source-run"
    settings_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(TemplateFileError, match="no matching materialized content"):
        load_settings(settings_path)
    replay = load_settings(settings_path, source_settings=source_settings)
    assert replay.transforms[0].options["reference_content"] == source_settings["transforms"][0]["options"]["reference_content"]


def test_replay_payload_reads_only_source_run_evidence_and_restores_archived_output() -> None:
    input_bytes = b"source row blob"
    output_bytes = b"archived rendered page"
    input_hash = hashlib.sha256(input_bytes).hexdigest()
    output_hash = hashlib.sha256(output_bytes).hexdigest()
    source = _MemoryStore({input_hash: input_bytes, output_hash: output_bytes})
    current = _MemoryStore()
    store = SourceBoundPayloadStore(
        mode=RunMode.REPLAY,
        source_store=source,
        current_store=current,
        source_refs={input_hash},
        output_refs={output_hash},
    )

    assert store.retrieve(input_hash) == input_bytes
    assert current.reads == []
    with pytest.raises(IntegrityError, match="absent from source-run"):
        store.retrieve(output_hash)
    with pytest.raises(IntegrityError, match="newly computed"):
        store.store(b"new live page")
    assert store.restore_output(output_hash) == output_hash
    assert current.blobs == {output_hash: output_bytes}


def test_payload_corruption_and_verify_drift_fail_closed() -> None:
    content = b"original"
    content_hash = hashlib.sha256(content).hexdigest()
    source = _MemoryStore({content_hash: content})
    current = _MemoryStore({content_hash: b"changed"})
    store = SourceBoundPayloadStore(mode=RunMode.VERIFY, source_store=source, current_store=current, source_refs={content_hash})

    with pytest.raises(IntegrityError, match="hash"):
        store.retrieve(content_hash)
    source.blobs[content_hash] = b"corrupt source"
    with pytest.raises(IntegrityError, match="source-run hash"):
        store.retrieve(content_hash)


@pytest.mark.parametrize(
    ("transform_class", "options", "body", "row_fields"),
    [
        (BlobCSVExpand, {}, b"id,name\n1,Alice\n", {}),
        (
            BlobJSONExpand,
            {"fields": ["document_id", "title", "sections"], "data_key": "documents"},
            b'{"documents":[{"document_id":"d1","title":"One","sections":[]}]}',
            {"blob_content_type": "application/json"},
        ),
        (BlobTextExpand, {}, b"alpha\nbeta\n", {}),
    ],
)
def test_blob_expanders_read_audited_source_bytes(
    transform_class: type,
    options: dict[str, object],
    body: bytes,
    row_fields: dict[str, object],
) -> None:
    content_hash = hashlib.sha256(body).hexdigest()
    source = _MemoryStore({content_hash: body})
    current = _MemoryStore()
    transform = transform_class({"schema": {"mode": "observed"}, "blob_ref_field": "blob_ref", **options})
    transform._payload_store = SourceBoundPayloadStore(
        mode=RunMode.REPLAY,
        source_store=source,
        current_store=current,
        source_refs={content_hash},
    )

    result = transform.process(
        make_pipeline_row({"url": "https://example.test/blob", "blob_ref": content_hash, **row_fields}), make_context()
    )

    assert result.status == "success"
    assert result.rows is not None and len(result.rows) >= 1
    assert source.reads == [content_hash]
    assert current.reads == []
