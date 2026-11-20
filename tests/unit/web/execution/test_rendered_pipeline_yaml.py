"""RenderedPipelineYaml answers a bounded load from the document it rendered.

The property under test: ``_render(doc).load()`` returns or
raises exactly what the deleted round trip did, which is kept here as a
test-local reference implementation:
``load_bounded_pipeline_yaml(yaml.dump(doc, default_flow_style=False))``.
"""

from __future__ import annotations

import importlib
import importlib.util
import math
import random
from collections.abc import Callable
from types import ModuleType
from typing import Any, Never

import pytest
import yaml

from elspeth.config_loading import load_settings_from_config_dict
from elspeth.core.config import (
    MAX_IN_MEMORY_PIPELINE_YAML_BYTES,
    MAX_IN_MEMORY_PIPELINE_YAML_DEPTH,
    MAX_IN_MEMORY_PIPELINE_YAML_NODES,
    load_bounded_pipeline_yaml,
)

_MODULE = "elspeth.web.execution._rendered_pipeline_yaml"


def _rendered_module() -> ModuleType:
    # Looked up at call time so this file collects on a tree without the
    # module, where every test then fails on this assertion.
    assert importlib.util.find_spec(_MODULE) is not None, f"{_MODULE} is missing"
    return importlib.import_module(_MODULE)


def _render(document: object) -> Any:
    return _rendered_module().RenderedPipelineYaml.render(document)


def _of_text(text: str) -> Any:
    return _rendered_module().RenderedPipelineYaml.of_text(text)


def _reference_round_trip(document: object) -> object:
    """The deleted path: dump the document, then parse the text back."""
    return load_bounded_pipeline_yaml(yaml.dump(document, default_flow_style=False))


def _identical(left: object, right: object) -> bool:
    """Equal with exact types, mapping key order, and NaN sign."""
    if type(left) is not type(right):
        return False
    if type(left) is dict and type(right) is dict:
        return list(left) == list(right) and all(_identical(left[key], right[key]) for key in left)
    if type(left) is list and type(right) is list:
        return len(left) == len(right) and all(_identical(a, b) for a, b in zip(left, right, strict=True))
    if type(left) is float and type(right) is float:
        if math.isnan(left) or math.isnan(right):
            return math.isnan(left) and math.isnan(right) and math.copysign(1.0, left) == math.copysign(1.0, right)
        return left == right and math.copysign(1.0, left) == math.copysign(1.0, right)
    return left == right


def _outcome(load: Callable[[], object]) -> tuple[str, object]:
    try:
        return ("value", load())
    except ValueError as exc:
        return ("error", (type(exc), str(exc)))


def _assert_same_as_reference(document: object) -> None:
    expected = _outcome(lambda: _reference_round_trip(document))
    actual = _outcome(_render(document).load)
    assert actual[0] == expected[0], (actual, expected)
    if expected[0] == "value":
        assert _identical(actual[1], expected[1]), (actual[1], expected[1])
    else:
        assert actual[1] == expected[1]


def _forbid_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any parse of the rendered text fail: the load must come from the document."""

    def _no_parse(_yaml_content: str) -> Never:
        raise AssertionError("RenderedPipelineYaml.load() parsed text it rendered from a plain document")

    monkeypatch.setattr(_rendered_module(), "load_bounded_pipeline_yaml", _no_parse)


# ── the adversarial scalar corpus ─────────────────────────────────────────

_YAML_11_RETYPED = [
    "yes", "no", "on", "off", "y", "n", "Yes", "NO", "On", "OFF", "true", "False", "TRUE",
    "~", "null", "Null", "NULL", "", "=", "<<",
    "1e3", "1E3", "-1e-3", "0x10", "0o17", "017", "0b101", "1_000", "1:20", "190:20:30", ".5", "+1", "-0", "1.",
    ".inf", "-.inf", ".nan", ".NaN", "2020-01-01", "2001-12-14t21:59:43.10-05:00", "2001-12-14 21:59:43.10 -5", "12:30",
]  # fmt: skip
_STRUCTURAL = [
    "a: b", "a:b", "a #b", "#a", "&a", "*a", "!a", "!!str x", "%a", "@a", "`a", "|", ">", "'", '"', "{}", "[]",
    "{a: 1}", "[1, 2]", "?", "? a", ",", "- item", "-", "--", "---", "...", "key: value\n  nested: 1",
]  # fmt: skip
_WHITESPACE_AND_LINES = [
    " ", "  ", "\n", "\n\n", " \n", "\n ", "a\n", "a\n\n", "a \n", "a\nb", "a\n b", " a", "a ", "\ta", "a\t", "\t",
    "line1\n\nline3\n\n\n", "trailing spaces   \n  ", "a\r\nb", "\r", "\x0b\x0c",
    "Prompt line one.\nPrompt line two with {{ row.text }}.\n\n",
]  # fmt: skip
_CODEPOINTS = [
    "a\x00b", "\x85", "\u2028", "\u2029", "\ufeff", "\ufeffa", "\x7f", "\x1b[0m", "é", "日本語の指示", "😀 emoji",
    "\ud800", "\udfff", "mixed é 日本 😀 \u2028 end",
]  # fmt: skip
_LONG = ["a" * 200, ("word " * 60).strip(), "word " * 60, "x" * 81 + " " + "y" * 81, "q" * 250_000]
_TEMPLATES = ["{{ row.text }}", "{% if x %}y{% endif %}", "${HOME}", "${VAR:-default}", "secret_ref", "blob_ref inline_content"]
ADVERSARIAL_STRINGS = _YAML_11_RETYPED + _STRUCTURAL + _WHITESPACE_AND_LINES + _CODEPOINTS + _LONG + _TEMPLATES

ADVERSARIAL_NUMBERS: list[object] = [
    0, -1, 2**70, -(2**70), True, False, None, 0.1, -0.0, 0.0, 1e300, 1e-300, 5e-324, 1e16, 123456789.123456789,
    float("inf"), float("-inf"), float("nan"),
]  # fmt: skip


def _settings_skeleton(options: dict[str, object]) -> dict[str, object]:
    return {
        "sources": {"primary": {"plugin": "csv", "on_success": "out", "options": {"path": "/data/in.csv", **options}}},
        "sinks": {"out": {"plugin": "csv", "on_write_failure": "discard", "options": {"path": "/data/out.csv"}}},
    }


@pytest.mark.parametrize("value", ADVERSARIAL_STRINGS + ADVERSARIAL_NUMBERS, ids=repr)
def test_adversarial_values_load_as_the_round_trip_did(value: object) -> None:
    for document in (
        {"value": value},
        {"list": [value, value if not isinstance(value, str) else value + "x"]},
        {"nested": {"z": value, "a": {"b": [value]}}},
    ):
        _assert_same_as_reference(document)
    if type(value) is str and value:
        _assert_same_as_reference({value: "as a key"})


@pytest.mark.parametrize("value", ADVERSARIAL_STRINGS, ids=repr)
def test_adversarial_prompts_produce_the_same_runtime_settings(value: str) -> None:
    document = _settings_skeleton({"prompt": value, "queries": {"q": {"template": value}}, "list": [value]})
    reference = _reference_round_trip(document)
    loaded = _render(document).load()
    assert type(reference) is dict
    assert type(loaded) is dict
    assert load_settings_from_config_dict(loaded) == load_settings_from_config_dict(reference)


def test_seeded_random_strings_load_as_the_round_trip_did() -> None:
    rng = random.Random(20261120)
    alphabet = " \n\t\r:#-?{}[],&*!|>'\"%@`abcXYZ019._~=\\"

    def random_string() -> str:
        chars: list[str] = []
        for _ in range(rng.choice([0, 1, 2, 5, 20, 90, 300])):
            roll = rng.random()
            if roll < 0.5:
                chars.append(rng.choice(alphabet))
            elif roll < 0.8:
                chars.append(chr(rng.randrange(0x20, 0x7F)))
            elif roll < 0.95:
                chars.append(chr(rng.randrange(0x80, 0x3000)))
            else:
                chars.append(chr(rng.randrange(0, 0x110000)))
        return "".join(chars)

    for _ in range(2000):
        text = random_string()
        _assert_same_as_reference({"value": text, "list": [text], **({text: 1} if text else {})})


def test_unsorted_mapping_loads_with_the_sorted_key_order_a_reload_gives(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_parse(monkeypatch)
    document = {"zeta": {"y": 1, "b": 2}, "alpha": [{"k2": "v", "k1": "w"}], "mid": "x"}
    loaded = _render(document).load()
    assert _identical(loaded, {"alpha": [{"k1": "w", "k2": "v"}], "mid": "x", "zeta": {"b": 2, "y": 1}})
    assert list(document) == ["zeta", "alpha", "mid"]  # the caller's document is untouched


@pytest.mark.parametrize(
    "document",
    [
        pytest.param({"tuple": (1, 2)}, id="tuple-value"),
        pytest.param({(1, 2): "tuple key"}, id="tuple-key"),
        pytest.param({1: "int key"}, id="int-key"),
        pytest.param({None: "null key"}, id="null-key"),
        pytest.param({"set": {1, 2}}, id="set"),
        pytest.param({"bytes": b"\x00\xff"}, id="bytes"),
        pytest.param({"nan": float("nan")}, id="nan"),
        pytest.param({"sub": type("_Str", (str,), {})("subclass")}, id="str-subclass"),
        pytest.param({"sub": type("_Int", (int,), {})(3)}, id="int-subclass"),
        pytest.param({"sub": type("_Dict", (dict,), {})(a=1)}, id="dict-subclass"),
        pytest.param({"sub": type("_List", (list,), {})([1])}, id="list-subclass"),
        pytest.param(["root", "list"], id="list-root"),
        pytest.param("scalar root", id="scalar-root"),
    ],
)
def test_documents_outside_plain_data_load_through_the_parser(document: object) -> None:
    _assert_same_as_reference(document)


def test_shared_container_is_refused_as_an_alias_exactly_as_before() -> None:
    shared = {"k": "v"}
    document = {"a": shared, "b": shared}
    with pytest.raises(ValueError) as reference:
        _reference_round_trip(document)
    with pytest.raises(ValueError) as rendered:
        _render(document).load()
    assert str(rendered.value) == str(reference.value) == "YAML parse failed: ComposerError"


def test_each_load_returns_a_fresh_plain_tree_detached_from_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    document: dict[str, object] = {"sources": {"s": {"options": {"q": ["a", "b"]}}}}
    rendered = _render(document)
    _forbid_parse(monkeypatch)
    document["sources"] = "mutated after render"
    first = rendered.load()
    assert type(first) is dict
    assert type(first["sources"]) is dict
    assert type(first["sources"]["s"]["options"]["q"]) is list
    first["sources"]["s"]["options"]["q"].append("c")
    second = rendered.load()
    assert second == {"sources": {"s": {"options": {"q": ["a", "b"]}}}}
    assert second is not first


def test_text_is_exactly_what_yaml_dump_renders() -> None:
    document = {"b": [1, "yes"], "a": {"multi": "x\ny\n"}}
    assert _render(document).text == yaml.dump(document, default_flow_style=False)


def test_of_text_loads_through_the_bounded_loader() -> None:
    assert _of_text("b: 1\na: [x]\n").load() == {"b": 1, "a": ["x"]}
    with pytest.raises(ValueError, match="YAML parse failed"):
        _of_text("a: &x 1\nb: *x\n").load()


# ── limits: each boundary passes at the limit and refuses one over ────────


def _bytes_document(text_bytes: int) -> dict[str, str]:
    # "k: " + value + "\n": a plain scalar without spaces never folds.
    return {"k": "x" * (text_bytes - 4)}


def _list_nodes_document(nodes: int) -> dict[str, list[int]]:
    # root mapping + key + sequence = 3 nodes, then one per item.
    return {"k": list(range(nodes - 3))}


def _mapping_nodes_document(nodes: int) -> dict[str, object]:
    # root mapping + "k" + inner mapping + "l" + sequence = 5 nodes, two per
    # inner entry (key and value), and one more item in "l" to reach an even count.
    pairs, odd = divmod(nodes - 5, 2)
    return {"k": {f"k{i:05d}": i for i in range(pairs)}, "l": ["x"] * odd}


def _depth_document(depth: int) -> dict[str, object]:
    # root mapping at depth 1, the key "k" and its value at depth 2,
    # each wrapping list one deeper, the innermost scalar at ``depth``.
    value: object = "leaf"
    for _ in range(depth - 2):
        value = [value]
    return {"k": value}


def test_the_boundary_builders_hit_the_exact_sizes_they_claim() -> None:
    assert len(yaml.dump(_bytes_document(1000), default_flow_style=False).encode("utf-8")) == 1000


@pytest.mark.parametrize(
    ("builder", "limit", "message"),
    [
        pytest.param(
            _bytes_document,
            MAX_IN_MEMORY_PIPELINE_YAML_BYTES,
            f"YAML content exceeds the {MAX_IN_MEMORY_PIPELINE_YAML_BYTES} byte YAML limit",
            id="bytes-2MiB",
        ),
        pytest.param(_list_nodes_document, MAX_IN_MEMORY_PIPELINE_YAML_NODES, "YAML parse failed: ComposerError", id="nodes-list"),
        pytest.param(_mapping_nodes_document, MAX_IN_MEMORY_PIPELINE_YAML_NODES, "YAML parse failed: ComposerError", id="nodes-mapping"),
        pytest.param(_depth_document, MAX_IN_MEMORY_PIPELINE_YAML_DEPTH, "YAML parse failed: ComposerError", id="depth-64"),
    ],
)
def test_limit_passes_at_the_boundary_and_refuses_one_over_exactly_as_before(
    builder: Callable[[int], object], limit: int, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    at_limit = builder(limit)
    over_limit = builder(limit + 1)
    reference_ok = _reference_round_trip(at_limit)
    with pytest.raises(ValueError) as reference_refusal:
        _reference_round_trip(over_limit)
    assert type(reference_refusal.value) is ValueError
    assert str(reference_refusal.value) == message

    rendered_ok = _render(at_limit)
    rendered_over = _render(over_limit)
    with pytest.raises(ValueError) as refusal:
        rendered_over.load()
    assert type(refusal.value) is ValueError
    assert str(refusal.value) == message
    assert type(refusal.value.__cause__) is type(reference_refusal.value.__cause__)

    # At the limit the answer comes from the document, never from a parse.
    _forbid_parse(monkeypatch)
    assert _identical(rendered_ok.load(), reference_ok)
