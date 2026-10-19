"""Operator-owned Azure Prompt Shields authoring configuration."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from elspeth.core.url_validation import validate_credential_safe_https_url

AZURE_PROMPT_SHIELDS_API_VERSION = "2024-09-01"


class PromptGuardSettings(BaseModel):
    """A dedicated Content Safety endpoint, credential and total check timeout."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    adapter: Literal["azure_prompt_shields"] = "azure_prompt_shields"
    endpoint: str = Field(repr=False)
    subscription_key: SecretStr = Field(repr=False)
    timeout_seconds: float = Field(default=10.0, strict=True, gt=0, le=60, allow_inf_nan=False)

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        if any(character.isspace() or not character.isprintable() for character in value):
            raise ValueError("Prompt guard endpoint must not contain whitespace or control characters")
        safe_url = validate_credential_safe_https_url(value, field_name="prompt guard endpoint")
        parsed = urlsplit(safe_url)
        try:
            port = parsed.port
        except ValueError:
            raise ValueError("Prompt guard endpoint has an invalid port") from None
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("Prompt guard endpoint has an invalid port")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("Prompt guard endpoint must be an HTTPS origin without path, query or fragment")
        return safe_url.rstrip("/")

    @field_validator("subscription_key")
    @classmethod
    def validate_subscription_key(cls, value: SecretStr) -> SecretStr:
        text = value.get_secret_value()
        if (
            not 1 <= len(text) <= 1024
            or not text.isascii()
            or any(character.isspace() or not character.isprintable() for character in text)
        ):
            raise ValueError("Prompt guard subscription key must be bounded nonempty ASCII without whitespace")
        return value
