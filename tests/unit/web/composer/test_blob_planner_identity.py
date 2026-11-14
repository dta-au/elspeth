"""Planner facts use admitted selector identity, never malformed arguments."""

import pytest

from elspeth.core.canonical import canonical_json
from elspeth.web.composer.pipeline_planner import PlannerInformationManifest, _ParsedToolCall, planner_discovery_information_keys
from tests.fixtures.resource_identity import BLOB_UUID, UUID_SPELLINGS, uuid_spelling


@pytest.mark.parametrize("spelling", UUID_SPELLINGS)
@pytest.mark.parametrize(
    "tool,prefix,suffix",
    [
        ("get_blob_metadata", "blob.metadata:", None),
        ("inspect_source", "blob.inspection:", None),
        ("get_blob_content", "blob.content:", (BLOB_UUID, 0, None)),
        ("read_attachment", "blob.attachment:", (BLOB_UUID, None)),
    ],
)
def test_planner_fact_keys_collapse_equivalent_selectors(spelling, tool, prefix, suffix):
    arguments = {"blob_id": uuid_spelling(BLOB_UUID, spelling)}
    call = _ParsedToolCall(call_id="identity", name=tool, arguments=arguments, raw_arguments=canonical_json(arguments))
    expected = prefix + (BLOB_UUID if suffix is None else canonical_json(suffix))
    assert planner_discovery_information_keys(call) == (expected,)
    assert call.arguments["blob_id"] == arguments["blob_id"]


@pytest.mark.parametrize("tool", ["get_blob_metadata", "inspect_source", "get_blob_content", "read_attachment"])
@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"blob_id": None},
        {"blob_id": 7},
        {"blob_id": "bad"},
        {"blob_id": " " + BLOB_UUID},
        {"blob_id": BLOB_UUID + " "},
        {"blob_id": BLOB_UUID, "extra": True},
    ],
)
def test_invalid_blob_arguments_supply_no_planner_fact(tool, arguments):
    call = _ParsedToolCall(call_id="invalid", name=tool, arguments=arguments, raw_arguments=canonical_json(arguments))
    keys = planner_discovery_information_keys(call)
    assert keys == ()
    for available in (True, False):
        manifest = PlannerInformationManifest(supplied=frozenset()).with_result(keys, available=available)
        assert manifest.supplied == frozenset()
        assert manifest.unavailable == frozenset()


@pytest.mark.parametrize("tool", ["get_blob_metadata", "inspect_source", "get_blob_content", "read_attachment"])
def test_distinct_blob_ids_remain_distinct_planner_facts(tool):
    def keys(arguments):
        return planner_discovery_information_keys(
            _ParsedToolCall(call_id="distinct", name=tool, arguments=arguments, raw_arguments=canonical_json(arguments))
        )

    assert keys({"blob_id": BLOB_UUID}) != keys({"blob_id": "bcdefabc-defa-4bcd-8efa-bcdefabcdefa"})
    if tool in {"get_blob_content", "read_attachment"}:
        assert keys({"blob_id": BLOB_UUID}) != keys({"blob_id": BLOB_UUID, "expected_content_hash": "a" * 64})
    if tool == "get_blob_content":
        assert keys({"blob_id": BLOB_UUID}) != keys({"blob_id": BLOB_UUID, "offset_chars": 1})
