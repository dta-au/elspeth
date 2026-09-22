"""Only a complete, consistent JSON object can clear an advisor checkpoint."""

from __future__ import annotations

import json

import pytest

from elspeth.web.composer.service import _ADVISOR_MALFORMED_USER_DETAIL, _parse_advisor_checkpoint_guidance


@pytest.mark.parametrize("findings", ["", "Intent satisfied, contracts consistent.", "The CLEAN token is ordinary text here."])
def test_clean_json_verdict_preserves_technical_findings(findings: str) -> None:
    guidance = json.dumps({"verdict": "CLEAN", "category": "other", "steps": [], "findings": findings, "note": None})
    verdict = _parse_advisor_checkpoint_guidance(guidance)

    assert verdict.ok is True
    assert verdict.blocking is False
    assert verdict.failure_class == "none"
    assert verdict.findings_text == findings
    assert verdict.note is None


@pytest.mark.parametrize("guidance", ["CLEAN", "**CLEAN**", "CLEAN: fine", "Verdict: CLEAN", "FLAGGED: bad sink"])
def test_prose_verdict_cannot_clear_or_block_as_an_accepted_response(guidance: str) -> None:
    verdict = _parse_advisor_checkpoint_guidance(guidance)

    assert verdict.ok is False
    assert verdict.blocking is False
    assert verdict.failure_class == "malformed"
    assert verdict.findings_text == _ADVISOR_MALFORMED_USER_DETAIL
