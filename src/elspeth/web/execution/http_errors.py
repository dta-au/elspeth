"""Shared HTTP projections for execution admission errors."""

from fastapi import HTTPException

from elspeth.web.execution.errors import PipelineValidationError


def pipeline_validation_http_exception(exc: PipelineValidationError) -> HTTPException:
    """Preserve the /validate error shape at every execution HTTP entry."""
    return HTTPException(
        status_code=422,
        detail={
            "error_type": "pipeline_validation_failure",
            "detail": str(exc),
            "kind": "pipeline_validation_failure",
            "errors": [
                {
                    "component_id": err.component_id,
                    "component_type": err.component_type,
                    "message": err.message,
                    "suggestion": err.suggestion,
                    "error_code": err.error_code,
                }
                for err in exc.errors
            ],
        },
    )
