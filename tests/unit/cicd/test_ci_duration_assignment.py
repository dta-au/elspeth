"""Duration estimates balance native collections without defining membership."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from scripts.ci_test_shards import (
    DURATION_PROFILE_PATH,
    LONG_RUN_CASES,
    load_duration_profile,
    ordinary_assignment,
    select_long_run_nodeids,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _profile(checkout: Path, weights: dict[str, int]) -> Path:
    path = checkout / DURATION_PROFILE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 1, "default_weight_ms": 1, "weights_ms": weights, "source": {}}),
        encoding="utf-8",
    )
    return path


def test_duration_assignment_balances_long_tests_and_preserves_native_order(tmp_path: Path) -> None:
    _profile(tmp_path, {"slow-a": 100, "slow-b": 90, "medium": 10, "removed": 10000})
    profile = load_duration_profile(tmp_path)
    ordinary = ["new-b", "slow-b", "medium", "new-a", "slow-a"]
    expected = {"slow-a": 0, "slow-b": 1, "medium": 1, "new-a": 0, "new-b": 1}
    assert ordinary_assignment(ordinary, 2, profile) == expected
    assert ordinary_assignment(list(reversed(ordinary)), 2, profile) == expected
    marked = list(LONG_RUN_CASES.values())
    selected = [select_long_run_nodeids(ordinary + marked, marked, "ordinary", None, 2, i, profile) for i in range(2)]
    assert selected == [["new-a", "slow-a"], ["new-b", "slow-b", "medium"]]
    assert {node for group in selected for node in group} == set(ordinary)
    assert "removed" not in expected
    assert [sum(profile.weights_ms.get(n, profile.default_weight_ms) for n in group) for group in selected] == [101, 101]


def test_equal_weights_and_unseen_ids_use_stable_node_and_shard_ties(tmp_path: Path) -> None:
    _profile(tmp_path, {})
    profile = load_duration_profile(tmp_path)
    assert ordinary_assignment(["d", "c", "b", "a"], 2, profile) == {"a": 0, "b": 1, "c": 0, "d": 1}
    assert ordinary_assignment([], 8, profile) == {}
    assert ordinary_assignment(["a"], 8, profile) == {"a": 0}
    assert ordinary_assignment(["a", "b"], 1, profile) == {"a": 0, "b": 0}


@pytest.mark.parametrize("seed", ["1", "999", "random"])
def test_assignment_is_independent_of_python_hash_seed(tmp_path: Path, seed: str) -> None:
    _profile(tmp_path, {"z": 10})
    script = (
        "import json,sys\nfrom pathlib import Path\n"
        "from scripts.ci_test_shards import ordinary_assignment,load_duration_profile\n"
        "print(json.dumps(ordinary_assignment(['z','a','b','c'],2,load_duration_profile(Path(sys.argv[1]))),sort_keys=True))\n"
    )
    environment = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(REPO_ROOT)}
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"z": 0, "a": 1, "b": 1, "c": 1}


@pytest.mark.parametrize("count", [True, 2.0, 0, -1])
def test_assignment_refuses_invalid_count(tmp_path: Path, count: int) -> None:
    _profile(tmp_path, {})
    profile = load_duration_profile(tmp_path)
    assert ordinary_assignment(["a"], 1, profile) == {"a": 0}
    with pytest.raises(ValueError):
        ordinary_assignment(["a"], count, profile)


def test_assignment_refuses_duplicate_native_ids(tmp_path: Path) -> None:
    _profile(tmp_path, {})
    profile = load_duration_profile(tmp_path)
    assert ordinary_assignment(["a", "b"], 2, profile) == {"a": 0, "b": 1}
    with pytest.raises(ValueError):
        ordinary_assignment(["a", "a"], 2, profile)


@pytest.mark.parametrize("field", ["default_weight_ms", "weights_ms"])
@pytest.mark.parametrize("weight", [True, False, 0, -1, 1.0, "1", None, float("nan"), float("inf")])
def test_profile_refuses_nonpositive_or_nonliteral_weights(tmp_path: Path, field: str, weight: object) -> None:
    path = _profile(tmp_path, {"a": 1})
    assert load_duration_profile(tmp_path).weights_ms["a"] == 1
    value = json.loads(path.read_text())
    value[field] = weight if field == "default_weight_ms" else {"a": weight}
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        load_duration_profile(tmp_path)


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        '{"schema_version":1}',
        '{"schema_version":1,"schema_version":1,"default_weight_ms":1,"weights_ms":{},"source":{}}',
        '{"schema_version":1,"default_weight_ms":1,"weights_ms":{"a":1,"a":2},"source":{}}',
        '{"schema_version":true,"default_weight_ms":1,"weights_ms":{},"source":{}}',
        '{"schema_version":1.0,"default_weight_ms":1,"weights_ms":{},"source":{}}',
        '{"schema_version":1,"default_weight_ms":1,"weights_ms":{"":1},"source":{}}',
        '{"schema_version":1,"default_weight_ms":1,"weights_ms":{},"source":[]}',
    ],
)
def test_profile_refuses_ambiguous_shape_and_duplicate_keys(tmp_path: Path, raw: str) -> None:
    path = _profile(tmp_path, {})
    assert load_duration_profile(tmp_path).default_weight_ms == 1
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError):
        load_duration_profile(tmp_path)


def test_profile_digest_binds_exact_bytes_and_weights_are_immutable(tmp_path: Path) -> None:
    path = _profile(tmp_path, {"a": 1})
    profile = load_duration_profile(tmp_path)
    with pytest.raises(TypeError):
        profile.weights_ms["a"] = 2
    path.write_bytes(path.read_bytes() + b"\n")
    changed = load_duration_profile(tmp_path)
    assert changed.weights_ms == profile.weights_ms
    assert changed.sha256 != profile.sha256


def test_checked_in_profile_is_an_estimate_and_contains_no_proof_membership() -> None:
    profile = load_duration_profile(REPO_ROOT)
    source = json.loads((REPO_ROOT / DURATION_PROFILE_PATH).read_text())["source"]
    assert source["estimate_only"] is True
    assert source["coverage"] is False
    assert source["python_version"] == "3.13.15"
    assert set(profile.weights_ms).isdisjoint(LONG_RUN_CASES.values())
    assert profile.default_weight_ms > 0
