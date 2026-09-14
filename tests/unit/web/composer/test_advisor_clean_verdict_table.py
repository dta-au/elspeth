"""CLEAN verdict terminator table for the advisor checkpoint parser.

One table decides every CLEAN-acceptance shape that turns on the character
after the ``CLEAN`` token. The parser's anchored CLEAN arms
(``_ADVISOR_CLEAN_VERDICT_LINE_RE`` and ``_ADVISOR_CLEAN_VERDICT_LABEL_RE`` in
``elspeth.web.composer.service``) accept a token closed by ``:``, an en/em dash,
end of line, or a run of ASCII hyphens or full stops followed by whitespace or
end of line. A hyphen or full stop that runs straight into more text joins a
word (``clean-room``) or names a file (``clean.csv``), so a reply that opens
that way describes something rather than signing the build off.

Rows marked ``malformed`` cost one format retry; they never block and never
sign off. FLAGGED scanning is out of scope here (it has its own tests in
``test_advisor_checkpoint.py``).
"""

from __future__ import annotations

from typing import Literal

import pytest

from elspeth.web.composer.service import _ADVISOR_MALFORMED_USER_DETAIL, _parse_advisor_checkpoint_guidance

_TABLE: list[tuple[str, Literal["clean", "malformed"]]] = [
    # -- MUST sign off ------------------------------------------------------
    ("CLEAN", "clean"),
    ("CLEAN.", "clean"),
    ("CLEAN. Intent satisfied, contracts consistent.", "clean"),
    ("CLEAN...", "clean"),
    ("CLEAN: ok", "clean"),
    # The ASCII double hyphen is the dash the live advisor model types.
    ("CLEAN -- no issues found.", "clean"),
    ("CLEAN --", "clean"),
    ("Verdict: CLEAN -- no issues found.", "clean"),
    # Finding #2's recorded decision: a single spaced hyphen is a dash.
    ("CLEAN - ok", "clean"),
    # Named escapes so the en dash cannot be confused with a hyphen on review.
    ("CLEAN \N{EM DASH} ok", "clean"),
    ("CLEAN \N{EN DASH} ok", "clean"),
    ("**CLEAN**", "clean"),
    ("`CLEAN`", "clean"),
    ("``CLEAN``", "clean"),
    ("CLEAN\nThe pipeline reads every CSV row and writes it to the sink.", "clean"),
    # -- MUST NOT sign off --------------------------------------------------
    ("clean.csv is the input", "malformed"),
    ("CLEAN.csv is fine", "malformed"),
    ("Verdict: CLEAN.csv is fine", "malformed"),
    ("clean-room build", "malformed"),
    ("clean--room build", "malformed"),
    # An unspaced double hyphen is not accepted as a dash: only a hyphen run
    # followed by whitespace or end of line closes the token.
    ("CLEAN--no issues", "malformed"),
    # An unbalanced backtick is literal text (CommonMark), not emphasis.
    ("`CLEAN: ok", "malformed"),
]


@pytest.mark.parametrize(("guidance", "expected"), _TABLE, ids=[repr(row[0]) for row in _TABLE])
def test_clean_verdict_terminator_table(guidance: str, expected: Literal["clean", "malformed"]) -> None:
    verdict = _parse_advisor_checkpoint_guidance(guidance)

    assert verdict.blocking is False
    if expected == "clean":
        assert verdict.ok is True, f"not signed off: {guidance!r}"
        assert verdict.failure_class == "none"
        assert verdict.findings_text == guidance.strip()
    else:
        assert verdict.ok is False, f"signed off: {guidance!r}"
        assert verdict.failure_class == "malformed"
        assert verdict.findings_text == _ADVISOR_MALFORMED_USER_DETAIL
