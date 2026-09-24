"""Replay binds blob refs declared by every supported payload consumer."""

from types import SimpleNamespace

from elspeth.engine.orchestrator.run_context_factory import _configured_replay_blob_fields


def test_replay_blob_fields_include_textract_and_llm_images() -> None:
    transforms = [
        SimpleNamespace(name="aws_textract_inline_analysis", config={"blob_ref_field": "document_sha"}),
        SimpleNamespace(name="llm", config={"image_inputs": [{"field": "photos", "format": "png"}]}),
    ]

    scalar, images = _configured_replay_blob_fields(transforms)

    assert scalar == {"document_sha"}
    assert images == {"photos"}
