"""Protected live-Chroma PB-09 lifecycle proof for sink:chroma_sink.

One node per production connection-mode discriminator variant (``persistent``,
``client``) from ``tests/golden/state_engine/plugin_lifecycle_matrix.json``.
Each node writes a uniquely-prefixed disposable document through the FULL
production boundary (settings YAML -> ``instantiate_plugins_from_config`` ->
``ExecutionGraph`` -> runtime preflight -> public ``Orchestrator.run`` with a
real Landscape database), exercising the recoverable member sink-effect
protocol end to end, then externally observes the written document with an
independent ``chromadb`` read.

``ELSPETH_TEST_CHROMA_TENANT`` / ``ELSPETH_TEST_CHROMA_DATABASE`` are lane
resources but stay unread here: ``ChromaSinkConfig`` exposes no tenant or
database knobs, so the sink always targets the server defaults.

Variant facts pinned by ``ChromaConnectionConfig``:

- ``persistent`` requires ``persist_directory`` (local disk; this test uses a
  per-test temporary directory, so cleanup is automatic);
- ``client`` requires ``host`` and forbids ``persist_directory``; ``ssl=false``
  is only permitted for loopback hosts, and TLS targets must be literal IPs
  with a matching certificate IP SAN — the operator-provisioned
  ``ELSPETH_TEST_CHROMA_HOST`` / ``PORT`` / ``SSL`` values must satisfy that
  policy.  Created collections carry ``ELSPETH_TEST_CHROMA_COLLECTION_PREFIX``
  plus a UUID and are deleted in ``finally``.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

import chromadb
import chromadb.api
import chromadb.errors
import pytest

from elspeth.contracts import RunStatus
from tests.integration.plugins._azure_live import (
    jsonl_source_options,
    run_orchestrated_pipeline,
    write_jsonl,
)
from tests.integration.plugins._dataverse_chroma_live import (
    CHROMA_COLLECTION_PREFIX_ENV,
    CHROMA_HOST_ENV,
    CHROMA_PORT_ENV,
    CHROMA_SINK_MODE_VARIANTS,
    CHROMA_SSL_ENV,
    require_resource,
)

pytestmark = [pytest.mark.integration, pytest.mark.live_chroma, pytest.mark.live_provider]

_DOCUMENT_TEXT = "elspeth state-engine live acceptance document"


@dataclass(frozen=True, slots=True)
class ChromaVariantTarget:
    """Resolved connection facts and disposable-collection identity for one variant."""

    variant: str
    collection: str
    host: str | None
    port: int
    ssl: bool


@pytest.fixture(params=CHROMA_SINK_MODE_VARIANTS)
def chroma_variant_target(request: pytest.FixtureRequest) -> ChromaVariantTarget:
    """Resolve one connection-mode variant's live resources, skipping when unconfigured."""
    variant = request.param
    prefix = require_resource(CHROMA_COLLECTION_PREFIX_ENV)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", prefix):
        raise AssertionError(f"{CHROMA_COLLECTION_PREFIX_ENV} must satisfy the Chroma collection-name grammar, got {prefix!r}")
    collection = f"{prefix}-{uuid.uuid4().hex[:12]}"
    if variant == "persistent":
        return ChromaVariantTarget(variant=variant, collection=collection, host=None, port=8000, ssl=True)
    host = require_resource(CHROMA_HOST_ENV)
    port_text = require_resource(CHROMA_PORT_ENV)
    if not re.fullmatch(r"[0-9]+", port_text):
        raise AssertionError(f"{CHROMA_PORT_ENV} must be a decimal port number, got {port_text!r}")
    ssl_text = require_resource(CHROMA_SSL_ENV).lower()
    if ssl_text not in {"true", "false"}:
        raise AssertionError(f"{CHROMA_SSL_ENV} must be 'true' or 'false', got {ssl_text!r}")
    return ChromaVariantTarget(
        variant=variant,
        collection=collection,
        host=host,
        port=int(port_text),
        ssl=ssl_text == "true",
    )


def verification_client(target: ChromaVariantTarget, persist_directory: Path) -> chromadb.api.ClientAPI:
    """Independent read-back handle for the variant's Chroma store (support only)."""
    if target.variant == "persistent":
        return chromadb.PersistentClient(path=str(persist_directory))
    assert target.host is not None
    return chromadb.HttpClient(host=target.host, port=target.port, ssl=target.ssl)


def test_chroma_sink_live_lifecycle_writes_document(
    chroma_variant_target: ChromaVariantTarget,
    tmp_path: Path,
) -> None:
    target = chroma_variant_target
    persist_directory = tmp_path / "chroma-store"
    doc_id = f"{target.collection}-doc-1"
    input_path = tmp_path / "rows.jsonl"
    write_jsonl(input_path, [{"doc_id": doc_id, "text": _DOCUMENT_TEXT}])

    sink_options: dict[str, object] = {
        "collection": target.collection,
        "mode": target.variant,
        "field_mapping": {"document_field": "text", "id_field": "doc_id"},
        "on_duplicate": "overwrite",
        "schema": {"mode": "observed"},
    }
    if target.variant == "persistent":
        sink_options["persist_directory"] = str(persist_directory)
    else:
        assert target.host is not None
        sink_options["host"] = target.host
        sink_options["port"] = target.port
        sink_options["ssl"] = target.ssl

    pipeline: dict[str, object] = {
        "sources": {
            "input": {
                "plugin": "json",
                "on_success": "subject_sink",
                "options": jsonl_source_options(input_path),
            }
        },
        "sinks": {
            "subject_sink": {
                "plugin": "chroma_sink",
                "on_write_failure": "discard",
                "options": sink_options,
            }
        },
    }

    try:
        evidence = run_orchestrated_pipeline(pipeline, tmp_path)
        assert evidence.status is RunStatus.COMPLETED, f"variant {target.variant}: {evidence}"
        evidence.assert_completed_lifecycle()

        # External observation: the document must be visible to an independent read.
        reader = verification_client(target, persist_directory)
        fetched = reader.get_collection(target.collection).get(ids=[doc_id])
        assert fetched["ids"] == [doc_id]
        assert fetched["documents"] == [_DOCUMENT_TEXT]
    finally:
        if target.variant == "client":
            cleanup = verification_client(target, persist_directory)
            with contextlib.suppress(chromadb.errors.NotFoundError):
                cleanup.delete_collection(target.collection)
