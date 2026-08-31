"""Shared provider tool-call boundary contracts."""

from typing import Final, TypeGuard

PROVIDER_TOOL_CALL_ID_MAX_LENGTH: Final[int] = 256


def is_valid_provider_replay_tool_call_id(value: object) -> TypeGuard[str]:
    """Return whether *value* is an admissible ephemeral replay ID.

    Provider IDs are opaque: LiteLLM may embed a Gemini thought signature in
    the ID, so replay paths must preserve IDs longer than our persisted-field
    limit. Persistence boundaries enforce their own length contract.
    """
    return type(value) is str and bool(value.strip())
