from elspeth.web.composer.continuation_provider_input import continuation_provider_input_hash


def test_complete_native_input_and_semantic_options_bind_the_prepared_effect() -> None:
    original = {
        "model": "model",
        "messages": [{"role": "user", "content": "exact original"}],
        "temperature": 0.1,
        "reasoning_effort": "high",
    }
    expected = continuation_provider_input_hash(original)
    assert continuation_provider_input_hash(original | {"timeout": 10, "api_key": "fixture-transport"}) == expected
    assert continuation_provider_input_hash(original | {"temperature": 0.2}) != expected
    assert continuation_provider_input_hash(original | {"messages": [{"role": "user", "content": "changed"}]}) != expected
    assert continuation_provider_input_hash(original | {"tools": []}) != expected
    assert continuation_provider_input_hash(original | {"api_base": "https://other-provider.example/v1"}) != expected
    assert continuation_provider_input_hash(original | {"custom_llm_provider": "another-provider"}) != expected
    assert continuation_provider_input_hash(original | {"model": "another-model"}) != expected
