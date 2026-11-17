"""Closed, content-free discovery response for inline prompt targets."""

from __future__ import annotations

from types import MappingProxyType
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.redaction import NodeOptionSubstringTarget, PromptPartSubstringTarget
from elspeth.web.composer.response_contracts import SelectedResponseContract


class PromptTargetMetadata(BaseModel):
    target: Annotated[NodeOptionSubstringTarget | PromptPartSubstringTarget, Field(discriminator="kind")]
    length_chars: int = Field(ge=0)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class PromptTargetsResponse(BaseModel):
    state_id: str | None
    targets: tuple[PromptTargetMetadata, ...]

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _parse(value: object) -> PromptTargetsResponse:
    if type(value) not in (dict, MappingProxyType, PromptTargetsResponse):
        raise FrameworkBugError("Invalid prompt target discovery response")
    try:
        if type(value) is MappingProxyType:
            thawed = deep_thaw(value)
            if type(thawed.get("targets")) is list:
                thawed["targets"] = tuple(thawed["targets"])
            value = thawed
        return PromptTargetsResponse.model_validate(value)
    except ValidationError:
        raise FrameworkBugError("Invalid prompt target discovery response") from None


PROMPT_TARGETS_RESPONSE_CONTRACT = SelectedResponseContract(
    parse=_parse,
    encode=lambda response: response.model_dump(mode="json"),
)
