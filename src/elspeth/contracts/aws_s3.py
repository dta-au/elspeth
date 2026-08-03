"""Owned cross-layer authority for profile-safe S3 audit evidence."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class S3ProfiledAuditIdentity:
    """Server-owned identity for the safe evidence projection of one profiled read."""

    profile_alias: str
    relative_key: str

    def __post_init__(self) -> None:
        if type(self.profile_alias) is not str or not self.profile_alias:
            raise ValueError("profile_alias must be an exact non-empty string")
        if type(self.relative_key) is not str or not self.relative_key:
            raise ValueError("relative_key must be an exact non-empty string")
