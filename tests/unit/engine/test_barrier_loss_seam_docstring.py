"""Pin the corrected _notify_barrier_of_lost_branch docstring BY ITS CLAIMS.

The old docstring called the method "THE single seam every early-exit path
calls" and claimed "at most one arm yields results" — both false (verified
2026-08-21; see docs/superpowers/specs/2026-08-21-barrier-scope-proposal.md)
and both trusted by design work. Pin the truth so the claims cannot silently
regress while WS3 is pending.

DELETE THIS MODULE with the method itself when WS3 lands the unified
settle-member seam (spec §6.1) — it pins a docstring that dies with its code.
"""

from elspeth.engine.processor import RowProcessor


def test_loss_seam_docstring_does_not_claim_universal_coverage() -> None:
    doc = RowProcessor._notify_barrier_of_lost_branch.__doc__ or ""
    lowered = doc.lower()
    assert "the single seam every early-exit path calls" not in lowered
    assert "at most one arm yields results" not in lowered
    assert "not a single seam" in lowered


def test_loss_seam_docstring_names_the_verified_bypass_classes() -> None:
    doc = RowProcessor._notify_barrier_of_lost_branch.__doc__ or ""
    assert "BATCH_CONSUMED" in doc
    assert "QUARANTINED_AT_SOURCE" in doc
    assert "record_token_outcome" in doc
