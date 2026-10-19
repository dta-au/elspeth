"""Configuration-owned checks over an LLM transform's actual input or result.

Checks never widen template visibility or alter generated values. Input checks
can refuse row requests; result checks refuse completed output before release.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, Field, PrivateAttr, model_validator

from elspeth.contracts.errors import TransformErrorReason
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.core.expression_parser import (
    ExpressionEvaluationError,
    ExpressionParser,
    ExpressionSecurityError,
    ExpressionSyntaxError,
)


class ResultCheck(BaseModel):
    """One bounded, named row assertion, compiled before provider startup."""

    model_config = {"extra": "forbid", "frozen": True}

    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$", strict=True)
    expression: str = Field(min_length=1, max_length=4096, pattern=r"\S", strict=True)
    _parser: ExpressionParser = PrivateAttr()

    @model_validator(mode="after")
    def _compile(self) -> Self:
        try:
            self._parser = ExpressionParser(self.expression)
        except (ExpressionSyntaxError, ExpressionSecurityError) as exc:
            raise ValueError(f"Invalid result check expression: {exc}") from exc
        return self

    def failure_for(self, row: PipelineRow, *, phase: Literal["input", "result"] = "result") -> TransformErrorReason | None:
        """Fail closed on row-data errors or non-Boolean values, without echoing data."""
        try:
            value = self._parser.evaluate(row)
        except ExpressionEvaluationError:
            return {"reason": "validation_failed", "operation": self.name, "error_type": f"{phase}_check_evaluation_error"}
        if type(value) is not bool:
            return {"reason": "validation_failed", "operation": self.name, "error_type": f"{phase}_check_not_boolean"}
        if not value:
            return {"reason": "validation_failed", "operation": self.name, "error_type": f"{phase}_check_false"}
        return None
