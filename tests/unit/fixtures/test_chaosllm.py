"""ChaosLLM marker admission preserves every authored fault control."""

from pathlib import Path

import pytest
from tests.fixtures.chaosllm import _build_config_from_marker


@pytest.fixture(autouse=True)
def register_chaosllm_marker(pytestconfig: pytest.Config) -> None:
    pytestconfig.addinivalue_line("markers", "chaosllm: configure the ChaosLLM fixture")


@pytest.mark.parametrize("with_valid_override", [False, True])
def test_marker_rejects_unknown_arguments(tmp_path: Path, with_valid_override: bool) -> None:
    kwargs = {"rate_limit": 100.0}
    if with_valid_override:
        kwargs["rate_limit_pct"] = 10.0
    with pytest.raises(pytest.UsageError, match="rate_limit"):
        _build_config_from_marker(pytest.mark.chaosllm(**kwargs).mark, tmp_path)


def test_marker_rejects_positional_arguments(tmp_path: Path) -> None:
    with pytest.raises(pytest.UsageError, match="positional"):
        _build_config_from_marker(pytest.mark.chaosllm({"rate_limit_pct": 100.0}).mark, tmp_path)


@pytest.mark.parametrize("with_marker", [False, True])
def test_absent_overrides_keep_clean_config(tmp_path: Path, with_marker: bool) -> None:
    config = _build_config_from_marker(pytest.mark.chaosllm().mark if with_marker else None, tmp_path)
    assert config.error_injection.rate_limit_pct == 0
    assert config.latency.base_ms == 0
    assert config.latency.jitter_ms == 0


def test_marker_loads_preset(tmp_path: Path) -> None:
    config = _build_config_from_marker(pytest.mark.chaosllm(preset="gentle").mark, tmp_path)
    assert config.error_injection.rate_limit_pct == 1.0


def test_marker_applies_overrides(tmp_path: Path) -> None:
    config = _build_config_from_marker(pytest.mark.chaosllm(preset="gentle", rate_limit_pct=25.0, base_ms=4, jitter_ms=2).mark, tmp_path)
    assert config.error_injection.rate_limit_pct == 25.0
    assert config.latency.base_ms == 4
    assert config.latency.jitter_ms == 2
