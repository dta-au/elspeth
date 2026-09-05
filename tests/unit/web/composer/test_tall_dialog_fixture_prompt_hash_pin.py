"""Pin the Playwright tall-dialog fixture's precomputed prompt hash to the live hasher.

The E2E helper ``tests/e2e/helpers/workspace-fixtures.ts`` seeds 80 llm
transforms whose prompt-template review is RESOLVED against
``resolved_prompt_template_hash = stable_hash(prompt_template)`` — the value
``interpretation_state._validate_prompt_template_review`` recomputes for an
unstructured prompt. The helper carries that hash as a TypeScript constant
rather than re-implementing canonical hashing in TS.

Why this pin exists (a0's condition on elspeth-e5a38115a6 / e425a36805): no
composer surface checks the hash before Run. The pending-site enumerator
(``_missing_prompt_template_review_sites``) returns nothing for any row whose
``status`` is ``"resolved"`` without comparing the hash, and the seed route's
``_reject_malformed_interpretation_requirements`` only parses row shape. So if
the hasher or the prompt text drifts, the fixture still seeds, no review card
opens, and the drift surfaces only when the run gate
(``materialize_state_for_execution`` -> ``_validate_prompt_template_review``)
raises ``ValueError("... prompt-template review hash drifted")``. The
tall-dialog scenario never presses Run, so in E2E the drift would be
INVISIBLE: this test is the only guard. A drifted constant must fail a test,
not go unnoticed.
"""

from __future__ import annotations

import re
from pathlib import Path

from elspeth.core.canonical import stable_hash

_FIXTURE = (
    Path(__file__).resolve().parents[4] / "src" / "elspeth" / "web" / "frontend" / "tests" / "e2e" / "helpers" / "workspace-fixtures.ts"
)
_PROMPT_PATTERN = re.compile(r'const TALL_DIALOG_PROMPT_TEMPLATE =\s*"((?:[^"\\]|\\.)*)";')
_HASH_PATTERN = re.compile(r'const TALL_DIALOG_PROMPT_TEMPLATE_HASH =\s*"([0-9a-f]{64})";')


def _fixture_constants() -> tuple[str, str]:
    source = _FIXTURE.read_text(encoding="utf-8")
    prompt_match = _PROMPT_PATTERN.search(source)
    hash_match = _HASH_PATTERN.search(source)
    assert prompt_match is not None, f"TALL_DIALOG_PROMPT_TEMPLATE not found in {_FIXTURE}"
    assert hash_match is not None, f"TALL_DIALOG_PROMPT_TEMPLATE_HASH not found in {_FIXTURE}"
    prompt = prompt_match.group(1)
    assert "\\" not in prompt, "the pin reads the prompt literally; keep the TS literal free of escapes"
    return prompt, hash_match.group(1)


def test_tall_dialog_prompt_hash_constant_matches_stable_hash() -> None:
    prompt, pinned = _fixture_constants()
    assert pinned == stable_hash(prompt), (
        "TALL_DIALOG_PROMPT_TEMPLATE_HASH in workspace-fixtures.ts no longer equals "
        "stable_hash(TALL_DIALOG_PROMPT_TEMPLATE); re-derive the constant or the seeded "
        "prompt-template review will re-open as a pending card."
    )


def test_tall_dialog_prompt_is_the_one_the_fixture_seeds() -> None:
    # The constant must be the ONLY prompt text the fixture's llm nodes use;
    # a second literal prompt would bypass the hash the pin guards.
    source = _FIXTURE.read_text(encoding="utf-8")
    assert source.count("prompt_template: TALL_DIALOG_PROMPT_TEMPLATE,") == 1
    assert source.count("draft: TALL_DIALOG_PROMPT_TEMPLATE,") == 1
    assert source.count("accepted_value: TALL_DIALOG_PROMPT_TEMPLATE,") == 1
    assert source.count("resolved_prompt_template_hash: TALL_DIALOG_PROMPT_TEMPLATE_HASH,") == 1
