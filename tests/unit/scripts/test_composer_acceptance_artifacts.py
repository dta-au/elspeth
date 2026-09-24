"""Cumulative sink publications retain history but only the stream tip is current."""

from copy import deepcopy
from pathlib import Path

import pytest
from scripts.composer_acceptance import runner
from scripts.composer_acceptance.artifacts import current_artifacts

from tests.unit.scripts.test_composer_acceptance_runner import ScenarioApi, _session_store
from tests.unit.scripts.test_composer_convergence_checks import CASES, _evidence


def _publications() -> tuple[list[dict], list[dict]]:
    artifacts = [
        {
            "artifact_id": "a0",
            "sink_effect_id": "e0",
            "sink_node_id": "quarantine",
            "path_or_uri": "file:///output/quarantine.csv",
            "size_bytes": 24,
        },
        {
            "artifact_id": "a1",
            "sink_effect_id": "e1",
            "sink_node_id": "quarantine",
            "path_or_uri": "file:///output/quarantine.csv",
            "size_bytes": 34,
        },
    ]
    effects = [
        {
            "effect_id": "e0",
            "artifact_id": "a0",
            "sink_node_id": "quarantine",
            "stream_id": "s",
            "stream_sequence": 0,
            "predecessor_effect_id": None,
            "state": "finalized",
        },
        {
            "effect_id": "e1",
            "artifact_id": "a1",
            "sink_node_id": "quarantine",
            "stream_id": "s",
            "stream_sequence": 1,
            "predecessor_effect_id": "e0",
            "state": "finalized",
        },
    ]
    return artifacts, effects


@pytest.mark.parametrize("reverse", [False, True])
def test_selects_final_cumulative_publication_by_chain_not_manifest_order(reverse: bool) -> None:
    artifacts, effects = _publications()
    original = deepcopy(artifacts)
    assert current_artifacts(list(reversed(artifacts)) if reverse else artifacts, list(reversed(effects))) == [artifacts[1]]
    assert artifacts == original


@pytest.mark.parametrize(
    "field,value",
    [
        ("stream_id", "other"),
        ("predecessor_effect_id", "missing"),
        ("stream_sequence", 4),
        ("state", "prepared"),
        ("artifact_id", "wrong"),
        ("sink_node_id", "wrong"),
    ],
)
def test_rejects_unproven_supersession(field: str, value: object) -> None:
    artifacts, effects = _publications()
    effects[1][field] = value
    with pytest.raises(ValueError):
        current_artifacts(artifacts, effects)


def test_rejects_branched_stream() -> None:
    artifacts, effects = _publications()
    artifacts.append({**artifacts[1], "artifact_id": "a2", "sink_effect_id": "e2"})
    effects.append({**effects[1], "effect_id": "e2", "artifact_id": "a2"})
    with pytest.raises(ValueError, match="branch"):
        current_artifacts(artifacts, effects)


def test_keeps_distinct_targets_even_when_basenames_match() -> None:
    artifacts, effects = _publications()
    artifacts[1]["path_or_uri"] = "file:///other/quarantine.csv"
    effects[1].update(stream_id="other", stream_sequence=0, predecessor_effect_id=None)
    assert current_artifacts(artifacts, effects) == artifacts


def test_rejects_legacy_duplicate_target_without_effect_evidence() -> None:
    artifacts, _ = _publications()
    artifacts[0]["sink_effect_id"] = None
    with pytest.raises(ValueError):
        current_artifacts(artifacts, [])


def test_keeps_unique_artifact_without_requiring_effect_history() -> None:
    artifacts, _ = _publications()
    artifacts[0]["sink_effect_id"] = None
    assert current_artifacts(artifacts[:1], []) == artifacts[:1]


@pytest.mark.parametrize("singleton", [False, True])
def test_rejects_manifest_missing_known_finalized_stream_tip(singleton: bool) -> None:
    artifacts, effects = _publications()
    effects.append({**effects[1], "effect_id": "e2", "artifact_id": "a2", "stream_sequence": 2, "predecessor_effect_id": "e1"})
    with pytest.raises(ValueError, match="incomplete"):
        current_artifacts(artifacts[:1] if singleton else artifacts, effects)


@pytest.mark.parametrize("other_stream,other_sink", [("other", "quarantine"), ("s", "other"), ("other", "other")])
def test_unrelated_finalized_effect_does_not_supersede_target(other_stream: str, other_sink: str) -> None:
    artifacts, effects = _publications()
    effects.append(
        {**effects[0], "effect_id": "unrelated", "artifact_id": "elsewhere", "stream_id": other_stream, "sink_node_id": other_sink}
    )
    assert current_artifacts(artifacts, effects) == [artifacts[1]]


def test_unique_effect_artifact_requires_its_audit_evidence() -> None:
    artifacts, _ = _publications()
    with pytest.raises(ValueError, match="evidence"):
        current_artifacts(artifacts[:1], [])


class CumulativeApi(ScenarioApi):
    drift = False

    def json(self, path: str, body: dict | None = None):
        value = super().json(path, body)
        if path.endswith("/outputs"):
            final = next(item for item in value["artifacts"] if item["path_or_uri"] == "quarantine.csv")
            final.update(sink_effect_id="e1", sink_node_id="quarantine")
            value["artifacts"].insert(0, {**final, "artifact_id": "old", "sink_effect_id": "e0"})
        return value

    def raw(self, path: str, body: dict | None = None) -> bytes:
        if "/old/content" in path:
            raise AssertionError("Historical artifact must not be downloaded")
        if self.drift:
            raise runner.ApiError(409, "artifact_content_drift")
        return super().raw(path, body)


@pytest.mark.parametrize("drift", [False, True])
def test_runner_downloads_tip_but_does_not_hide_genuine_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: bool) -> None:
    case = next(item for item in CASES if item["id"] == "02_numeric_quarantine")
    api = CumulativeApi(case)
    api.approved = True
    api.drift = drift
    _session_store(tmp_path)
    _, effects = _publications()
    final_id = str(list(api.outputs).index("quarantine.csv"))
    effects[0]["artifact_id"] = "old"
    effects[1]["artifact_id"] = final_id
    monkeypatch.setattr(runner, "assert_frozen", lambda output: "frozen")
    monkeypatch.setattr(
        runner, "runtime_evidence", lambda output, out, scenario, state, run: {**_evidence(scenario, state, run), "sink_effects": effects}
    )
    result = runner.run_case(api, case, tmp_path, set())
    assert result["status"] == ("harness_error" if drift else "passed")
    if drift:
        assert result["failures"] == ["HTTP 409"]
