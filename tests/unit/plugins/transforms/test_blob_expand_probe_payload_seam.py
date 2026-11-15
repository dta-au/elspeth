"""The blob expanders' invariant-probe payload seam honours the bounded-read contract.

The P3 blob-rasterize change (#95) moved BlobCSVExpand, BlobJSONExpand and
BlobTextExpand from ``PayloadStore.retrieve`` to
``PayloadStore.retrieve_bounded(ref, max_bytes=max_blob_bytes)``. Each plugin's
``execute_forward_invariant_probe`` installs a hermetic payload store so the
ADR-009 governance harness can drive the real ``process()`` path; that seam still
implemented only ``retrieve``, so every forward probe raised ``AttributeError``
and twelve tests under ``tests/invariants`` went red.

A seam that answered ``retrieve_bounded`` with the whole payload regardless of
``max_bytes`` would turn those tests green while hiding a cap regression. These
tests pin the seam to the real store's contract: content within the cap is
returned, an oversize payload yields ``None`` (which the plugin reports as
``blob_too_large``), and the probe succeeds under the default cap.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.transforms.blob_csv_expand import BlobCSVExpand
from elspeth.plugins.transforms.blob_json_expand import BlobJSONExpand
from elspeth.plugins.transforms.blob_text_expand import BlobTextExpand
from elspeth.testing import make_pipeline_row
from tests.fixtures.factories import make_context

_BLOB_EXPANDERS: tuple[type[BaseTransform], ...] = (BlobCSVExpand, BlobJSONExpand, BlobTextExpand)


def _run_probe(plugin_cls: type[BaseTransform], **overrides: Any) -> Any:
    config = {**plugin_cls.probe_config(), **overrides}
    transform = plugin_cls(config)
    probe_rows = transform.forward_invariant_probe_rows(make_pipeline_row({"baseline": "kept"}))
    return transform.execute_forward_invariant_probe(probe_rows, make_context())


@pytest.mark.parametrize("plugin_cls", _BLOB_EXPANDERS, ids=lambda cls: cls.__name__)
def test_probe_seam_serves_the_bounded_read_within_the_cap(plugin_cls: type[BaseTransform]) -> None:
    result = _run_probe(plugin_cls)

    assert result.status == "success", result.reason


@pytest.mark.parametrize("plugin_cls", _BLOB_EXPANDERS, ids=lambda cls: cls.__name__)
def test_probe_seam_refuses_a_payload_over_the_cap(plugin_cls: type[BaseTransform]) -> None:
    result = _run_probe(plugin_cls, max_blob_bytes=1)

    assert result.status == "error"
    assert result.reason is not None
    assert result.reason["reason"] == "blob_too_large"
    assert result.reason["max_blob_bytes"] == 1
