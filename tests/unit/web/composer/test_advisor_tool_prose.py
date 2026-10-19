"""Phase P5.9 — the advisor tool description no longer claims 'Disabled by default'."""

from __future__ import annotations

from elspeth.web.composer.tools._dispatch import _REQUEST_ADVISOR_HINT_DEFINITION


def test_advisor_tool_prose_not_stale() -> None:
    description = _REQUEST_ADVISOR_HINT_DEFINITION["description"]
    assert isinstance(description, str)
    assert "Disabled by default" not in description
    # On-demand advice cannot replace the independent END publication review.
    assert "Independent END publication review still applies" in description
    assert "errors do not confirm a stop" in description
