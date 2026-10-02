"""Pin the Playwright tall-dialog fixture's precomputed prompt hash to the live hasher.

The E2E helper ``tests/e2e/helpers/workspace-fixtures.ts`` seeds 80 llm
transforms whose prompt-template review is RESOLVED against
``resolved_prompt_template_hash``. Every composer llm node carries both prompt
roles (Stage-1 ``llm_system_prompt_missing``), so the reviewed surface is the
labelled system-prompt/prompt-template pair and the anchor is
``interpretation_state.prompt_review_anchor_hash_from_options(options)`` — the
value ``_validate_prompt_template_review`` recomputes at the run gate. The
helper carries that hash as a TypeScript constant rather than re-implementing
canonical hashing in TS.

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

from elspeth.web.interpretation_state import (
    prompt_review_anchor_hash_from_options,
    prompt_review_draft_from_options,
)

_FIXTURE = (
    Path(__file__).resolve().parents[4] / "src" / "elspeth" / "web" / "frontend" / "tests" / "e2e" / "helpers" / "workspace-fixtures.ts"
)
_SYSTEM_PROMPT_PATTERN = re.compile(r'const TALL_DIALOG_SYSTEM_PROMPT =\s*"((?:[^"\\]|\\.)*)";')
_PROMPT_PATTERN = re.compile(r'const TALL_DIALOG_PROMPT_TEMPLATE =\s*"((?:[^"\\]|\\.)*)";')
_HASH_PATTERN = re.compile(r'const TALL_DIALOG_PROMPT_TEMPLATE_HASH =\s*"([0-9a-f]{64})";')
# The draft is a TS template literal over the two constants; the pin reads its
# exact spelling so the Python rendering below is the same string.
_DRAFT_LITERAL = "`System prompt:\\n${TALL_DIALOG_SYSTEM_PROMPT}\\n\\nPrompt template:\\n${TALL_DIALOG_PROMPT_TEMPLATE}`;"


def _fixture_constants() -> tuple[str, str, str]:
    source = _FIXTURE.read_text(encoding="utf-8")
    system_match = _SYSTEM_PROMPT_PATTERN.search(source)
    prompt_match = _PROMPT_PATTERN.search(source)
    hash_match = _HASH_PATTERN.search(source)
    assert system_match is not None, f"TALL_DIALOG_SYSTEM_PROMPT not found in {_FIXTURE}"
    assert prompt_match is not None, f"TALL_DIALOG_PROMPT_TEMPLATE not found in {_FIXTURE}"
    assert hash_match is not None, f"TALL_DIALOG_PROMPT_TEMPLATE_HASH not found in {_FIXTURE}"
    system_prompt = system_match.group(1)
    prompt = prompt_match.group(1)
    assert "\\" not in system_prompt, "the pin reads the system prompt literally; keep the TS literal free of escapes"
    assert "\\" not in prompt, "the pin reads the prompt literally; keep the TS literal free of escapes"
    return system_prompt, prompt, hash_match.group(1)


def test_tall_dialog_prompt_hash_constant_matches_the_review_anchor() -> None:
    system_prompt, prompt, pinned = _fixture_constants()
    options = {"system_prompt": system_prompt, "prompt_template": prompt}
    assert pinned == prompt_review_anchor_hash_from_options(options), (
        "TALL_DIALOG_PROMPT_TEMPLATE_HASH in workspace-fixtures.ts no longer equals "
        "prompt_review_anchor_hash_from_options for the fixture's system_prompt and "
        "prompt_template; re-derive the constant or the run gate will reject the "
        "seeded review as drifted."
    )


def test_tall_dialog_review_draft_is_the_production_review_surface() -> None:
    system_prompt, prompt, _ = _fixture_constants()
    source = _FIXTURE.read_text(encoding="utf-8")
    assert source.count(_DRAFT_LITERAL) == 1
    expected = f"System prompt:\n{system_prompt}\n\nPrompt template:\n{prompt}"
    assert prompt_review_draft_from_options({"system_prompt": system_prompt, "prompt_template": prompt}) == expected


def test_tall_dialog_prompts_are_the_ones_the_fixture_seeds() -> None:
    # The constants must be the ONLY prompt text the fixture's llm nodes use;
    # a second literal prompt would bypass the hash the pin guards.
    source = _FIXTURE.read_text(encoding="utf-8")
    assert source.count("system_prompt: TALL_DIALOG_SYSTEM_PROMPT,") == 1
    assert source.count("prompt_template: TALL_DIALOG_PROMPT_TEMPLATE,") == 1
    assert source.count("draft: TALL_DIALOG_PROMPT_REVIEW_DRAFT,") == 1
    assert source.count("accepted_value: TALL_DIALOG_PROMPT_REVIEW_DRAFT,") == 1
    assert source.count("resolved_prompt_template_hash: TALL_DIALOG_PROMPT_TEMPLATE_HASH,") == 1
