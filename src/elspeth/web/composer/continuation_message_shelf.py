from __future__ import annotations

import json
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_CONTENT_PARTS_KEY,
    PROMPT_GUARD_THINKING_PARTS_KEY,
    PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY,
    PROMPT_GUARD_TRUST_KEY,
)
from elspeth.web.composer.continuation import ContinuationValue
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    encode_private_chunk,
    owned_json,
    verify_private_chunk,
)
from elspeth.web.composer.continuation_prompt_recipes import ContinuationMessageRecipe


def encode_transcript_object_json(value: object) -> str:
    admitted = owned_json(value)
    if type(admitted) is not dict:
        raise AuditIntegrityError("transcript shelf requires an owned JSON object")
    # Preserve the original JCS numeric domain without retaining its sorted keys.
    canonical_json(admitted)
    return json.dumps(admitted, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class RetainedTranscriptHistory(ContinuationValue):
    session_id: str
    actor_user_id: str
    request_id: str
    request_hash: str
    events_json: str


class RetainedObservedMessage(ContinuationValue):
    message_index: int = Field(ge=0)
    event_json: str
    pending_adoption: bool


class MessageShelfSnapshot(ContinuationValue):
    schema_version: Literal["composer.transcript.v1"] = "composer.transcript.v1"
    request: str
    history: RetainedTranscriptHistory | None
    messages_json: tuple[str, ...]
    current_indexes: tuple[int, ...]
    excluded_indexes: tuple[int, ...]
    observed: tuple[RetainedObservedMessage, ...]
    producer_recipes: tuple[ContinuationMessageRecipe | None, ...] = ()

    @model_validator(mode="after")
    def require_owned_links(self) -> MessageShelfSnapshot:
        if self.producer_recipes and len(self.producer_recipes) != len(self.messages_json):
            raise ValueError("transcript shelf lost its complete producer positions")
        indexes = (
            *self.current_indexes,
            *self.excluded_indexes,
            *(entry.message_index for entry in self.observed),
        )
        if any(type(index) is not int or not 0 <= index < len(self.messages_json) for index in indexes):
            raise ValueError("transcript shelf has an out-of-range alias")
        for text in (*self.messages_json, *(entry.event_json for entry in self.observed)):
            value, error = parse_json_strict(text)
            if error is not None or type(value) is not dict or encode_transcript_object_json(value) != text:
                raise ValueError("transcript shelf contains a noncompact or invalid ordered object")
        for text in self.messages_json:
            value, _ = parse_json_strict(text)
            if not isinstance(value, dict):
                raise ValueError("transcript shelf message is not an object")
            if frozenset(
                {
                    PROMPT_GUARD_TRUST_KEY,
                    PROMPT_GUARD_CONTENT_PARTS_KEY,
                    PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY,
                    PROMPT_GUARD_THINKING_PARTS_KEY,
                }
            ).intersection(value):
                raise ValueError("plain transcript shelf cannot restore guard provenance")
        if self.history is not None:
            value, error = parse_json_strict(self.history.events_json)
            if error is not None or type(value) is not list:
                raise ValueError("transcript shelf history is not a strict event sequence")
        return self


def encode_message_shelf(snapshot: MessageShelfSnapshot) -> PrivateChunk:
    if type(snapshot) is not MessageShelfSnapshot:
        raise TypeError("transcript shelf requires its owned snapshot")
    return encode_private_chunk(
        codec="composer.transcript.v1",
        canonical=canonical_json(snapshot.model_dump(mode="json")),
    )


def decode_message_shelf(chunk: PrivateChunk) -> MessageShelfSnapshot:
    if type(chunk) is not PrivateChunk or chunk.reference.codec != "composer.transcript.v1":
        raise AuditIntegrityError("transcript shelf has the wrong codec")
    verify_private_chunk(chunk.reference, chunk.canonical)
    try:
        return MessageShelfSnapshot.model_validate_json(chunk.canonical)
    except ValidationError as exc:
        raise AuditIntegrityError("transcript shelf violates its owned codec") from exc
