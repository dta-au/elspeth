"""END full request continuity includes the gateway's actual usage opt-in."""

from copy import deepcopy

import pytest

from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer.advisor_request import build_advisor_request_options
from elspeth.web.composer.continuation_checkpoint_end_producer import checkpoint_end_request_identity
from elspeth.web.composer.continuation_provider_input import continuation_provider_input_hash
from elspeth.web.composer.provider_gateway import _apply_openrouter_app_identity, _apply_openrouter_usage_accounting


def _request(model):
    options = build_advisor_request_options(
        model=model,
        temperature=0.2,
        seed=123,
        max_tokens=128,
        reasoning_effort=None,
        api_base=None,
        api_key=None,
        structured_output=True,
        checkpoint_phase="end",
    )
    options["messages"] = [{"role": "system", "content": "Review"}, {"role": "user", "content": "Original request"}]
    return options


@pytest.mark.parametrize("model", ["openrouter/anthropic/claude-opus-4-7", "anthropic/claude-opus-4-7"])
def test_end_request_hash_survives_original_gateway_preparation(model):
    options = _request(model)
    original_messages = options["messages"]
    audit_hash = stable_hash(original_messages)
    original = deepcopy(options)
    identity = checkpoint_end_request_identity(options)
    _apply_openrouter_app_identity(options)
    _apply_openrouter_usage_accounting(options)
    assert continuation_provider_input_hash(options) == identity
    assert options["messages"] is original_messages
    assert stable_hash(options["messages"]) == audit_hash
    if model.startswith("openrouter/"):
        assert options["usage"] == {"include": True}
        assert continuation_provider_input_hash(original) != identity
    else:
        assert "usage" not in options
        assert continuation_provider_input_hash(original) == identity
    options["max_tokens"] = 129
    assert continuation_provider_input_hash(options) != identity
    assert stable_hash(options["messages"]) == audit_hash


def test_end_request_hash_preserves_explicit_usage_choice():
    options = _request("openrouter/anthropic/claude-opus-4-7")
    usage = {"include": False}
    options["usage"] = usage
    identity = checkpoint_end_request_identity(options)
    _apply_openrouter_usage_accounting(options)
    assert options["usage"] is usage
    assert continuation_provider_input_hash(options) == identity
