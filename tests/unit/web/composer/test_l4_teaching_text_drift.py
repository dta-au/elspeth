"""Regression tests for finding #40/#41: composer teaching text drift.

#41: ``_STRUCTURAL_NODE_TYPE_GUIDANCE['coalesce']`` (tools/_common.py) told
planners to prefer one ``llm`` node's ``queries`` map over fork/coalesce
unconditionally, while the SHAPE SELECTION rule in
``planner_authoring_aids.py`` conditions that preference on same-field vs
genuinely independent branches. The two surfaces must agree.

#40: ``get_plugin_assistance``'s tool description and
``_execute_get_plugin_assistance``'s docstring claimed discovery mode
(``issue_code`` omitted) never returns ``examples``, but the handler
unconditionally serializes ``assistance.examples`` and the ``llm`` transform
publishes a worked ``queries`` exemplar in discovery mode. The text must not
claim examples are failure-mode-exclusive.

Also covers a stray docstring reference in ``state.py`` to a function name
that does not exist (``_multi_query_template_binding_errors``); the live
guard is ``_validate_multi_query_template_variable_bindings``.
"""

from __future__ import annotations

import inspect

from elspeth.web.composer import state as composer_state
from elspeth.web.composer.tools._common import _STRUCTURAL_NODE_TYPE_GUIDANCE
from elspeth.web.composer.tools.generation import (
    _GET_PLUGIN_ASSISTANCE_DECLARATION,
    _execute_get_plugin_assistance,
)


def test_coalesce_guidance_conditions_the_queries_preference_on_independence() -> None:
    """The coalesce reactive message must not assert an unconditional
    queries-over-fork/coalesce preference; it must carry the same
    same-field-vs-independent-branches condition as the SHAPE SELECTION rule.
    """
    message = _STRUCTURAL_NODE_TYPE_GUIDANCE["coalesce"]

    assert "queries" in message

    # The old, refuted wording asserted an unconditional preference with no
    # carve-out for independent branches. That sentence must be gone.
    unconditional = "prefer ONE llm transform with a `queries` map instead of fork/coalesce."
    assert unconditional not in message, f"coalesce guidance must not restate the queries preference unconditionally; got: {message!r}"

    # The restated guidance must carry the independence condition so it
    # agrees with planner_authoring_aids.py's SHAPE SELECTION rule.
    assert "queries" in message and "independent" in message.lower(), (
        "coalesce guidance must condition the queries preference on same-field vs "
        f"genuinely independent branches, mirroring SHAPE SELECTION: {message!r}"
    )


def test_get_plugin_assistance_description_does_not_claim_examples_are_failure_mode_only() -> None:
    """The tool description must not claim discovery mode never returns
    ``examples`` — it does, for plugins that define them (e.g. ``llm``).
    """
    description = _GET_PLUGIN_ASSISTANCE_DECLARATION.description

    # Discovery-mode bullet must not enumerate only summary/composer_hints
    # while implying examples are exclusive to failure mode. We check the
    # substance: the description must say examples can appear in either mode.
    assert "examples" in description
    assert (
        "may also" in description or "either mode" in description or "plugin-defined" in description or "may be populated" in description
    ), f"description must state examples may be returned in either mode: {description!r}"


def test_execute_get_plugin_assistance_docstring_does_not_claim_examples_are_failure_mode_only() -> None:
    docstring = inspect.getdoc(_execute_get_plugin_assistance) or ""

    assert "may also" in docstring or "either mode" in docstring or "plugin-defined" in docstring or "may be populated" in docstring, (
        f"docstring must state examples may be returned in either mode: {docstring!r}"
    )


def test_state_module_does_not_reference_nonexistent_multi_query_helper() -> None:
    """state.py must not point readers at a function name that does not
    exist. The live multi-query template binding guard is
    ``_validate_multi_query_template_variable_bindings``.
    """
    source = inspect.getsource(composer_state)

    assert "_multi_query_template_binding_errors" not in source
    assert callable(composer_state._validate_multi_query_template_variable_bindings)
