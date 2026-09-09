"""Phase P5.1 — the public advisor sign-off checkpoint Protocol method."""

from __future__ import annotations

import inspect

from elspeth.web.composer.protocol import ComposerService


def test_protocol_declares_run_signoff_checkpoint() -> None:
    sig = inspect.signature(ComposerService.run_signoff_checkpoint)
    params = sig.parameters
    # keyword-only contract (verbatim names)
    assert params["state"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["session_id"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["recorder"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["progress"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["progress"].default is None
    assert inspect.iscoroutinefunction(ComposerService.run_signoff_checkpoint)


def test_protocol_describes_evidence_scoped_completion_advisory() -> None:
    doc = inspect.getdoc(ComposerService.run_signoff_checkpoint)

    assert doc is not None
    assert "evidence-scoped completion advisory verdict" in doc
    assert "whole-pipeline" not in doc
    assert "structural sign-off" not in doc
    assert "verify the pipeline" not in doc
