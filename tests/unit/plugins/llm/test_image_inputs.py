"""Tests for image_inputs: config-declared blob-ref columns -> ImageParts.

FakePayloadStore mirrors tests/unit/plugins/transforms/aws/test_textract_inline_analysis.py.
"""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from elspeth.contracts.chat_parts import ImagePart
from elspeth.contracts.payload_store import IntegrityError, PayloadNotFoundError
from elspeth.contracts.results import TransformResult
from elspeth.plugins.transforms.llm.image_inputs import ImageInputConfig, resolve_image_parts
from elspeth.testing import make_pipeline_row
from tests.unit.contracts.test_chat_parts import JPEG_BYTES, PNG_BYTES

PNG_SHA256 = hashlib.sha256(PNG_BYTES).hexdigest()
JPEG_SHA256 = hashlib.sha256(JPEG_BYTES).hexdigest()


class FakePayloadStore:
    def __init__(self, contents: dict[str, bytes] | None = None, *, integrity_error: bool = False) -> None:
        self.contents = {PNG_SHA256: PNG_BYTES} if contents is None else contents
        self.integrity_error = integrity_error
        self.retrieve_calls: list[str] = []

    def store(self, content: bytes) -> str:
        raise AssertionError("resolve_image_parts must never store payloads")

    def retrieve(self, content_hash: str) -> bytes:
        self.retrieve_calls.append(content_hash)
        if self.integrity_error:
            raise IntegrityError("payload integrity check failed")
        try:
            return self.contents[content_hash]
        except KeyError:
            raise PayloadNotFoundError(content_hash) from None

    def exists(self, content_hash: str) -> bool:
        return content_hash in self.contents

    def delete(self, content_hash: str) -> bool:
        raise AssertionError("resolve_image_parts must never delete payloads")


def _resolve(row, specs, *, store=None, max_image_bytes=10_000_000, max_images_per_call=20):
    return resolve_image_parts(
        row,
        payload_store=store if store is not None else FakePayloadStore(),
        specs=specs,
        max_image_bytes=max_image_bytes,
        max_images_per_call=max_images_per_call,
    )


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestImageInputConfigValidation:
    def test_scalar_format_ok(self) -> None:
        cfg = ImageInputConfig(field="picture", format="png")
        assert cfg.format == "png"
        assert cfg.format_field is None

    def test_format_field_ok(self) -> None:
        cfg = ImageInputConfig(field="picture", format_field="picture_mime")
        assert cfg.format is None
        assert cfg.format_field == "picture_mime"

    def test_both_format_and_format_field_rejected(self) -> None:
        with pytest.raises(ValidationError, match="exactly one of"):
            ImageInputConfig(field="picture", format="png", format_field="picture_mime")

    def test_neither_format_nor_format_field_rejected(self) -> None:
        with pytest.raises(ValidationError, match="exactly one of"):
            ImageInputConfig(field="picture")

    def test_non_identifier_field_rejected(self) -> None:
        with pytest.raises(ValidationError, match="identifiers"):
            ImageInputConfig(field="not a field", format="png")

    def test_non_identifier_format_field_rejected(self) -> None:
        with pytest.raises(ValidationError, match="identifiers"):
            ImageInputConfig(field="picture", format_field="not a field")


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestHappyPaths:
    def test_scalar_literal_format(self) -> None:
        row = make_pipeline_row({"picture": PNG_SHA256})
        spec = ImageInputConfig(field="picture", format="png")
        result = _resolve(row, [spec])
        assert isinstance(result, tuple)
        assert len(result) == 1
        part = result[0]
        assert isinstance(part, ImagePart)
        assert part.format == "png"
        assert part.data == PNG_BYTES
        assert part.blob_ref == PNG_SHA256

    def test_format_field_mapping(self) -> None:
        row = make_pipeline_row({"picture": PNG_SHA256, "picture_mime": "image/png"})
        spec = ImageInputConfig(field="picture", format_field="picture_mime")
        result = _resolve(row, [spec])
        assert isinstance(result, tuple)
        assert len(result) == 1
        assert result[0].format == "png"

    def test_list_valued_column_preserves_order(self) -> None:
        second_sha = hashlib.sha256(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20).hexdigest()
        second_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
        store = FakePayloadStore({PNG_SHA256: PNG_BYTES, second_sha: second_bytes})
        row = make_pipeline_row({"pictures": [PNG_SHA256, second_sha]})
        spec = ImageInputConfig(field="pictures", format="png")
        result = _resolve(row, [spec], store=store)
        assert isinstance(result, tuple)
        assert [p.blob_ref for p in result] == [PNG_SHA256, second_sha]

    def test_two_specs_concatenate_in_spec_order(self) -> None:
        store = FakePayloadStore({PNG_SHA256: PNG_BYTES, JPEG_SHA256: JPEG_BYTES})
        row = make_pipeline_row({"picture_a": JPEG_SHA256, "picture_b": PNG_SHA256})
        spec_a = ImageInputConfig(field="picture_a", format="jpeg")
        spec_b = ImageInputConfig(field="picture_b", format="png")
        result = _resolve(row, [spec_a, spec_b], store=store)
        assert isinstance(result, tuple)
        assert [p.blob_ref for p in result] == [JPEG_SHA256, PNG_SHA256]
        assert [p.format for p in result] == ["jpeg", "png"]


# ---------------------------------------------------------------------------
# required=False semantics
# ---------------------------------------------------------------------------


class TestRequiredFalse:
    def test_absent_column_contributes_zero_parts(self) -> None:
        row = make_pipeline_row({"other": "x"})
        spec = ImageInputConfig(field="picture", format="png", required=False)
        result = _resolve(row, [spec])
        assert result == ()

    def test_none_column_contributes_zero_parts(self) -> None:
        row = make_pipeline_row({"picture": None})
        spec = ImageInputConfig(field="picture", format="png", required=False)
        result = _resolve(row, [spec])
        assert result == ()

    def test_present_bad_ref_still_errors(self) -> None:
        row = make_pipeline_row({"picture": "not-a-valid-ref"})
        spec = ImageInputConfig(field="picture", format="png", required=False)
        result = _resolve(row, [spec])
        assert isinstance(result, TransformResult)
        assert result.status == "error"
        assert result.reason is not None
        assert result.reason["reason"] == "invalid_input"
        assert result.reason["error_type"] == "invalid_payload_ref"


# ---------------------------------------------------------------------------
# required=True missing_field
# ---------------------------------------------------------------------------


class TestRequiredTrueMissing:
    def test_absent_column_errors(self) -> None:
        row = make_pipeline_row({"other": "x"})
        spec = ImageInputConfig(field="picture", format="png")
        result = _resolve(row, [spec])
        assert isinstance(result, TransformResult)
        assert result.status == "error"
        assert result.reason == {"reason": "missing_field", "field": "picture"}
        assert result.retryable is False

    def test_none_column_errors(self) -> None:
        row = make_pipeline_row({"picture": None})
        spec = ImageInputConfig(field="picture", format="png")
        result = _resolve(row, [spec])
        assert isinstance(result, TransformResult)
        assert result.reason == {"reason": "missing_field", "field": "picture"}


# ---------------------------------------------------------------------------
# Exact error-reason vocabulary
# ---------------------------------------------------------------------------


class TestErrorVocabulary:
    def test_non_string_ref(self) -> None:
        row = make_pipeline_row({"picture": 12345})
        spec = ImageInputConfig(field="picture", format="png")
        result = _resolve(row, [spec])
        assert isinstance(result, TransformResult)
        assert result.reason == {"reason": "invalid_input", "field": "picture", "error_type": "non_string_ref"}
        assert result.retryable is False

    def test_non_string_ref_in_list_names_index(self) -> None:
        row = make_pipeline_row({"pictures": [PNG_SHA256, 999]})
        spec = ImageInputConfig(field="pictures", format="png")
        result = _resolve(row, [spec])
        assert isinstance(result, TransformResult)
        assert result.reason == {
            "reason": "invalid_input",
            "field": "pictures",
            "error_type": "non_string_ref",
            "list_index": 1,
        }

    def test_invalid_payload_ref(self) -> None:
        row = make_pipeline_row({"picture": "not-hex"})
        spec = ImageInputConfig(field="picture", format="png")
        result = _resolve(row, [spec])
        assert isinstance(result, TransformResult)
        assert result.reason == {
            "reason": "invalid_input",
            "field": "picture",
            "error_type": "invalid_payload_ref",
        }

    def test_blob_not_found(self) -> None:
        missing_ref = "0" * 64
        row = make_pipeline_row({"picture": missing_ref})
        spec = ImageInputConfig(field="picture", format="png")
        result = _resolve(row, [spec])
        assert isinstance(result, TransformResult)
        assert result.reason == {"reason": "blob_not_found", "field": "picture", "blob_ref": missing_ref}
        assert result.retryable is False

    def test_empty_document(self) -> None:
        store = FakePayloadStore({PNG_SHA256: b""})
        row = make_pipeline_row({"picture": PNG_SHA256})
        spec = ImageInputConfig(field="picture", format="png")
        result = _resolve(row, [spec], store=store)
        assert isinstance(result, TransformResult)
        assert result.reason == {"reason": "invalid_input", "field": "picture", "error_type": "empty_document"}

    def test_image_signature_mismatch(self) -> None:
        row = make_pipeline_row({"picture": JPEG_SHA256})
        store = FakePayloadStore({JPEG_SHA256: JPEG_BYTES})
        spec = ImageInputConfig(field="picture", format="png")
        result = _resolve(row, [spec], store=store)
        assert isinstance(result, TransformResult)
        assert result.reason == {
            "reason": "invalid_input",
            "field": "picture",
            "error_type": "image_signature_mismatch",
            "expected": "png",
        }
        # bytes never leak into the error dict
        assert JPEG_BYTES not in repr(result.reason).encode()

    def test_unmapped_image_mime(self) -> None:
        row = make_pipeline_row({"picture": PNG_SHA256, "picture_mime": "application/pdf"})
        spec = ImageInputConfig(field="picture", format_field="picture_mime")
        result = _resolve(row, [spec])
        assert isinstance(result, TransformResult)
        assert result.reason == {
            "reason": "invalid_input",
            "field": "picture_mime",
            "error_type": "unmapped_image_mime",
        }

    def test_unmapped_image_mime_unknown_string(self) -> None:
        row = make_pipeline_row({"picture": PNG_SHA256, "picture_mime": "image/gif"})
        spec = ImageInputConfig(field="picture", format_field="picture_mime")
        result = _resolve(row, [spec])
        assert isinstance(result, TransformResult)
        assert result.reason == {
            "reason": "invalid_input",
            "field": "picture_mime",
            "error_type": "unmapped_image_mime",
        }

    def test_too_many_images(self) -> None:
        store = FakePayloadStore({PNG_SHA256: PNG_BYTES})
        row = make_pipeline_row({"pictures": [PNG_SHA256, PNG_SHA256, PNG_SHA256]})
        spec = ImageInputConfig(field="pictures", format="png")
        result = _resolve(row, [spec], store=store, max_images_per_call=2)
        assert isinstance(result, TransformResult)
        assert result.reason == {"reason": "too_many_images", "max_images": 2, "actual": "3"}
        assert result.retryable is False


# ---------------------------------------------------------------------------
# IntegrityError propagation (Tier-1)
# ---------------------------------------------------------------------------


class TestIntegrityErrorPropagates:
    def test_integrity_error_propagates(self) -> None:
        store = FakePayloadStore(integrity_error=True)
        row = make_pipeline_row({"picture": PNG_SHA256})
        spec = ImageInputConfig(field="picture", format="png")
        with pytest.raises(IntegrityError):
            _resolve(row, [spec], store=store)


# ---------------------------------------------------------------------------
# max_image_bytes boundary
# ---------------------------------------------------------------------------


class TestMaxImageBytesBoundary:
    def test_exactly_at_limit_passes(self) -> None:
        store = FakePayloadStore({PNG_SHA256: PNG_BYTES})
        row = make_pipeline_row({"picture": PNG_SHA256})
        spec = ImageInputConfig(field="picture", format="png")
        result = _resolve(row, [spec], store=store, max_image_bytes=len(PNG_BYTES))
        assert isinstance(result, tuple)
        assert len(result) == 1

    def test_one_over_limit_fails(self) -> None:
        store = FakePayloadStore({PNG_SHA256: PNG_BYTES})
        row = make_pipeline_row({"picture": PNG_SHA256})
        spec = ImageInputConfig(field="picture", format="png")
        result = _resolve(row, [spec], store=store, max_image_bytes=len(PNG_BYTES) - 1)
        assert isinstance(result, TransformResult)
        assert result.reason == {
            "reason": "blob_too_large",
            "field": "picture",
            "max_blob_bytes": len(PNG_BYTES) - 1,
            "actual": str(len(PNG_BYTES)),
        }
        assert result.retryable is False


# ---------------------------------------------------------------------------
# max_images_per_call boundary
# ---------------------------------------------------------------------------


class TestMaxImagesPerCallBoundary:
    def test_exactly_at_limit_passes(self) -> None:
        store = FakePayloadStore({PNG_SHA256: PNG_BYTES})
        row = make_pipeline_row({"pictures": [PNG_SHA256, PNG_SHA256]})
        spec = ImageInputConfig(field="pictures", format="png")
        result = _resolve(row, [spec], store=store, max_images_per_call=2)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_one_over_limit_fails(self) -> None:
        store = FakePayloadStore({PNG_SHA256: PNG_BYTES})
        row = make_pipeline_row({"pictures": [PNG_SHA256, PNG_SHA256, PNG_SHA256]})
        spec = ImageInputConfig(field="pictures", format="png")
        result = _resolve(row, [spec], store=store, max_images_per_call=2)
        assert isinstance(result, TransformResult)
        assert result.reason is not None
        assert result.reason["reason"] == "too_many_images"
