"""Replay binds blob refs declared by every supported payload consumer."""

from types import SimpleNamespace

from elspeth.engine.orchestrator.run_context_factory import _configured_replay_blob_fields


def test_replay_blob_fields_include_textract_llm_images_and_web_multipart() -> None:
    transforms = [
        SimpleNamespace(name="aws_textract_inline_analysis", config={"blob_ref_field": "document_sha"}),
        SimpleNamespace(name="llm", config={"image_inputs": [{"field": "photos", "format": "png"}]}),
        SimpleNamespace(name="web_scrape", config={"request_multipart_field": "upload_parts"}),
    ]

    scalar, images, multipart = _configured_replay_blob_fields(transforms)

    assert scalar == {"document_sha"}
    assert images == {"photos"}
    assert multipart == {"upload_parts"}
