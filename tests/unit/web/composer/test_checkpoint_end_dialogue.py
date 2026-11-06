"""END retains a live alias but freezes its original wire/provenance proof."""

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_CONTENT_PARTS_KEY,
    PROMPT_GUARD_TRUST_KEY,
    PromptGuardContentPart,
    PromptGuardTrust,
    detach_prompt_guard_evidence,
)
from elspeth.web.composer.continuation_checkpoint_end_locals import _BorrowedEndDialogue, _EndDialogueProof


def test_end_dialogue_keeps_guarded_messages_and_detaches_nested_wire_aliases():
    block = {"type": "text", "text": "Original instruction"}
    messages = [{"role": "system", "content": [block], PROMPT_GUARD_TRUST_KEY: PromptGuardTrust.INSTRUCTION}]
    original = _EndDialogueProof.capture(messages)
    borrowed = _BorrowedEndDialogue(messages)
    borrowed.require_unchanged()
    assert original.trusted_message_indexes == frozenset({0})
    assert original.messages_json == canonical_json(detach_prompt_guard_evidence(messages).messages)
    block["text"] = "Changed through original nested alias"
    assert "Changed through" not in original.messages_json
    with pytest.raises(AuditIntegrityError, match="dialogue or its owned provenance"):
        borrowed.require_unchanged()


def test_end_dialogue_detects_original_list_append():
    messages = [{"role": "user", "content": "Original request"}]
    borrowed = _BorrowedEndDialogue(messages)
    messages.append({"role": "assistant", "content": "Later reply"})
    with pytest.raises(AuditIntegrityError, match="dialogue or its owned provenance"):
        borrowed.require_unchanged()


def test_end_dialogue_detects_trust_removal_with_identical_wire():
    message = {"role": "system", "content": "Original instruction", PROMPT_GUARD_TRUST_KEY: PromptGuardTrust.INSTRUCTION}
    messages = [message]
    borrowed = _BorrowedEndDialogue(messages)
    wire = canonical_json(detach_prompt_guard_evidence(messages).messages)
    del message[PROMPT_GUARD_TRUST_KEY]
    assert canonical_json(detach_prompt_guard_evidence(messages).messages) == wire
    with pytest.raises(AuditIntegrityError, match="dialogue or its owned provenance"):
        borrowed.require_unchanged()


def test_end_dialogue_detects_partition_substitution_with_identical_wire():
    message = {
        "role": "user",
        "content": "Original request",
        PROMPT_GUARD_CONTENT_PARTS_KEY: (PromptGuardContentPart("Original request"),),
    }
    messages = [message]
    borrowed = _BorrowedEndDialogue(messages)
    wire = canonical_json(detach_prompt_guard_evidence(messages).messages)
    message[PROMPT_GUARD_CONTENT_PARTS_KEY] = (PromptGuardContentPart("Original "), PromptGuardContentPart("request"))
    assert canonical_json(detach_prompt_guard_evidence(messages).messages) == wire
    with pytest.raises(AuditIntegrityError, match="dialogue or its owned provenance"):
        borrowed.require_unchanged()
