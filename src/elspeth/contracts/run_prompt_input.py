"""Owned identities of file prompts admitted to a run."""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class RunPromptInputEvidence:
    node_id: str
    field_path: str
    blob_id: str
    content_sha256: str
    review_anchor: str
    effective_artifact_hash: str | None

    def __post_init__(self) -> None:
        for name, value in (("node_id", self.node_id), ("field_path", self.field_path), ("blob_id", self.blob_id)):
            if type(value) is not str or not value:
                raise ValueError(f"{name} must be a non-empty string")
        try:
            if str(UUID(self.blob_id)) != self.blob_id:
                raise ValueError
        except ValueError as exc:
            raise ValueError("blob_id must be a canonical UUID") from exc
        for name, value in (("content_sha256", self.content_sha256), ("review_anchor", self.review_anchor)):
            if type(value) is not str or _HEX64.fullmatch(value) is None:
                raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
        if self.effective_artifact_hash is not None and (
            type(self.effective_artifact_hash) is not str or _HEX64.fullmatch(self.effective_artifact_hash) is None
        ):
            raise ValueError("effective_artifact_hash must be a lowercase SHA-256 hex digest")
