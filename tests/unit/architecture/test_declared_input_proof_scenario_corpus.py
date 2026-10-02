"""The DAG scenario corpus pins the build's declared-input proof (elspeth-5887fb7928 R2, systems C4).

Twin of ``tests/unit/core/dag/test_canonical_hash_corpus.py::test_examples_declared_input_proof_is_pinned``
over the scenario corpus: every harness case is built through the corpus's
own production assembly (``build_scenario``) and its proof map — keyed by
node name — is compared with the pin. The engine aborts on a miss of a
PROVEN field and routes a miss the build never proved (ADR-013 Amendment
2026-09-27), so a presence-vote change that under-proves converts aborts
into routed rows without any other test noticing. Judge every moved entry
before re-recording with

    ELSPETH_DECLARED_INPUT_PROOF_RECORD=1 pytest tests/unit/architecture/test_declared_input_proof_scenario_corpus.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from tests.fixtures.dag_scenario_corpus.harness import build_scenario, render_settings
from tests.fixtures.dag_scenario_corpus.loader import iter_harness_cases, load_manifest
from tests.fixtures.dag_scenario_corpus.plugins import install_corpus_plugin_manager
from tests.fixtures.declared_input_proof_pin import named_declared_input_proof

_PINS_PATH = Path(__file__).parent / "declared_input_proof_scenario_corpus.json"
_RECORD = os.environ.get("ELSPETH_DECLARED_INPUT_PROOF_RECORD") == "1"


def test_scenario_corpus_declared_input_proof_is_pinned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_corpus_plugin_manager(monkeypatch)
    proofs: dict[str, dict[str, list[str]]] = {}
    for scenario, case in iter_harness_cases(load_manifest()):
        key = f"{scenario.id}/{case.id}"
        case_dir = tmp_path / key.replace("/", "__")
        case_dir.mkdir()
        proofs[key] = named_declared_input_proof(build_scenario(render_settings(case, case_dir)).graph)
    if _RECORD:
        _PINS_PATH.write_text(json.dumps(proofs, indent=2, sort_keys=True) + "\n")
        pytest.fail(f"Proof recorded to {_PINS_PATH.name} — commit it and re-run without ELSPETH_DECLARED_INPUT_PROOF_RECORD.")
    pinned = json.loads(_PINS_PATH.read_text())
    assert sorted(proofs) == sorted(pinned), "The corpus case roster moved: add or drop the case's pin deliberately."
    assert proofs == pinned, (
        "The declared-input proof moved for a corpus case. A field that leaves a node's proof turns its "
        "run-ending miss into a routed row; a field that joins it does the reverse. Diff the two dicts "
        "and judge each moved entry against the vote change that moved it before re-recording."
    )
