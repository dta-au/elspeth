"""Freeform sink-path canonicalization never depends on a workflow mode."""

import pytest

from elspeth.web.composer.path_canonicalization import canonical_sink_local_paths


def test_relative_sink_path_uses_managed_output_pool() -> None:
    assert canonical_sink_local_paths({"path": "report.jsonl"}) == {"path": "outputs/report.jsonl"}


def test_parent_traversal_is_rejected_without_echoing_path() -> None:
    with pytest.raises(ValueError, match="must not contain") as error:
        canonical_sink_local_paths({"path": "../private/report.jsonl"})
    assert "../private/report.jsonl" not in str(error.value)
