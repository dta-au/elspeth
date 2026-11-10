"""file_inject config: format-aware parsing, refusals, caps and declared types.

Everything here fires at config validation, before any row is processed. The
entry ``content`` is always text: the settings loader has already turned a
``content_file`` into ``content`` + ``content_source`` by the time the model
sees the options.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from elspeth.contracts.schema import FieldDefinition
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.transforms import file_inject
from elspeth.plugins.transforms.file_inject import (
    FileInjectConfig,
    added_output_fields,
    parse_injected_content,
)

OBSERVED: dict[str, Any] = {"mode": "observed"}


def _all(files: dict[Any, Any], **extra: Any) -> dict[str, Any]:
    return {"schema": OBSERVED, "mode": "all", "files": files, **extra}


def _select(files: dict[Any, Any], **extra: Any) -> dict[str, Any]:
    return {"schema": OBSERVED, "mode": "select", "key_field": "kind", "output_field": "guidance", "files": files, **extra}


def _one(content: str, fmt: str, **entry: Any) -> object:
    """Parse one entry through the whole config model and return its value."""
    cfg = FileInjectConfig.model_validate(_all({"entry": {"content": content, "format": fmt, "output_field": "out", **entry}}))
    return cfg._parsed["entry"]


def _refused(options: dict[str, Any]) -> str:
    with pytest.raises(ValidationError) as exc:
        FileInjectConfig.model_validate(options)
    return str(exc.value)


def _text(name: str = "out") -> dict[str, Any]:
    return {"content": "hello", "format": "text", "output_field": name}


# --- values -----------------------------------------------------------------


def test_text_is_written_unmodified() -> None:
    assert _one("a\n\n", "text") == "a\n\n"
    assert _one("\ufeffa\r\n", "text") == "\ufeffa\r\n"


def test_json_object_array_and_scalar() -> None:
    assert _one('{"a":[1,2.5,null]}', "json") == {"a": [1, 2.5, None]}
    assert _one("3", "json") == 3
    assert _one('["x", true]', "json") == ["x", True]


def test_json_data_key_takes_member() -> None:
    assert _one('{"rubric": {"max": 5}, "other": 1}', "json", data_key="rubric") == {"max": 5}


def test_jsonl_one_value_per_nonblank_line() -> None:
    assert _one('{"a":1}\n\n[2]\n', "jsonl") == [{"a": 1}, [2]]


def test_jsonl_keeps_a_line_separator_inside_a_string() -> None:
    # U+2028 is legal unescaped inside a JSON string; only \n, \r\n and \r end a line.
    assert _one('"a\u2028b"\n', "jsonl") == ["a\u2028b"]


def test_csv_rows_keyed_by_header() -> None:
    assert _one("k,v\na,1\n", "csv") == [{"k": "a", "v": "1"}]
    assert _one("k,v\n", "csv") == []


def test_csv_custom_delimiter() -> None:
    assert _one("k;v\na,b;1\n", "csv", delimiter=";") == [{"k": "a,b", "v": "1"}]


# --- review focus 1, 3, 5 ---------------------------------------------------


def test_bom_is_stripped_for_structured_formats() -> None:
    assert _one("\ufeffk,v\na,1\n", "csv") == [{"k": "a", "v": "1"}]
    assert _one('\ufeff{"a": 1}', "json") == {"a": 1}
    assert _one("\ufeff[1]\n[2]\n", "jsonl") == [[1], [2]]


def test_empty_file_per_format() -> None:
    assert _one("", "text") == ""
    assert _one("", "jsonl") == []
    for fmt in ("json", "csv"):
        message = _refused(_all({"blank": {"content": "", "format": fmt, "output_field": "out"}}))
        assert "'blank'" in message
        assert "empty" in message


def test_crlf_leaves_no_carriage_return() -> None:
    assert _one("k,v\r\na,1\r\n", "csv") == [{"k": "a", "v": "1"}]
    assert _one('{"a":"x"}\r\n[2]\r\n', "jsonl") == [{"a": "x"}, [2]]
    assert _one("k,v\ra,1\r", "csv") == [{"k": "a", "v": "1"}]
    # Universal newlines reach inside a quoted cell too: its line break is \n.
    assert _one('k\r\n"a\r\nb"\r\n', "csv") == [{"k": "a\nb"}]


# --- refusals ---------------------------------------------------------------


def test_malformed_json_refused() -> None:
    message = _refused(_all({"rubric": {"content": '{"a": ', "format": "json", "output_field": "out"}}))
    assert "files entry 'rubric'" in message
    assert "not valid JSON" in message
    message = _refused(_all({"lines": {"content": '{"a": 1}\n{"b": \n', "format": "jsonl", "output_field": "out"}}))
    assert "files entry 'lines'" in message
    assert "line 2" in message


def test_deeply_nested_json_refused() -> None:
    message = _refused(_all({"deep": {"content": "[" * 200_000, "format": "json", "output_field": "out"}}))
    assert "files entry 'deep'" in message
    assert "nested too deeply" in message


@pytest.mark.parametrize(
    ("content", "fragment"),
    [
        ('{"a": NaN}', "NaN"),
        ('{"a": -Infinity}', "-Infinity"),
        ('{"a": 1e999}', "finite"),
        ('{"a": 1, "a": 2}', "duplicate JSON object member 'a'"),
    ],
)
def test_nan_and_duplicate_members_refused(content: str, fragment: str) -> None:
    for fmt in ("json", "jsonl"):
        message = _refused(_all({"rubric": {"content": content, "format": fmt, "output_field": "out"}}))
        assert "files entry 'rubric'" in message
        assert fragment in message


def test_data_key_on_non_object_or_absent_refused() -> None:
    message = _refused(_all({"rubric": {"content": "[1]", "format": "json", "data_key": "rubric", "output_field": "out"}}))
    assert "files entry 'rubric'" in message
    assert "data_key 'rubric' needs a top-level JSON object, got list" in message
    message = _refused(_all({"rubric": {"content": '{"other": 1}', "format": "json", "data_key": "rubric", "output_field": "out"}}))
    assert "files entry 'rubric'" in message
    assert "no member 'rubric'" in message


@pytest.mark.parametrize(
    ("content", "fragment"),
    [
        ("k,v\na,1,extra\n", "data row 1 has more cells"),
        ("k,v\na,1\nb\n", "data row 2 has fewer cells"),
        ("k,k\na,1\n", "duplicate CSV header 'k'"),
        ("k,\na,1\n", "empty CSV header name"),
        ("\n\n", "no CSV header row"),
        ('k,v\na,"1\n', "not valid CSV"),
    ],
)
def test_csv_ragged_row_and_bad_headers_refused(content: str, fragment: str) -> None:
    message = _refused(_all({"examples": {"content": content, "format": "csv", "output_field": "out"}}))
    assert "files entry 'examples'" in message
    assert fragment in message


@pytest.mark.parametrize("delimiter", ["", ";;", "\n", '"'])
def test_csv_delimiter_must_be_one_usable_character(delimiter: str) -> None:
    message = _refused(_all({"examples": {"content": "k;v\n", "format": "csv", "delimiter": delimiter, "output_field": "out"}}))
    assert "files entry 'examples'" in message
    assert "delimiter" in message


def test_format_option_on_wrong_format_refused() -> None:
    message = _refused(_all({"examples": {"content": "k\n", "format": "csv", "data_key": "k", "output_field": "out"}}))
    assert "files entry 'examples'" in message
    assert "data_key applies only to format 'json'" in message
    message = _refused(_all({"rubric": {"content": "{}", "format": "json", "delimiter": ";", "output_field": "out"}}))
    assert "files entry 'rubric'" in message
    assert "delimiter applies only to format 'csv'" in message


def test_entry_needs_content() -> None:
    message = _refused(_all({"policy": {"format": "text", "output_field": "out"}}))
    assert "files.policy.content" in message
    assert "Field required" in message
    assert "at least one entry" in _refused(_all({}))


def test_duplicate_output_field_refused() -> None:
    message = _refused(_all({"a": _text("same"), "b": _text("same")}))
    assert "'a'" in message
    assert "'b'" in message
    assert "output_field 'same'" in message


def test_output_field_colliding_with_declared_input_refused() -> None:
    message = _refused(_all({"a": _text("policy")}, required_input_fields=["policy"]))
    assert "output_field 'policy'" in message
    assert "required_input_fields" in message
    message = _refused(_select({"A": {"content": "x", "format": "text"}}, output_field="kind"))
    assert "key_field 'kind'" in message
    assert "output_field" in message


def test_output_field_must_be_a_field_name() -> None:
    assert "not a valid Python identifier" in _refused(_all({"a": _text("not a name")}))
    assert "not a valid Python identifier" in _refused(_select({"A": {"content": "x", "format": "text"}}, output_field="not a name"))


def test_select_requires_key_field_and_output_field() -> None:
    files = {"A": {"content": "x", "format": "text"}}
    assert "mode 'select' requires key_field" in _refused({"schema": OBSERVED, "mode": "select", "output_field": "out", "files": files})
    assert "mode 'select' requires output_field" in _refused({"schema": OBSERVED, "mode": "select", "key_field": "kind", "files": files})
    message = _refused(_select(files, key_field="  "))
    assert "key_field" in message
    assert "must not be empty" in message


def test_all_requires_per_entry_output_field() -> None:
    message = _refused(_all({"policy": {"content": "x", "format": "text"}}))
    assert "files entry 'policy'" in message
    assert "needs an output_field" in message


@pytest.mark.parametrize(
    ("extra", "fragment"),
    [
        ({"on_miss": "null"}, "on_miss applies only to mode 'select'"),
        ({"key_field": "kind"}, "key_field applies only to mode 'select'"),
        ({"output_field": "guidance"}, "top-level output_field applies only to mode 'select'"),
        ({"default_content": "x"}, "default_content applies only to mode 'select'"),
    ],
)
def test_wrong_mode_options_refused(extra: dict[str, Any], fragment: str) -> None:
    assert fragment in _refused(_all({"a": _text()}, **extra))


def test_entry_output_field_refused_in_select() -> None:
    message = _refused(_select({"A": _text()}))
    assert "files entry 'A'" in message
    assert "output_field applies only to mode 'all'" in message


def test_default_content_iff_on_miss_default() -> None:
    files = {"A": {"content": "x", "format": "text"}}
    assert "on_miss is 'default' but default_content is not set" in _refused(_select(files, on_miss="default"))
    for on_miss in ("fail", "null"):
        assert "default_content applies only when on_miss is 'default'" in _refused(_select(files, on_miss=on_miss, default_content="x"))
    assert "default_content applies only when on_miss is 'default'" in _refused(_select(files, default_content="x"))
    cfg = FileInjectConfig.model_validate(_select(files, on_miss="default", default_content={"a": 1}))
    assert cfg.default_content == {"a": 1}
    # A null default is on_miss: null; None means "not set" so a dumped config validates again.
    assert "use on_miss: null" in _refused(_select(files, on_miss="default", default_content=None))
    assert FileInjectConfig.model_validate(cfg.model_dump()) == cfg
    all_cfg = FileInjectConfig.model_validate(_all({"a": _text()}))
    assert FileInjectConfig.model_validate(all_cfg.model_dump()) == all_cfg


def test_on_miss_defaults_to_fail_in_select() -> None:
    cfg = FileInjectConfig.model_validate(_select({"A": {"content": "x", "format": "text"}}))
    assert cfg.on_miss is None
    assert cfg.effective_on_miss == "fail"
    assert FileInjectConfig.model_validate(_select({"A": {"content": "x", "format": "text"}}, on_miss="null")).effective_on_miss == "null"


@pytest.mark.parametrize("name", [1, True, 2.5, None])
def test_non_string_entry_name_refused(name: object) -> None:
    message = _refused(_select({name: {"content": "x", "format": "text"}}))
    assert "entry names must be quoted strings" in message
    assert repr(name) in message


def test_empty_entry_name_refused() -> None:
    message = _refused(_select({"": {"content": "x", "format": "text"}}))
    assert "entry name must not be empty" in message


@pytest.mark.parametrize("name", [" A", "A ", "A\n"])
def test_entry_name_with_surrounding_whitespace_refused(name: str) -> None:
    message = _refused(_select({name: {"content": "x", "format": "text"}}))
    assert "leading or trailing whitespace" in message
    assert repr(name) in message


def test_entry_and_node_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(file_inject, "MAX_FILE_INJECT_ENTRY_BYTES", 12)
    monkeypatch.setattr(file_inject, "MAX_FILE_INJECT_NODE_BYTES", 20)
    # Canonical size of a str is its bytes plus two quotes.
    FileInjectConfig.model_validate(_all({"a": {"content": "x" * 10, "format": "text", "output_field": "out"}}))
    message = _refused(_all({"big": {"content": "x" * 11, "format": "text", "output_field": "out"}}))
    assert "files entry 'big'" in message
    assert "12-byte per-entry limit" in message
    # The cap measures the parsed value, not the text: whitespace is free.
    FileInjectConfig.model_validate(_all({"a": {"content": "[1,   2,\n\n   3]", "format": "json", "output_field": "out"}}))

    two = {
        "a": {"content": "x" * 8, "format": "text", "output_field": "a_out"},
        "b": {"content": "y" * 8, "format": "text", "output_field": "b_out"},
    }
    FileInjectConfig.model_validate(_all(two))
    three = {**two, "c": {"content": "z", "format": "text", "output_field": "c_out"}}
    message = _refused(_all(three))
    assert "20-byte per-node limit" in message
    # Both modes count every entry.
    select_three = {name: {"content": entry["content"], "format": "text"} for name, entry in three.items()}
    assert "20-byte per-node limit" in _refused(_select(select_three))


def test_parse_injected_content_raises_value_error_without_naming_an_entry() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_injected_content("{", fmt="json", data_key=None, delimiter=",")
    assert parse_injected_content("a;b\n1;2\n", fmt="csv", data_key=None, delimiter=";") == [{"a": "1", "b": "2"}]


def test_from_dict_reports_the_entry_as_a_plugin_config_error() -> None:
    options = _all({"rubric": {"content": "{", "format": "json", "output_field": "out"}})
    with pytest.raises(PluginConfigError, match="files entry 'rubric'"):
        FileInjectConfig.from_dict(options, plugin_name="file_inject")


def test_select_declares_its_key_field_as_input() -> None:
    cfg = FileInjectConfig.model_validate(_select({"A": {"content": "x", "format": "text"}}, required_input_fields=["other"]))
    assert cfg.declared_input_fields == frozenset({"kind", "other"})
    assert FileInjectConfig.model_validate(_all({"a": _text()})).declared_input_fields == frozenset()


# --- declared types ---------------------------------------------------------


def _added(options: dict[str, Any]) -> tuple[FieldDefinition, ...]:
    return added_output_fields(FileInjectConfig.model_validate(options))


def test_all_mode_types() -> None:
    fields = _added(
        _all(
            {
                "policy": {"content": "p", "format": "text", "output_field": "policy_text"},
                "rubric": {"content": '{"a": 1}', "format": "json", "output_field": "rubric"},
                "lines": {"content": "1\n2\n", "format": "jsonl", "output_field": "lines"},
                "examples": {"content": "k\na\n", "format": "csv", "output_field": "examples"},
                "nothing": {"content": "null", "format": "json", "output_field": "nothing"},
            }
        )
    )
    assert fields == (
        FieldDefinition("examples", "any"),
        FieldDefinition("lines", "any"),
        FieldDefinition("nothing", "any", nullable=True),
        FieldDefinition("policy_text", "str"),
        FieldDefinition("rubric", "any"),
    )


def test_select_types() -> None:
    text = {"A": {"content": "a", "format": "text"}, "B": {"content": "b", "format": "text"}}
    mixed = {**text, "C": {"content": "[1]", "format": "json"}}
    assert _added(_select(text)) == (FieldDefinition("guidance", "str"),)
    assert _added(_select(text, on_miss="fail")) == (FieldDefinition("guidance", "str"),)
    assert _added(_select(mixed)) == (FieldDefinition("guidance", "any"),)
    assert _added(_select(text, on_miss="null")) == (FieldDefinition("guidance", "str", nullable=True),)
    assert _added(_select(mixed, on_miss="null")) == (FieldDefinition("guidance", "any", nullable=True),)
    assert _added(_select(text, on_miss="default", default_content="fallback")) == (FieldDefinition("guidance", "str"),)
    assert _added(_select(text, on_miss="default", default_content=3)) == (FieldDefinition("guidance", "any"),)


def _declared(spec: str | dict[str, Any], files: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {**_select(files, **extra), "schema": {"mode": "flexible", "fields": [spec]}}


def _field(field_type: str, *, required: bool = True, nullable: bool = False) -> dict[str, Any]:
    return {"name": "guidance", "type": field_type, "required": required, "nullable": nullable}


def test_a_true_schema_declaration_is_honored_whole() -> None:
    text = {"A": {"content": "a", "format": "text"}}
    assert _added(_declared("guidance: str", text)) == (FieldDefinition("guidance", "str"),)
    # A nullable declaration over a never-null field is weaker but true.
    assert _added(_declared(_field("str", nullable=True), text)) == (FieldDefinition("guidance", "str", nullable=True),)
    assert _added(_declared(_field("str", nullable=True), text, on_miss="null")) == (FieldDefinition("guidance", "str", nullable=True),)
    # 'any' is an explicit abstention.
    assert _added(_declared("guidance: any", text)) == (FieldDefinition("guidance", "any"),)
    # A json scalar is still exactly one runtime type, so a concrete declaration can be true.
    assert _added(_declared("guidance: int", {"A": {"content": "3", "format": "json"}})) == (FieldDefinition("guidance", "int"),)


def test_a_refuted_schema_declaration_is_refused() -> None:
    text = {"A": {"content": "a", "format": "text"}}
    message = _refused(_declared("guidance: int", text))
    assert "schema declares 'guidance' as int" in message
    assert "['str']" in message
    message = _refused(_declared("guidance: str", {**text, "B": {"content": "k\na\n", "format": "csv"}}))
    assert "schema declares 'guidance' as str" in message
    assert "['list', 'str']" in message
    message = _refused(_declared("guidance: str", text, on_miss="default", default_content=3))
    assert "['int', 'str']" in message
    message = _refused(_declared("guidance: str", text, on_miss="null"))
    assert "schema declares 'guidance' as never null" in message
    assert "on_miss is 'null'" in message
    # The string form's '?' means optional AND nullable, and a created field is guaranteed.
    message = _refused(_declared("guidance: str?", text))
    assert "schema declares 'guidance' optional" in message


def test_all_mode_declaration_is_checked_per_entry() -> None:
    options = _all({"policy": {"content": "p", "format": "text", "output_field": "guidance"}})
    assert _added({**options, "schema": {"mode": "flexible", "fields": ["guidance: str"]}}) == (FieldDefinition("guidance", "str"),)
    message = _refused({**options, "schema": {"mode": "flexible", "fields": ["guidance: float"]}})
    assert "schema declares 'guidance' as float" in message
