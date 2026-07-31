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
_EXPECTED_ADAPTER_KEYS = {"name", "version", "adapter_api_major", "fingerprint", "validates_model_targets"}


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


async def test_readyz_reports_the_adapter_validates_its_model_targets(gateway_client):
    """Qualification requirement, not merely a runtime nicety.

    The gateway core cannot tell whether a configured model target is one the
    adapter can consume -- the mapping value is opaque to it. Only the adapter
    knows, via the optional ``sdk.protocol.ModelTargetValidator`` hook. The
    runtime treats that hook as optional so an adapter built against this same
    adapter API major before it existed keeps working; the conformance kit is
    where it becomes mandatory. An adapter that does not implement it cannot
    have its targets checked at readiness, so a deployment of it can pass
    admission and then fail every completion -- which is exactly what image
    qualification exists to catch. Asserted in both modes: the reference
    adapter implements it, and so must any derived image's.
    """
    response = await gateway_client.get("/readyz")

    assert response.status_code in (200, 503)
    assert response.json()["adapter"]["validates_model_targets"] is True


async def test_readyz_identity_stable_across_two_calls(gateway_client):
    first = await gateway_client.get("/readyz")
    second = await gateway_client.get("/readyz")

    assert first.status_code == second.status_code
    assert first.json() == second.json()
