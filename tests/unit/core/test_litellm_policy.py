"""Fresh processes prove pricing import policy without incidental plugin imports."""

import builtins
import os
import subprocess
import sys
from typing import Any

import pytest

from elspeth.core.llm_pricing import calculate_missing_provider_cost


@pytest.mark.parametrize("missing_module", ["litellm", "litellm_transitive_dependency"])
def test_missing_optional_pricing_library_is_unavailable_but_broken_install_raises(
    monkeypatch: pytest.MonkeyPatch, missing_module: str
) -> None:
    original_import = builtins.__import__
    observed: list[str] = []

    def controlled_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "litellm":
            observed.append(name)
            raise ModuleNotFoundError("controlled missing module", name=missing_module)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", controlled_import)
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    if missing_module == "litellm":
        assert calculate_missing_provider_cost(usage, pricing_model="azure/gpt-4o") == (None, "not_available")
    else:
        with pytest.raises(ModuleNotFoundError) as raised:
            calculate_missing_provider_cost(usage, pricing_model="azure/gpt-4o")
        assert raised.value.name == missing_module
    assert observed == ["litellm"]


@pytest.mark.parametrize("configured", [None, "False"])
def test_core_pricing_sets_local_default_and_preserves_operator_override(configured: str | None) -> None:
    env = dict(os.environ)
    env.pop("LITELLM_LOCAL_MODEL_COST_MAP", None)
    if configured is not None:
        env["LITELLM_LOCAL_MODEL_COST_MAP"] = configured
    script = """
import os
import sys
assert "litellm" not in sys.modules
from elspeth.core.llm_pricing import calculate_missing_provider_cost
assert "litellm" not in sys.modules
assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == sys.argv[1]
if sys.argv[1] == "True":
    import socket
    def forbidden(*args, **kwargs):
        raise AssertionError("pricing must not access the network by default")
    socket.socket.connect = forbidden
    cost, source = calculate_missing_provider_cost(
        {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        pricing_model="openai/gpt-4o-2024-08-06",
    )
    assert abs(cost - 0.00045) < 1e-12
    assert source == "litellm.cost_per_token"
"""
    result = subprocess.run(
        [sys.executable, "-c", script, configured or "True"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
