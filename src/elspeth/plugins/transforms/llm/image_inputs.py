"""Config-declared image inputs for LLM transforms: blob refs -> ImageParts.

Resolution mirrors textract_inline_analysis._read_document_bytes: every
row-data failure is a typed row-level TransformResult.error; payload-store
IntegrityError propagates (Tier-1). Bytes exist only in the returned
ImageParts — never in error dicts or logs.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import cast

from pydantic import BaseModel, ConfigDict, model_validator

from elspeth.contracts import TransformResult
from elspeth.contracts.binary_documents import BINARY_DOCUMENT_FORMAT_BY_MIME
from elspeth.contracts.chat_parts import IMAGE_FORMATS, ImageFormat, ImagePart
from elspeth.contracts.errors import FrameworkBugError, TransformErrorReason
from elspeth.contracts.payload_store import PayloadNotFoundError, PayloadStore
from elspeth.contracts.schema_contract import PipelineRow

_PAYLOAD_REF_PATTERN = re.compile(r"[0-9a-f]{64}")

# Includes the retained decoded bytes and their projected base64 wire form.
# This is deliberately not configurable by pipeline authors.
MAX_IMAGE_EXPANDED_BYTES = 32 * 1024 * 1024


class ImageInputConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    format: ImageFormat | None = None
    format_field: str | None = None
    required: bool = True

    @model_validator(mode="after")
    def _exactly_one_format_source(self) -> ImageInputConfig:
        if (self.format is None) == (self.format_field is None):
            raise ValueError("image input requires exactly one of 'format' or 'format_field'")
        for name in (self.field, self.format_field):
            if name is not None and not name.isidentifier():
                raise ValueError(f"image input field names must be identifiers, got {name!r}")
        return self


def _resolve_format(spec: ImageInputConfig, row: PipelineRow) -> ImageFormat | TransformResult:
    if spec.format is not None:
        return spec.format
    format_field = spec.format_field
    assert format_field is not None  # enforced by the config validator
    if format_field not in row or row[format_field] is None:
        missing_reason: TransformErrorReason = {"reason": "missing_field", "field": format_field}
        return TransformResult.error(missing_reason, retryable=False)
    mime = row[format_field]
    mapped = BINARY_DOCUMENT_FORMAT_BY_MIME[mime] if type(mime) is str and mime in BINARY_DOCUMENT_FORMAT_BY_MIME else None
    if mapped is None or mapped not in IMAGE_FORMATS:
        unmapped_reason: TransformErrorReason = {
            "reason": "invalid_input",
            "field": format_field,
            "error_type": "unmapped_image_mime",
        }
        return TransformResult.error(unmapped_reason, retryable=False)
    return cast(ImageFormat, mapped)


def _resolve_one_ref(
    spec: ImageInputConfig,
    ref: object,
    image_format: ImageFormat,
    *,
    payload_store: PayloadStore | None,
    max_image_bytes: int,
    list_index: int | None,
) -> ImagePart | TransformResult:
    if type(ref) is not str:
        non_string_reason: TransformErrorReason = {
            "reason": "invalid_input",
            "field": spec.field,
            "error_type": "non_string_ref",
        }
        if list_index is not None:
            non_string_reason["list_index"] = list_index
        return TransformResult.error(non_string_reason, retryable=False)

    if _PAYLOAD_REF_PATTERN.fullmatch(ref) is None:
        invalid_ref_reason: TransformErrorReason = {
            "reason": "invalid_input",
            "field": spec.field,
            "error_type": "invalid_payload_ref",
        }
        if list_index is not None:
            invalid_ref_reason["list_index"] = list_index
        return TransformResult.error(invalid_ref_reason, retryable=False)

    if payload_store is None:
        raise FrameworkBugError("image_inputs resolution requires a payload store")

    try:
        content = payload_store.retrieve(ref)
    except PayloadNotFoundError:
        not_found_reason: TransformErrorReason = {"reason": "blob_not_found", "field": spec.field, "blob_ref": ref}
        if list_index is not None:
            not_found_reason["list_index"] = list_index
        return TransformResult.error(not_found_reason, retryable=False)

    if not content:
        empty_reason: TransformErrorReason = {
            "reason": "invalid_input",
            "field": spec.field,
            "error_type": "empty_document",
        }
        if list_index is not None:
            empty_reason["list_index"] = list_index
        return TransformResult.error(empty_reason, retryable=False)

    if len(content) > max_image_bytes:
        too_large_reason: TransformErrorReason = {
            "reason": "blob_too_large",
            "field": spec.field,
            "max_blob_bytes": max_image_bytes,
            "actual": str(len(content)),
        }
        if list_index is not None:
            too_large_reason["list_index"] = list_index
        return TransformResult.error(too_large_reason, retryable=False)

    try:
        return ImagePart.from_bytes(format=image_format, data=content, blob_ref=ref)
    except ValueError:
        signature_reason: TransformErrorReason = {
            "reason": "invalid_input",
            "field": spec.field,
            "error_type": "image_signature_mismatch",
            "expected": image_format,
        }
        if list_index is not None:
            signature_reason["list_index"] = list_index
        return TransformResult.error(signature_reason, retryable=False)


def resolve_image_parts(
    row: PipelineRow,
    *,
    payload_store: PayloadStore | None,
    specs: Sequence[ImageInputConfig],
    max_image_bytes: int,
    max_images_per_call: int,
    expansion_factor: int = 1,
) -> tuple[ImagePart, ...] | TransformResult:
    if expansion_factor < 1:
        raise FrameworkBugError("image expansion factor must be positive")

    prepared: list[tuple[ImageInputConfig, ImageFormat, int | None, object]] = []

    # Validate the complete reference set before retrieving any payload. This
    # keeps an oversized row from allocating up to its author-selected limit
    # before the count guard fires.
    total_refs = 0
    for spec in specs:
        if spec.field not in row or row[spec.field] is None:
            if spec.required:
                missing_reason: TransformErrorReason = {"reason": "missing_field", "field": spec.field}
                return TransformResult.error(missing_reason, retryable=False)
            continue

        image_format = _resolve_format(spec, row)
        if isinstance(image_format, TransformResult):
            return image_format

        raw_value = row[spec.field]
        refs = enumerate(raw_value) if isinstance(raw_value, (list, tuple)) else [(None, raw_value)]
        for list_index, ref in refs:
            total_refs += 1
            if total_refs > max_images_per_call:
                too_many_reason: TransformErrorReason = {
                    "reason": "too_many_images",
                    "max_images": max_images_per_call,
                    "actual": str(total_refs),
                }
                return TransformResult.error(too_many_reason, retryable=False)
            prepared.append((spec, image_format, list_index, ref))

    parts: list[ImagePart] = []
    resolved_refs: dict[tuple[str, ImageFormat], ImagePart] = {}
    expanded_bytes = 0
    for spec, image_format, list_index, ref in prepared:
        cache_key = (ref, image_format) if type(ref) is str else None
        cached_part = resolved_refs.get(cache_key) if cache_key is not None else None
        if cached_part is None:
            resolution = _resolve_one_ref(
                spec,
                ref,
                image_format,
                payload_store=payload_store,
                max_image_bytes=max_image_bytes,
                list_index=list_index,
            )
            if isinstance(resolution, TransformResult):
                return resolution
            resolved = resolution
            if cache_key is not None:
                resolved_refs[cache_key] = resolved
        else:
            resolved = cached_part

        decoded_bytes = len(resolved.data)
        base64_bytes = 4 * ((decoded_bytes + 2) // 3)
        expanded_bytes += expansion_factor * (decoded_bytes + base64_bytes)
        if expanded_bytes > MAX_IMAGE_EXPANDED_BYTES:
            budget_reason: TransformErrorReason = {
                "reason": "invalid_input",
                "field": spec.field,
                "error_type": "image_payload_budget_exceeded",
                "max_image_bytes_total": MAX_IMAGE_EXPANDED_BYTES,
                "projected_image_bytes": expanded_bytes,
            }
            return TransformResult.error(budget_reason, retryable=False)
        parts.append(resolved)

    return tuple(parts)
