"""Marker configuration must not silently disable intended fault injection."""

from pathlib import Path

import pytest
from tests.fixtures.chaosweb import _build_config_from_marker


@pytest.fixture(autouse=True)
def register_chaosweb_marker(pytestconfig: pytest.Config) -> None:
    pytestconfig.addinivalue_line("markers", "chaosweb: configure the ChaosWeb fixture")


def test_marker_rejects_positional_arguments(tmp_path: Path) -> None:
    marker = pytest.mark.chaosweb({"rate_limit_pct": 100.0}).mark
    with pytest.raises(pytest.UsageError, match="positional"):
        _build_config_from_marker(marker, tmp_path)


def test_marker_loads_preset(tmp_path: Path) -> None:
    config = _build_config_from_marker(pytest.mark.chaosweb(preset="stress_scraping").mark, tmp_path)
    assert config.error_injection.rate_limit_pct == 15.0


@pytest.mark.parametrize("unknown", ["rate_limit", "not_found"])
@pytest.mark.parametrize("with_valid_override", [False, True])
def test_marker_rejects_unknown_arguments(tmp_path: Path, unknown: str, with_valid_override: bool) -> None:
    kwargs = {unknown: 25.0}
    if with_valid_override:
        kwargs["rate_limit_pct"] = 10.0
    marker = pytest.mark.chaosweb(**kwargs).mark

    with pytest.raises(pytest.UsageError, match=unknown):
        _build_config_from_marker(marker, tmp_path)


def test_marker_applies_supported_overrides(tmp_path: Path) -> None:
    marker = pytest.mark.chaosweb(rate_limit_pct=25.0, base_ms=4, jitter_ms=2, content_mode="random").mark
    config = _build_config_from_marker(marker, tmp_path)

    assert config.error_injection.rate_limit_pct == 25.0
    assert config.latency.base_ms == 4
    assert config.latency.jitter_ms == 2
    assert config.content.mode == "random"


@pytest.mark.parametrize("with_marker", [False, True])
def test_absent_overrides_keep_clean_config(tmp_path: Path, with_marker: bool) -> None:
    marker = pytest.mark.chaosweb().mark if with_marker else None
    config = _build_config_from_marker(marker, tmp_path)

    assert config.error_injection.rate_limit_pct == 0
    assert config.latency.base_ms == 0
    assert config.latency.jitter_ms == 0
