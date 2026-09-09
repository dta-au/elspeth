"""Configuration models for pipeline dependencies and commencement gates."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)
from pydantic_core import InitErrorDetails, PydanticCustomError

from elspeth.contracts.freeze import deep_freeze, deep_thaw
from elspeth.core.commencement_gate_expression import validate_commencement_gate_condition
from elspeth.core.expression_parser import ExpressionSecurityError, ExpressionSyntaxError

_REDACTED_CONDITION_INPUT = "<redacted-commencement-gate-condition>"


def _condition_validation_error(*, code: str, message: str) -> ValidationError:
    """Build an owned Pydantic error whose input cannot reveal source text."""
    return ValidationError.from_exception_data(
        title="CommencementGateConfig",
        line_errors=[
            InitErrorDetails(
                type=PydanticCustomError(code, message),
                loc=(),
                input=_REDACTED_CONDITION_INPUT,
            )
        ],
    )


def _validate_non_blank(value: str, field_name: str) -> str:
    """Strip and reject blank-only strings at the config boundary."""
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} must not be blank (whitespace-only)")
    return stripped


class DependencyConfig(BaseModel):
    """Declares a pipeline that must run before this one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, description="Unique label for this dependency")
    settings: str = Field(min_length=1, description="Path to dependency pipeline settings file")

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return _validate_non_blank(v, "name")

    @field_validator("settings")
    @classmethod
    def _strip_settings(cls, v: str) -> str:
        return _validate_non_blank(v, "settings")


class CommencementGateConfig(BaseModel):
    """Declares a go/no-go condition evaluated before the pipeline starts."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    name: str = Field(min_length=1, description="Unique label for this gate")
    condition: str = Field(min_length=1, description="Expression evaluated against pre-flight context")

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return _validate_non_blank(v, "name")

    @field_validator("condition")
    @classmethod
    def _validate_condition(cls, v: str) -> str:
        """Parse the condition expression at config time.

        ExpressionParser raises ExpressionSyntaxError or ExpressionSecurityError
        on invalid input — surfaced immediately instead of at gate evaluation time.
        """
        stripped = _validate_non_blank(v, "condition")
        try:
            validate_commencement_gate_condition(stripped)
        except ExpressionSyntaxError:
            raise _condition_validation_error(
                code="commencement_gate_condition_syntax",
                message="condition has invalid syntax; provide one valid expression using the supported commencement gate grammar",
            ) from None
        except ExpressionSecurityError:
            raise _condition_validation_error(
                code="commencement_gate_condition_security",
                message=(
                    "condition uses unsupported or unsafe syntax; use only pre-flight lookups, comparisons, boolean operators, "
                    "literals, and supported safe builtins"
                ),
            ) from None
        return stripped


# Supported probe providers — intentionally small, system-owned registry.
SupportedProbeProvider = Literal["chroma"]


class CollectionProbeConfig(BaseModel):
    """Declares a vector store collection to probe before gate evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    collection: str = Field(min_length=1, description="Collection name to probe")
    provider: SupportedProbeProvider = Field(description="Provider type (e.g., 'chroma')")
    provider_config: Mapping[str, Any] = Field(
        default_factory=dict,
        description="Provider-specific connection config",
    )

    @field_validator("collection")
    @classmethod
    def _strip_collection(cls, v: str) -> str:
        return _validate_non_blank(v, "collection")

    @model_validator(mode="after")
    def _freeze_provider_config(self) -> CollectionProbeConfig:
        """Deep-freeze provider_config contents — Pydantic frozen=True prevents reassignment but not mutation of mutable containers."""
        object.__setattr__(self, "provider_config", deep_freeze(self.provider_config))
        return self

    @field_serializer("provider_config")
    @classmethod
    def _serialize_provider_config(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        """Thaw MappingProxyType back to dict for Pydantic JSON serialization."""
        result = deep_thaw(value)
        if type(result) is not dict:
            raise TypeError(
                f"deep_thaw(provider_config) returned {type(result).__name__}, expected dict. Input type was {type(value).__name__}."
            )
        return result
