"""An exact edit that takes a prompt template over the byte bound says so; every other refusal keeps the generic text.

The plugin checks a prompt template's size before anything else, so an edited
``prompt_template`` over the bound was refused for its size. Before the change
the model was told only "Edited prompt did not pass node validation", and could
not tell a size refusal from a syntax error. Expected texts are spelled here so
the file collects on the tree before the change.
"""

from __future__ import annotations

from hashlib import sha256

from tests.unit.web.composer.test_prompt_patch_reviews import _approved_state
from tests.unit.web.composer.test_tools import _mock_catalog, execute_tool

LIMIT = 64 * 1024
GENERIC = "Edited prompt did not pass node validation; inspect the node and retry."
NESTED = "{% for item in [1] %}" * 21 + "x" + "{% endfor %}" * 21


def _edit(text: str, old: str, replacement: str, *, replace_all: bool = False) -> dict[str, object]:
    return {
        "target": {"kind": "node_option", "node_id": "code_themes", "option": "prompt_template"},
        "expected_sha256": sha256(text.encode("utf-8")).hexdigest(),
        "expected_state_id": "state-1",
        "edits": [{"old": old, "replacement": replacement, "replace_all": replace_all}],
    }


def _grown(text: str, fill: str) -> tuple[dict[str, object], int]:
    """Replace every space with ``fill``: one edit fragment under the tool's own 64 KiB cap, a result over the bound."""
    assert text.count(" ") >= 3
    return _edit(text, " ", fill, replace_all=True), len(text.replace(" ", fill).encode("utf-8"))


def _refusal(args: dict[str, object]) -> tuple[str, str | None]:
    state = _approved_state()
    result = execute_tool("change_substring", args, state, _mock_catalog(), current_state_id="state-1")
    assert not result.success
    assert result.updated_state is None or result.updated_state is state
    error = result.validation.errors[0]
    return error.message, error.error_code


def test_an_edit_over_the_byte_bound_is_refused_with_its_size_and_the_limit() -> None:
    text = _approved_state().nodes[0].options["prompt_template"]
    args, size = _grown(text, "x" * 30_000)
    assert size > LIMIT

    message, code = _refusal(args)

    assert message == (
        f"prompt_template is {size} bytes; the limit for one prompt template is {LIMIT} bytes. "
        "Shorten it, or move reference text into a file_inject step and read it from the row."
    )
    assert code == "plugin_options_invalid"


def test_multi_byte_text_is_measured_in_utf8_bytes() -> None:
    text = _approved_state().nodes[0].options["prompt_template"]
    args, size = _grown(text, "é" * 15_000)  # 2 bytes per character: under the bound in characters, over it in bytes
    assert len(text.replace(" ", "é" * 15_000)) < LIMIT < size

    message, _code = _refusal(args)

    assert message.startswith(f"prompt_template is {size} bytes; the limit for one prompt template is {LIMIT} bytes.")


def test_other_causes_keep_the_generic_sentence() -> None:
    text = _approved_state().nodes[0].options["prompt_template"]

    for replacement in (NESTED, "{{ unclosed"):
        message, code = _refusal(_edit(text, "Classify", replacement))
        assert message == GENERIC, replacement[:30]
        assert code == "plugin_options_invalid"
