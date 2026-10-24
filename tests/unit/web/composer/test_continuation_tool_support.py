"""Prospective actual-registry classification, without invoking handlers."""

from dataclasses import replace
from types import MappingProxyType

import pytest

from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.tools import _registry
from elspeth.web.composer.tools.continuation_support import continuation_tool_support
from elspeth.web.composer.tools.declarations import EffectDomain, ToolEffects, ToolKind
from tests.unit.web.composer.test_tools import _valid_pipeline_args


@pytest.mark.parametrize(
    "tool_name,arguments,expected",
    (
        ("get_plugin_schema", {"plugin_name": "csv", "plugin_type": "source"}, "read"),
        ("get_expression_grammar", {}, "read"),
        ("list_sources", {}, "read"),
        ("list_blobs", {}, "read"),
        ("list_composer_blobs", {}, "read"),
        ("get_blob_metadata", {}, "read"),
        ("get_blob_content", {}, "read"),
        ("read_attachment", {}, "read"),
        ("inspect_source", {}, "read"),
        ("set_pipeline", _valid_pipeline_args(), "graph"),
        ("request_advisor_hint", {}, "advisor"),
        ("request_interpretation_review", {}, "session_sql"),
        ("preview_pipeline", {}, "unsupported"),
        ("create_blob", {}, "unsupported"),
        ("unknown_continuation_tool", {}, "unsupported"),
    ),
)
def test_actual_declared_support(tool_name, arguments, expected):
    assert continuation_tool_support(tool_name, arguments) == expected
    if tool_name not in _registry.ASYNC_TOOL_EFFECTS:
        assert _registry.continuation_declaration(tool_name) is _registry._DECLARATIONS_BY_NAME.get(tool_name)


@pytest.mark.parametrize("tool_name", ("read_attachment", "inspect_source", "get_expression_grammar"))
@pytest.mark.parametrize("change", ("removed", "contract", "effect", "kind"))
def test_read_support_requires_current_declaration_contract_kind_and_empty_effects(monkeypatch, tool_name, change):
    assert continuation_tool_support(tool_name, {}) == "read"
    original = _registry._DECLARATIONS_BY_NAME
    declaration = original[tool_name]
    changed = dict(original)
    if change == "removed":
        del changed[tool_name]
    elif change == "contract":
        changed[tool_name] = replace(declaration, json_schema=deep_thaw(declaration.json_schema), response_contract=None)
    elif change == "effect":
        changed[tool_name] = replace(
            declaration,
            json_schema=deep_thaw(declaration.json_schema),
            argument_effects=lambda _arguments: ToolEffects((EffectDomain.BLOB_STORE,)),
        )
    else:
        changed[tool_name] = replace(
            declaration,
            json_schema=deep_thaw(declaration.json_schema),
            kind=ToolKind.BLOB_MUTATION,
            response_contract=None,
            cacheable=False,
        )
    with monkeypatch.context() as patch:
        patch.setattr(_registry, "_DECLARATIONS_BY_NAME", MappingProxyType(changed))
        assert continuation_tool_support(tool_name, {}) == "unsupported"
    assert continuation_tool_support(tool_name, {}) == "read"


@pytest.mark.parametrize("tool_name", ("request_advisor_hint", "request_interpretation_review"))
@pytest.mark.parametrize("change", ("removed", "effects"))
def test_async_support_requires_actual_async_effect_owner(monkeypatch, tool_name, change):
    expected = "advisor" if tool_name == "request_advisor_hint" else "session_sql"
    assert continuation_tool_support(tool_name, {}) == expected
    changed = dict(_registry.ASYNC_TOOL_EFFECTS)
    if change == "removed":
        del changed[tool_name]
    else:
        changed[tool_name] = ToolEffects((EffectDomain.BLOB_STORE,))
    # Patch the one imported immutable registry value, not a replacement classifier.
    from elspeth.web.composer.tools import continuation_support

    with monkeypatch.context() as patch:
        patch.setattr(continuation_support, "ASYNC_TOOL_EFFECTS", MappingProxyType(changed))
        assert continuation_tool_support(tool_name, {}) == "unsupported"
    assert continuation_tool_support(tool_name, {}) == expected
