"""Exact ordinary and prescribed Review effect identities for one paid reply."""

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer.continuation import PrivateChunkRef


def end_effect_key(response: PrivateChunkRef) -> str:
    """Preserve the ordinary END review identity of one paid PRIMARY reply."""
    if type(response) is not PrivateChunkRef or response.codec != "composer.response.v1":
        raise AuditIntegrityError("END effect requires its actual paid PRIMARY response")
    return f"end:{response.digest}"


def semantic_end_effect_key(response: PrivateChunkRef, action_key: str) -> str:
    """Bind a prescribed Review to its original normalized action and reply.

    A later Review needs substantive new evidence to have a new action key.
    Another physical Advisor call alone cannot allocate another END effect.
    """
    end_effect_key(response)
    if type(action_key) is not str or len(action_key) != 64 or any(character not in "0123456789abcdef" for character in action_key):
        raise AuditIntegrityError("semantic END effect requires its original normalized action key")
    digest = stable_hash(
        {
            "schema": "composer.semantic-end-review-effect.v1",
            "primary_response_sha256": response.digest,
            "action_key": action_key,
        }
    )
    return f"end:review:{digest}"
