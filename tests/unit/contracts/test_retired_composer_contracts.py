"""Retired composer-mode contracts are absent from the public leaf modules."""

from elspeth.contracts import blobs, composer_llm_audit, errors


def test_guided_only_contracts_are_not_exported() -> None:
    assert not hasattr(blobs, "BlobGuidedOperationWriteFence")
    assert not hasattr(blobs, "BlobGuidedOperationFenceLostError")
    assert not hasattr(errors, "GuidedCustodyIntegrityError")
    assert not hasattr(composer_llm_audit, "ComposerChatTurn")
    assert not hasattr(composer_llm_audit, "ComposerChatTurnStatus")
    assert not hasattr(composer_llm_audit, "ComposerChatInitiator")
    assert not hasattr(composer_llm_audit, "ComposerChatTurnRecorder")
