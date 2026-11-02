"""Classifier PRIMARY reuses the exact original guarded provider input."""

from elspeth.web.composer.provider_gateway import ProviderGateway
from tests.unit.web.composer.test_llm_sampling_config import _settings


def test_prepared_classifier_primary_preserves_original_input_and_request_hash(tmp_path) -> None:
    gateway = ProviderGateway(model="test/model", settings=_settings(tmp_path), endpoint_base_url=None, endpoint_api_key=None)
    messages = [{"role": "user", "content": "The exact original planning request."}]
    prepared = gateway.prepare_loop_input(messages, ())
    from_original = gateway.prepare_continuation_primary_from_input(prepared)
    ordinary = gateway.prepare_continuation_primary(messages, ())
    assert from_original.provider_input is prepared
    assert from_original.input_sha256 == ordinary.input_sha256
    assert from_original.kwargs == ordinary.kwargs
    assert gateway.prepare_continuation_primary_from_input(prepared).provider_input is prepared
