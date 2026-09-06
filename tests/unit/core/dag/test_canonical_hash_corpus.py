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

The corpus is built under a CONTROLLED environment: every ``${VAR}`` the
examples reference resolves to a fixed, visibly non-secret placeholder
(:data:`_PLACEHOLDER_ENV`) and nothing else from the process environment or
an operator's ``.env`` reaches the build. The topology hash covers each
node's resolved config, so a pin recorded with a real key in the environment
can only ever pass on the box that holds that key (it did: 11 hashes were
recorded from the maintainer's key and failed in CI, elspeth-bc97e06221 B5)
and is a secret-derived value in the repository. Whether a secret's VALUE
should enter the topology hash at all is elspeth-fdfc6fe095; until that
lands, the placeholders make the pin reproducible everywhere and secret-free.
"""

from __future__ import annotations

import json
import os
import re
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
_PLACEHOLDER_ENV: dict[str, str] = {
    # Every environment variable an examples/*/settings*.yaml references
    # (`grep -oE '\$\{[A-Z_]+' examples/*/settings*.yaml`), each bound to a
    # value that is obviously not a credential. URL-shaped variables get a
    # placeholder host so URL validation is exercised, not bypassed.
    "AZURE_CONTENT_SAFETY_ENDPOINT": "https://placeholder-not-a-secret.cognitiveservices.azure.com",
    "AZURE_CONTENT_SAFETY_KEY": "placeholder-not-a-secret-AZURE_CONTENT_SAFETY_KEY",
    "AZURE_OPENAI_API_VERSION": "placeholder-not-a-secret-AZURE_OPENAI_API_VERSION",
    "AZURE_OPENAI_DEPLOYMENT": "placeholder-not-a-secret-AZURE_OPENAI_DEPLOYMENT",
    "AZURE_OPENAI_ENDPOINT": "https://placeholder-not-a-secret.openai.azure.com",
    "AZURE_OPENAI_KEY": "placeholder-not-a-secret-AZURE_OPENAI_KEY",
    "AZURE_STORAGE_ACCOUNT_URL": "https://placeholder-not-a-secret.blob.core.windows.net",
    "AZURE_STORAGE_CONTAINER": "placeholder-not-a-secret-AZURE_STORAGE_CONTAINER",
    "AZURE_STORAGE_SAS_TOKEN": "placeholder-not-a-secret-AZURE_STORAGE_SAS_TOKEN",
    "CHAOSLLM_ENDURANCE_INPUT_PATH": "/placeholder-not-a-secret/CHAOSLLM_ENDURANCE_INPUT_PATH.jsonl",
    "OPENROUTER_API_KEY": "placeholder-not-a-secret-OPENROUTER_API_KEY",
}


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


def _referenced_environment_variables() -> frozenset[str]:
    names: set[str] = set()
    for path in _settings_files():
        names.update(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)", path.read_text(encoding="utf-8")))
    return frozenset(names)


def _build_corpus(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, str]]:
    # Only the placeholders reach the build: an operator's real values (or a
    # loaded .env) must neither make an example buildable nor move a hash.
    for name in list(os.environ):
        if name in _PLACEHOLDER_ENV or name.startswith("ELSPETH_") or name.startswith(("AZURE_", "OPENROUTER_", "AWS_")):
            monkeypatch.delenv(name, raising=False)
    for name, value in _PLACEHOLDER_ENV.items():
        monkeypatch.setenv(name, value)
    hashes: dict[str, str] = {}
    unbuildable: dict[str, str] = {}
    for path in _settings_files():
        rel = str(path.relative_to(_REPO_ROOT))
        try:
            hashes[rel] = _topology_hash(path)
        except Exception as exc:  # pylint: disable=broad-except — roster records WHY, test pins the roster
            unbuildable[rel] = type(exc).__name__
    return {"hashes": hashes, "unbuildable": unbuildable}


def test_placeholder_environment_covers_every_variable_the_examples_reference() -> None:
    referenced = _referenced_environment_variables()
    assert referenced == frozenset(_PLACEHOLDER_ENV), sorted(referenced ^ frozenset(_PLACEHOLDER_ENV))
    for name, value in _PLACEHOLDER_ENV.items():
        assert "placeholder-not-a-secret" in value, name


def test_examples_canonical_hash_corpus_is_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    corpus = _build_corpus(monkeypatch)
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
