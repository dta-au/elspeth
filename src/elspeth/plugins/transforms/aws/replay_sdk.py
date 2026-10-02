"""Fail-closed SDK stand-in used while replay reconstructs audited AWS calls."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from threading import Lock
from typing import Any

from elspeth.contracts.call_data import RawCallPayload
from elspeth.contracts.errors import AuditIntegrityError


def require_replay_fields(
    retained: Mapping[str, Any] | None,
    *,
    fields: tuple[str, ...],
    source_call_id: str,
) -> RawCallPayload:
    """Turn archived call data into an owned payload with explicit required keys."""
    if retained is None or any(field not in retained for field in fields):
        raise AuditIntegrityError(f"Replay call {source_call_id} has incomplete response evidence")
    return RawCallPayload(retained)


class ReplayOnlySDK:
    """Never permits an AWS request, including one added after mode admission."""

    def head_bucket(self, **_kwargs: Any) -> object:
        raise AuditIntegrityError("S3 HeadBucket escaped the replay adapter")

    def start_document_analysis(self, **_kwargs: Any) -> object:
        raise AuditIntegrityError("Textract StartDocumentAnalysis escaped the replay adapter")

    def get_document_analysis(self, **_kwargs: Any) -> object:
        raise AuditIntegrityError("Textract GetDocumentAnalysis escaped the replay adapter")

    def analyze_document(self, **_kwargs: Any) -> object:
        raise AuditIntegrityError("Textract AnalyzeDocument escaped the replay adapter")

    def apply_guardrail(self, **_kwargs: Any) -> object:
        raise AuditIntegrityError("Bedrock ApplyGuardrail escaped the replay adapter")

    def close(self) -> None:
        return None


class DeferredAWSClient:
    """Construct an SDK client only after a mode adapter admits its call."""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._client: Any | None = None
        self._lock = Lock()

    def _ready(self) -> Any:
        with self._lock:
            if self._client is None:
                self._client = self._factory()
            return self._client

    def head_bucket(self, **kwargs: Any) -> object:
        return self._ready().head_bucket(**kwargs)

    def start_document_analysis(self, **kwargs: Any) -> object:
        return self._ready().start_document_analysis(**kwargs)

    def get_document_analysis(self, **kwargs: Any) -> object:
        return self._ready().get_document_analysis(**kwargs)

    def analyze_document(self, **kwargs: Any) -> object:
        return self._ready().analyze_document(**kwargs)

    def apply_guardrail(self, **kwargs: Any) -> object:
        return self._ready().apply_guardrail(**kwargs)

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None
