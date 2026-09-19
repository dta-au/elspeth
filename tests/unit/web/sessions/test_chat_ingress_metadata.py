"""Closed metadata shape for durable, content-free chat ingress evidence."""

from elspeth.web.sessions.service import _valid_chat_ingress_inputs_metadata


def test_chat_ingress_metadata_requires_exact_unique_durable_input_records() -> None:
    entry = {
        "message_id": "11111111-1111-4111-8111-111111111111",
        "text_sha256": "a" * 64,
        "foreign_compartment_ids": ["foreign-a", "foreign-b"],
    }

    assert _valid_chat_ingress_inputs_metadata([entry])
    assert not _valid_chat_ingress_inputs_metadata([entry, entry])
    assert not _valid_chat_ingress_inputs_metadata([{**entry, "content": "untrusted text"}])
    assert not _valid_chat_ingress_inputs_metadata([{**entry, "message_id": "not-a-message-id"}])
    assert not _valid_chat_ingress_inputs_metadata([{**entry, "text_sha256": "A" * 64}])
    assert not _valid_chat_ingress_inputs_metadata([{**entry, "foreign_compartment_ids": ["foreign-b", "foreign-a"]}])
