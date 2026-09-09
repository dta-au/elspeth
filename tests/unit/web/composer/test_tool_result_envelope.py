"""The envelope registry is the single authority for the ToolResult wire vocabulary (elspeth-e405ad7cd2).

``ToolResult.to_dict`` emits it, ``redaction`` admits it, and the planner's
closed discovery twin projects a subset of it. The whole-tree pins that hold
those three to this module live in ``test_tool_result_envelope_gate.py``; this
file pins the registry's own shape and its leaf-module discipline.
"""

from __future__ import annotations

import ast
from pathlib import Path

from elspeth.web.composer import tool_result_envelope as env

REPO_ROOT = Path(__file__).resolve().parents[4]
LEAF = REPO_ROOT / "src" / "elspeth" / "web" / "composer" / "tool_result_envelope.py"


def test_registry_tuples_are_disjoint_and_in_emission_order() -> None:
    required = env.TOOL_RESULT_REQUIRED_KEYS
    optional = env.TOOL_RESULT_OPTIONAL_KEYS
    assert required == ("success", "validation", "affected_nodes", "version")
    assert optional == (
        "data",
        "runtime_preflight",
        "validation_delta",
        "post_call_hints",
        "plugin_schemas",
        "validation_guidance",
        "applied_component",
    )
    assert not set(required) & set(optional)
    assert env.TOOL_RESULT_POST_DISPATCH_KEYS == ("pipeline_content_hash_schema", "pipeline_content_hash")
    assert not set(env.TOOL_RESULT_POST_DISPATCH_KEYS) & (set(required) | set(optional))


def test_tool_result_keys_drops_data_only_when_asked() -> None:
    with_data = env.tool_result_keys(data=True)
    without = env.tool_result_keys(data=False)
    assert "data" in with_data
    assert "data" not in without
    assert tuple(key for key in with_data if key != "data") == without
    assert with_data[: len(env.TOOL_RESULT_REQUIRED_KEYS)] == env.TOOL_RESULT_REQUIRED_KEYS


def test_sub_envelope_registries_are_closed_and_unique() -> None:
    for registry in (env.VALIDATION_KEYS, env.VALIDATION_DELTA_KEYS, env.APPLIED_COMPONENT_KEYS):
        assert len(set(registry)) == len(registry)
        assert all(isinstance(key, str) and key for key in registry)


def test_leaf_module_imports_nothing_from_the_web_package() -> None:
    """redaction.py and tools/_common.py both import this module; a web import here is a cycle.

    ``tools/__init__`` imports ``_dispatch``, which imports ``redaction``, so the
    registry cannot live under ``tools/`` either — it lives beside ``redaction``
    as a leaf.
    """
    tree = ast.parse(LEAF.read_text(encoding="utf-8"))
    offenders = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None and node.module.startswith("elspeth.web")
    ]
    offenders += [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name.startswith("elspeth.web")
    ]
    assert offenders == [], offenders


def test_guidance_typed_dicts_live_in_the_leaf_and_are_reexported() -> None:
    from elspeth.web.composer.tools import generation

    assert generation.ValidationGuidance is env.ValidationGuidance
    assert generation.ValidationCodeGuidance is env.ValidationCodeGuidance
