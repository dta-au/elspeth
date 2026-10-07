"""Canonical request semantics and strict terminal replay hashing."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash

type SessionOperationRequestSchema = Literal["session-operation-receipt-request.v1", "composer-operation-request.v1"]


def session_operation_request_hash(*, schema: SessionOperationRequestSchema, session_id: UUID, kind: str, request: BaseModel) -> str:
    """Bind all semantic fields, including defaulted and explicitly null bases."""
    config = type(request).model_config
    if config.get("strict") is not True or config.get("extra") != "forbid":
        raise AuditIntegrityError("Operation receipt requires a strict, extra-forbid request DTO")
    if "operation_id" not in type(request).model_fields:
        raise AuditIntegrityError("Operation receipt request DTO is missing operation_id")
    normalized = request.model_dump(mode="json", exclude={"operation_id"}, exclude_unset=False, exclude_defaults=False, exclude_none=False)
    return stable_hash({"schema": schema, "session_id": str(session_id), "kind": kind, "request": normalized})


def strict_response_hash(response: BaseModel) -> str:
    """Hash the strict response domain that terminal replay must reproduce."""
    config = type(response).model_config
    if config.get("strict") is not True or config.get("extra") != "forbid":
        raise AuditIntegrityError("Operation receipt replay requires a strict, extra-forbid response DTO")
    strict_response = type(response).model_validate(response.model_dump(mode="python"), strict=True)
    return stable_hash(strict_response.model_dump(mode="json"))
