from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
from typing import Any

from elspeth.contracts.hashing import canonical_json


def continuation_provider_input_json(kwargs: Mapping[str, Any]) -> str:
    """The complete actual identity payload, excluding only transport secrets/timeout."""
    options = {key: value for key, value in kwargs.items() if key not in ("messages", "tools", "api_key", "extra_headers", "timeout")}
    return canonical_json(
        {
            "schema": "composer.provider-input.v1",
            "messages": kwargs["messages"],
            "tools_present": "tools" in kwargs,
            "tools": kwargs["tools"] if "tools" in kwargs else None,
            "options": options,
        }
    )


def continuation_provider_input_hash(kwargs: Mapping[str, Any]) -> str:
    # This is the existing contracts.stable_hash algorithm over the one
    # canonical descriptor. No second soft owned descriptor escapes a parser.
    return sha256(continuation_provider_input_json(kwargs).encode("utf-8")).hexdigest()
