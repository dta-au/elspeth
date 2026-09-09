"""Configuration vocabulary shared by the Amazon Textract transforms.

The asynchronous (S3 document) and synchronous (inline payload-store
bytes) Textract transforms accept the same feature vocabulary, query
shapes, facet mappings, and AWS credential contract. One definition here
keeps the two public configuration surfaces from drifting; each transform
still owns its operation-specific fields and bounds.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

FeatureType = Literal["TABLES", "FORMS", "QUERIES", "SIGNATURES", "LAYOUT"]
AuthMode = Literal["default_chain", "secret_refs"]

FEATURE_TYPES = frozenset({"TABLES", "FORMS", "QUERIES", "SIGNATURES", "LAYOUT"})
FACET_NAMES = ("pages", "tables", "forms", "queries", "signatures", "layout")

_QUERY_TEXT_PATTERN = re.compile(r"^[a-zA-Z0-9\s!\"#$%'&()*+,\-./:;=?@[\\\]^_`{|}~><]+$")
_QUERY_PAGE_PATTERN = re.compile(r"^[0-9*\-]+$")


def require_non_whitespace(value: str | None, *, field_name: str) -> str | None:
    """Reject empty or whitespace-only optional string options."""
    if value is not None and not value.strip():
        raise ValueError(f"{field_name} must not be empty or whitespace-only")
    return value


def validate_textract_credential_fields(
    *,
    auth_mode: AuthMode,
    aws_access_key_id: str | None,
    aws_secret_access_key: str | None,
    aws_session_token: str | None,
) -> None:
    """Enforce the cross-field AWS credential contract shared by both transforms.

    ``default_chain`` forbids every explicit credential; ``secret_refs``
    requires the access/secret pair and admits the session token only
    alongside it. Raises ``ValueError`` with a field-precise message.
    """
    access_present = aws_access_key_id is not None
    secret_present = aws_secret_access_key is not None
    session_present = aws_session_token is not None
    if auth_mode == "default_chain":
        if access_present or secret_present or session_present:
            raise ValueError("explicit credentials are forbidden in default_chain auth mode")
    elif access_present != secret_present:
        raise ValueError("aws_access_key_id and aws_secret_access_key are required together as a credential pair")
    elif not access_present:
        if session_present:
            raise ValueError("aws_session_token requires the access and secret credential pair")
        raise ValueError("aws_access_key_id and aws_secret_access_key are required together in secret_refs auth mode")


class TextractQueryConfig(BaseModel):
    """One Textract query and its optional page selection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=200)
    alias: str | None = Field(default=None, min_length=1, max_length=200)
    pages: list[str] = Field(default_factory=list)

    @field_validator("text", "alias")
    @classmethod
    def _provider_text(cls, value: str | None) -> str | None:
        if value is not None and _QUERY_TEXT_PATTERN.fullmatch(value) is None:
            raise ValueError("value contains characters unsupported by Amazon Textract queries")
        return value

    @field_validator("pages")
    @classmethod
    def _page_selectors(cls, selectors: list[str]) -> list[str]:
        if len(set(selectors)) != len(selectors):
            raise ValueError("pages must not contain duplicate selectors")
        if "*" in selectors and selectors != ["*"]:
            raise ValueError("pages selector '*' must be the only selector")
        for selector in selectors:
            if not 1 <= len(selector) <= 9 or _QUERY_PAGE_PATTERN.fullmatch(selector) is None:
                raise ValueError("pages selectors must be 1-9 characters matching ^[0-9*-]+$")
            if selector == "*":
                continue
            parts = selector.split("-")
            if len(parts) == 1:
                if not parts[0].isdigit() or int(parts[0]) <= 0:
                    raise ValueError("pages selectors must use positive page numbers")
                continue
            if len(parts) != 2 or not parts[0].isdigit() or int(parts[0]) <= 0:
                raise ValueError("pages ranges must start with a positive page number")
            start = int(parts[0])
            end_text = parts[1]
            if end_text == "*":
                continue
            if not end_text.isdigit() or int(end_text) <= 0 or int(end_text) < start:
                raise ValueError("pages ranges must have positive, ordered endpoints")
        return selectors


class TextractExtractFields(BaseModel):
    """Optional normalized facet-to-row-field mappings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pages: str | None = None
    tables: str | None = None
    forms: str | None = None
    queries: str | None = None
    signatures: str | None = None
    layout: str | None = None

    def facet_items(self) -> tuple[tuple[str, str | None], ...]:
        """Facet name/output-field pairs in ``FACET_NAMES`` order via direct access.

        This model is ELSPETH-owned, so consumers enumerate its fields
        nominally (ADR-032) instead of probing with ``getattr``.
        """
        return (
            ("pages", self.pages),
            ("tables", self.tables),
            ("forms", self.forms),
            ("queries", self.queries),
            ("signatures", self.signatures),
            ("layout", self.layout),
        )
