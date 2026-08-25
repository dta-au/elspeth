"""Canonical-hash pin corpus (spec §3 / quality F7).

Records the full topology hash of every buildable ``examples/`` settings
file at pre-WS2 HEAD and asserts them byte-identical thereafter. Spec §3:
"No YAML churn for any pipeline that passes the new §7 validation;
canonical hash of such pipelines does not move."

Regenerate ONLY with an adjudicated reason (a deliberate canonicalization
change is a reviewed decision, never a drive-by re-pin):

    ELSPETH_CANONICAL_CORPUS_RECORD=1 pytest \
        tests/unit/core/dag/test_canonical_hash_corpus.py -x

The unbuildable roster is pinned too: a settings file that stops building
(or starts building) is a corpus change, not a silent skip.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from elspeth.core.canonical import compute_full_topology_hash
from elspeth.core.config import load_settings
from elspeth.core.dag import ExecutionGraph
from elspeth.plugins.infrastructure.runtime_factory import instantiate_plugins_from_config

_REPO_ROOT = Path(__file__).resolve().parents[4]
_EXAMPLES = _REPO_ROOT / "examples"
_PINS_PATH = Path(__file__).parent / "canonical_hash_corpus.json"
_RECORD = os.environ.get("ELSPETH_CANONICAL_CORPUS_RECORD") == "1"


def _settings_files() -> list[Path]:
    return sorted(_EXAMPLES.glob("*/settings*.yaml"))


def _topology_hash(settings_path: Path) -> str:
    config = load_settings(settings_path)
    plugins = instantiate_plugins_from_config(config, preflight_mode=True)
    graph = ExecutionGraph.from_plugin_instances(
        sources=plugins.sources,
        source_settings_map=plugins.source_settings_map,
        transforms=plugins.transforms,
        sinks=plugins.sinks,
        aggregations=plugins.aggregations,
        gates=list(config.gates),
        coalesce_settings=list(config.coalesce) or None,
        queues=config.queues,
        row_union_settings=list(config.row_unions) or None,
        collectors=plugins.collectors or None,
        scope_settings=list(config.scopes) or None,
        max_bound_region_depth=config.max_bound_region_depth,
    )
    return compute_full_topology_hash(graph)


def _build_corpus() -> dict[str, dict[str, str]]:
    hashes: dict[str, str] = {}
    unbuildable: dict[str, str] = {}
    for path in _settings_files():
        rel = str(path.relative_to(_REPO_ROOT))
        try:
            hashes[rel] = _topology_hash(path)
        except Exception as exc:  # pylint: disable=broad-except — roster records WHY, test pins the roster
            unbuildable[rel] = type(exc).__name__
    return {"hashes": hashes, "unbuildable": unbuildable}


def test_examples_canonical_hash_corpus_is_pinned() -> None:
    corpus = _build_corpus()
    if _RECORD:
        _PINS_PATH.write_text(json.dumps(corpus, indent=2, sort_keys=True) + "\n")
        pytest.fail("Corpus recorded to canonical_hash_corpus.json — commit it and re-run without ELSPETH_CANONICAL_CORPUS_RECORD.")
    pinned = json.loads(_PINS_PATH.read_text())
    # Roster first: a moved roster with matching hashes is still a corpus change.
    assert sorted(corpus["unbuildable"]) == sorted(pinned["unbuildable"]), (
        "Unbuildable-example roster moved. A file that stops (or starts) building is a "
        "corpus change requiring adjudication, not a silent skip."
    )
    assert corpus["hashes"] == pinned["hashes"], (
        "Canonical topology hash moved for a pipeline that passes §7 validation — "
        "spec §3 pins these byte-identical across WS2. Diff the two dicts, find the "
        "node whose canonical config changed, and fix the serialization (likely a "
        "key that stopped being omitted-when-None) rather than re-pinning."
    )
