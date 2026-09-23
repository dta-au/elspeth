"""Fail-closed SDK stand-in used while replay reconstructs audited AWS calls."""

from __future__ import annotations

from typing import Any

from elspeth.contracts.errors import AuditIntegrityError


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
