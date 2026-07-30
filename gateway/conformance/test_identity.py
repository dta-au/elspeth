"""``/readyz`` identity: the documented fields are present, and stable across
two calls (same adapter identity, same capability set, same model aliases --
nothing that varies call to call)."""

_EXPECTED_TOP_LEVEL_KEYS = {
    "ready",
    "contract_major",
    "adapter",
    "capabilities",
    "model_aliases",
    "mapping_generation",
    "oauth_fixed_lifetime",
    "errors",
}
_EXPECTED_ADAPTER_KEYS = {"name", "version", "adapter_api_major", "fingerprint"}


async def test_readyz_identity_fields_present(gateway_client):
    response = await gateway_client.get("/readyz")

    assert response.status_code in (200, 503)
    body = response.json()
    assert set(body.keys()) == _EXPECTED_TOP_LEVEL_KEYS
    assert set(body["adapter"].keys()) == _EXPECTED_ADAPTER_KEYS
    assert isinstance(body["contract_major"], int)
    assert isinstance(body["capabilities"], list)
    assert isinstance(body["model_aliases"], list)
    assert isinstance(body["mapping_generation"], str) and body["mapping_generation"]
    assert isinstance(body["oauth_fixed_lifetime"], bool)
    assert isinstance(body["errors"], list)


async def test_readyz_identity_stable_across_two_calls(gateway_client):
    first = await gateway_client.get("/readyz")
    second = await gateway_client.get("/readyz")

    assert first.status_code == second.status_code
    assert first.json() == second.json()
