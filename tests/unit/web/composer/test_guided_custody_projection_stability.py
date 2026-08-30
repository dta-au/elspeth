"""Hash-stability pin for the guided custody projection (elspeth-201903a286).

``guided_custody_projection_stability.json`` was captured by running the
BASE tree's ``redact_guided_snapshot_storage_paths`` (commit 7cd2fc6db, in
the projection order both callers used: ``redact_source_storage_path``
first) over ``_guided_custody_corpus``. Every shape that projected there
must project byte-identically now — settled guided operations store
``guided_response_hash(_state_response(record))`` and replays verify
against it (``guided_replay.py``), so drift on a non-raising shape
invalidates stored hashes. The shapes that raised at base are the defect
shapes; their new outcomes are pinned separately.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from elspeth.contracts.errors import GuidedCustodyIntegrityError
from elspeth.web.composer.redaction import REDACTED_BLOB_SOURCE_PATH, redact_guided_snapshot_storage_paths, redact_source_storage_path
from tests.unit.web.composer._guided_custody_corpus import corpus

_FIXTURE = Path(__file__).with_name("guided_custody_projection_stability.json")
_BASE = json.loads(_FIXTURE.read_text(encoding="utf-8"))
_CORPUS = corpus()


def _project_like_the_callers(sources: dict[str, Any] | None, meta: dict[str, Any] | None) -> tuple[Any, Any]:
    generic = redact_source_storage_path({"sources": sources})["sources"] if sources is not None else None
    return redact_guided_snapshot_storage_paths(generic, meta, raw_sources=sources)


def test_fixture_covers_exactly_the_corpus() -> None:
    assert _BASE["base_commit"] == "7cd2fc6db"
    assert set(_BASE["cases"]) == set(_CORPUS)


@pytest.mark.parametrize("name", [name for name, case in _BASE["cases"].items() if case["outcome"] == "projected"])
def test_base_projecting_shapes_are_byte_identical(name: str) -> None:
    sources, meta = _CORPUS[name]
    expected = _BASE["cases"][name]

    projected_sources, projected_meta = _project_like_the_callers(sources, meta)

    assert json.dumps(projected_sources, sort_keys=True) == json.dumps(expected["sources"], sort_keys=True)
    assert json.dumps(projected_meta, sort_keys=True) == json.dumps(expected["composer_meta"], sort_keys=True)


@pytest.mark.parametrize("name", [name for name, case in _BASE["cases"].items() if case["outcome"] == "raised"])
def test_base_raising_shapes_now_resolve_as_specified(name: str) -> None:
    sources, meta = _CORPUS[name]
    assert meta is not None
    terminal = meta["guided_session"]["terminal"] if "terminal" in meta["guided_session"] else None

    if name.startswith("defect_fork_explicit_blob_ref_projection_order"):
        # elspeth-75d320fb25: a consistent binding, refused only by the
        # projection order; correlating on the raw sources projects it.
        projected_sources, projected_meta = _project_like_the_callers(sources, meta)
        assert projected_sources["source"]["options"]["path"] == REDACTED_BLOB_SOURCE_PATH
        assert "custody_unavailable" not in projected_meta["guided_session"]
        return
    if terminal is None:
        with pytest.raises(GuidedCustodyIntegrityError):
            _project_like_the_callers(sources, meta)
        return
    projected_sources, projected_meta = _project_like_the_callers(sources, meta)
    assert projected_meta["guided_session"]["custody_unavailable"] is True
    assert "/srv/elspeth" not in json.dumps((projected_sources, projected_meta))
