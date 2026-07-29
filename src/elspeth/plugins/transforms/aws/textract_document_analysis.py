"""Asynchronous Amazon Textract document-analysis transform."""

from __future__ import annotations

import re
from typing import ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from elspeth.plugins.infrastructure.config_base import TransformDataConfig

FeatureType = Literal["TABLES", "FORMS", "QUERIES", "SIGNATURES", "LAYOUT"]
AuthMode = Literal["default_chain", "secret_refs"]

_FEATURE_TYPES = frozenset({"TABLES", "FORMS", "QUERIES", "SIGNATURES", "LAYOUT"})
_REGION_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_QUERY_TEXT_PATTERN = re.compile(r"^[a-zA-Z0-9\s!\"#$%'&()*+,\-./:;=?@[\\\]^_`{|}~><]+$")
_QUERY_PAGE_PATTERN = re.compile(r"^[0-9*\-]+$")
_FACET_NAMES = ("pages", "tables", "forms", "queries", "signatures", "layout")


def _require_non_whitespace(value: str | None, *, field_name: str) -> str | None:
    if value is not None and not value.strip():
        raise ValueError(f"{field_name} must not be empty or whitespace-only")
    return value


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


class AWSTextractDocumentAnalysisConfig(TransformDataConfig):
    """Validated public configuration for asynchronous S3 document analysis."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        **TransformDataConfig.model_config,
        hide_input_in_errors=True,
    )

    region: str = Field(min_length=1, max_length=64)
    auth_mode: AuthMode = "default_chain"
    aws_access_key_id: str | None = Field(default=None, min_length=1, max_length=256, repr=False)
    aws_secret_access_key: str | None = Field(default=None, min_length=1, max_length=4096, repr=False)
    aws_session_token: str | None = Field(default=None, min_length=1, max_length=16384, repr=False)

    bucket_field: str = Field(min_length=1, max_length=256)
    key_field: str = Field(min_length=1, max_length=256)
    version_field: str | None = Field(default=None, min_length=1, max_length=256)
    feature_types: list[FeatureType] = Field(min_length=1, max_length=5)
    queries: list[TextractQueryConfig] = Field(default_factory=list, max_length=30)

    text_field: str | None = None
    page_count_field: str | None = None
    metadata_field: str | None = None
    extract: TextractExtractFields = Field(default_factory=TextractExtractFields)
    result_field: str | None = None

    poll_interval_seconds: float = Field(default=1.0, gt=0)
    poll_backoff_multiplier: float = Field(default=1.5, ge=1)
    poll_max_interval_seconds: float = Field(default=10.0, gt=0)
    poll_timeout_seconds: float = Field(default=3600.0, gt=0)
    batch_wait_timeout_seconds: float = Field(default=3900.0, gt=0)
    max_result_pages: int = Field(default=1000, gt=0)
    max_blocks: int = Field(default=200_000, gt=0)
    max_result_bytes: int = Field(default=50_000_000, gt=0)

    @field_validator("region")
    @classmethod
    def _region(cls, value: str) -> str:
        if _REGION_PATTERN.fullmatch(value) is None:
            raise ValueError("region must be a lowercase hyphen-separated AWS region identifier")
        return value

    @field_validator("bucket_field", "key_field", "version_field", "text_field", "page_count_field", "metadata_field", "result_field")
    @classmethod
    def _field_name(cls, value: str | None, info: object) -> str | None:
        field_name = getattr(info, "field_name", "field")
        return _require_non_whitespace(value, field_name=field_name)

    @field_validator("feature_types")
    @classmethod
    def _features(cls, value: list[FeatureType]) -> list[FeatureType]:
        if any(feature not in _FEATURE_TYPES for feature in value):
            raise ValueError(f"feature_types must use only {sorted(_FEATURE_TYPES)}")
        if len(set(value)) != len(value):
            raise ValueError("feature_types must not contain duplicates")
        return value

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        for facet_name in _FACET_NAMES:
            _require_non_whitespace(getattr(self.extract, facet_name), field_name=f"extract.{facet_name}")

        has_queries = bool(self.queries)
        has_query_feature = "QUERIES" in self.feature_types
        if has_queries != has_query_feature:
            raise ValueError("queries and the QUERIES feature must be configured together")

        access_present = self.aws_access_key_id is not None
        secret_present = self.aws_secret_access_key is not None
        session_present = self.aws_session_token is not None
        if self.auth_mode == "default_chain":
            if access_present or secret_present or session_present:
                raise ValueError("explicit credentials are forbidden in default_chain auth mode")
        elif access_present != secret_present:
            raise ValueError("aws_access_key_id and aws_secret_access_key are required together as a credential pair")
        elif not access_present:
            if session_present:
                raise ValueError("aws_session_token requires the access and secret credential pair")
            raise ValueError("aws_access_key_id and aws_secret_access_key are required together in secret_refs auth mode")

        if self.poll_max_interval_seconds < self.poll_interval_seconds:
            raise ValueError("poll_max_interval_seconds must be >= poll_interval_seconds")

        names = self.all_output_field_names()
        if not names:
            raise ValueError(
                "At least one output target must be configured: text_field, page_count_field, metadata_field, result_field, or extract"
            )
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"Duplicate output field names: {duplicates}. Each output field must be unique")
        return self

    def configured_output_fields(self) -> dict[str, str]:
        """Map configured normalized facet names to their output-row fields."""
        return {facet_name: field_name for facet_name in _FACET_NAMES if (field_name := getattr(self.extract, facet_name)) is not None}

    def all_output_field_names(self) -> list[str]:
        """Return every configured output row field in stable projection order."""
        names = [name for name in (self.text_field, self.page_count_field, self.metadata_field, self.result_field) if name is not None]
        names.extend(self.configured_output_fields().values())
        return names

    @property
    def declared_input_fields(self) -> frozenset[str]:
        fields = {self.bucket_field, self.key_field}
        if self.version_field is not None:
            fields.add(self.version_field)
        return super().declared_input_fields | frozenset(fields)
