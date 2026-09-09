"""Tests for contracts enforcement script."""

import inspect
from pathlib import Path
from unittest.mock import patch

from scripts.check_contracts import (
    CENSUS_METHOD,
    SOFT_MAPPING_FORMS,
    CensusDrift,
    CensusSite,
    DictAliasIndex,
    FieldCoverageViolation,
    FieldMappingViolation,
    FieldMappingVisitor,
    HardcodeLiteralVisitor,
    HardcodeViolation,
    ImportIndex,
    SettingsAccessVisitor,
    SettingsViolation,
    census_file,
    census_totals,
    check_field_name_mappings,
    check_from_settings_coverage,
    check_hardcode_documentation,
    check_settings_alignment,
    check_soft_mapping_census,
    compare_census,
    extract_from_settings_accesses,
    extract_from_settings_field_mappings,
    extract_from_settings_hardcodes,
    find_dict_patterns_in_file,
    find_dict_violations,
    find_settings_classes,
    find_type_definitions,
    get_settings_class_fields,
    load_census,
    load_whitelist,
    tabulate_census,
    write_census,
)


def test_finds_dataclass_definitions(tmp_path: Path) -> None:
    """Finds @dataclass decorated classes."""
    test_file = tmp_path / "test.py"
    test_file.write_text("""
from dataclasses import dataclass

@dataclass
class MyType:
    name: str
""")

    definitions = find_type_definitions(test_file)
    assert len(definitions) == 1
    assert definitions[0][0] == "MyType"
    assert definitions[0][2] == "dataclass"


def test_finds_dataclass_with_args(tmp_path: Path) -> None:
    """Finds @dataclass(frozen=True) decorated classes."""
    test_file = tmp_path / "test.py"
    test_file.write_text("""
from dataclasses import dataclass

@dataclass(frozen=True)
class FrozenType:
    value: int
""")

    definitions = find_type_definitions(test_file)
    assert len(definitions) == 1
    assert definitions[0][0] == "FrozenType"
    assert definitions[0][2] == "dataclass"


def test_finds_enum_definitions(tmp_path: Path) -> None:
    """Finds Enum subclasses."""
    test_file = tmp_path / "test.py"
    test_file.write_text("""
from enum import Enum

class MyEnum(Enum):
    A = "a"
""")

    definitions = find_type_definitions(test_file)
    assert len(definitions) == 1
    assert definitions[0][0] == "MyEnum"
    assert definitions[0][2] == "Enum"


def test_finds_typeddict_definitions(tmp_path: Path) -> None:
    """Finds TypedDict subclasses."""
    test_file = tmp_path / "test.py"
    test_file.write_text("""
from typing import TypedDict

class MyDict(TypedDict):
    name: str
    value: int
""")

    definitions = find_type_definitions(test_file)
    assert len(definitions) == 1
    assert definitions[0][0] == "MyDict"
    assert definitions[0][2] == "TypedDict"


def test_finds_namedtuple_definitions(tmp_path: Path) -> None:
    """Finds NamedTuple subclasses."""
    test_file = tmp_path / "test.py"
    test_file.write_text("""
from typing import NamedTuple

class MyTuple(NamedTuple):
    x: int
    y: int
""")

    definitions = find_type_definitions(test_file)
    assert len(definitions) == 1
    assert definitions[0][0] == "MyTuple"
    assert definitions[0][2] == "NamedTuple"


def test_finds_qualified_type_definitions(tmp_path: Path) -> None:
    """Finds valid qualified decorator and base-class forms."""
    test_file = tmp_path / "test.py"
    test_file.write_text("""
import dataclasses
import enum
import typing

@dataclasses.dataclass
class QualifiedData:
    name: str


@dataclasses.dataclass(frozen=True)
class QualifiedFrozen:
    value: int


class QualifiedEnum(enum.Enum):
    A = "a"


class QualifiedDict(typing.TypedDict):
    name: str


class QualifiedTuple(typing.NamedTuple):
    x: int
""")

    definitions = find_type_definitions(test_file)

    assert {(name, kind) for name, _, kind in definitions} == {
        ("QualifiedData", "dataclass"),
        ("QualifiedFrozen", "dataclass"),
        ("QualifiedEnum", "Enum"),
        ("QualifiedDict", "TypedDict"),
        ("QualifiedTuple", "NamedTuple"),
    }


def test_ignores_pydantic_basemodel(tmp_path: Path) -> None:
    """Does not flag Pydantic BaseModel classes."""
    test_file = tmp_path / "test.py"
    test_file.write_text("""
from pydantic import BaseModel

class MyModel(BaseModel):
    name: str
""")

    definitions = find_type_definitions(test_file)
    assert len(definitions) == 0


def test_ignores_plugin_schema(tmp_path: Path) -> None:
    """Does not flag PluginSchema classes."""
    test_file = tmp_path / "test.py"
    test_file.write_text("""
from elspeth.contracts import PluginSchema

class MyPluginConfig(PluginSchema):
    setting: str
""")

    definitions = find_type_definitions(test_file)
    assert len(definitions) == 0


def test_finds_multiple_definitions(tmp_path: Path) -> None:
    """Finds multiple type definitions in a single file."""
    test_file = tmp_path / "test.py"
    test_file.write_text("""
from dataclasses import dataclass
from enum import Enum
from typing import TypedDict

@dataclass
class DataType:
    value: int

class StatusEnum(Enum):
    ACTIVE = "active"

class ConfigDict(TypedDict):
    name: str
""")

    definitions = find_type_definitions(test_file)
    assert len(definitions) == 3
    names = {d[0] for d in definitions}
    assert names == {"DataType", "StatusEnum", "ConfigDict"}


def test_whitelist_loading(tmp_path: Path) -> None:
    """Loads whitelist from YAML."""
    whitelist_file = tmp_path / ".contracts-whitelist.yaml"
    whitelist_file.write_text("""
allowed_external_types:
  - "foo/bar:MyType"
""")

    whitelist, entries = load_whitelist(whitelist_file)
    assert "foo/bar:MyType" in whitelist["types"]
    assert len(entries) == 1
    assert entries[0].value == "foo/bar:MyType"
    assert entries[0].category == "type"


def test_whitelist_loading_empty_file(tmp_path: Path) -> None:
    """Handles empty whitelist file."""
    whitelist_file = tmp_path / ".contracts-whitelist.yaml"
    whitelist_file.write_text("")

    whitelist, entries = load_whitelist(whitelist_file)
    assert whitelist == {"types": set(), "dicts": set()}
    assert entries == []


def test_whitelist_loading_nonexistent_file(tmp_path: Path) -> None:
    """Handles missing whitelist file."""
    whitelist_file = tmp_path / "nonexistent.yaml"

    whitelist, entries = load_whitelist(whitelist_file)
    assert whitelist == {"types": set(), "dicts": set()}
    assert entries == []


def test_whitelist_loading_multiple_entries(tmp_path: Path) -> None:
    """Loads multiple whitelist entries."""
    whitelist_file = tmp_path / ".contracts-whitelist.yaml"
    whitelist_file.write_text("""
allowed_external_types:
  - "module/a:TypeA"
  - "module/b:TypeB"
  - "module/c:TypeC"
""")

    whitelist, entries = load_whitelist(whitelist_file)
    assert len(whitelist["types"]) == 3
    assert "module/a:TypeA" in whitelist["types"]
    assert "module/b:TypeB" in whitelist["types"]
    assert "module/c:TypeC" in whitelist["types"]
    assert len(entries) == 3
    assert all(e.category == "type" for e in entries)


def test_handles_syntax_errors(tmp_path: Path) -> None:
    """Gracefully handles files with syntax errors."""
    test_file = tmp_path / "broken.py"
    test_file.write_text("def broken(\n")  # Invalid syntax

    definitions = find_type_definitions(test_file)
    assert definitions == []


def test_handles_unicode_errors(tmp_path: Path) -> None:
    """Gracefully handles files with encoding issues."""
    test_file = tmp_path / "binary.py"
    test_file.write_bytes(b"\x80\x81\x82")  # Invalid UTF-8

    definitions = find_type_definitions(test_file)
    assert definitions == []


def test_import_index_does_not_match_substring_module_names(tmp_path: Path) -> None:
    defining_file = tmp_path / "core" / "foo.py"
    defining_file.parent.mkdir(parents=True)
    defining_file.write_text("""
from dataclasses import dataclass

@dataclass
class Foo:
    value: str
""")
    using_file = tmp_path / "plugins" / "use.py"
    using_file.parent.mkdir(parents=True)
    using_file.write_text("""
from elspeth.core.foobar import Foo

value: Foo
""")

    index = ImportIndex.build(tmp_path)

    assert index.find_cross_boundary_usages(tmp_path, "Foo", defining_file) == []


def test_import_index_finds_qualified_import_alias_usage(tmp_path: Path) -> None:
    defining_file = tmp_path / "core" / "foo.py"
    defining_file.parent.mkdir(parents=True)
    defining_file.write_text("""
from dataclasses import dataclass

@dataclass
class Foo:
    value: str
""")
    using_file = tmp_path / "plugins" / "use.py"
    using_file.parent.mkdir(parents=True)
    using_file.write_text("""
import elspeth.core.foo as foo

value: foo.Foo
""")

    index = ImportIndex.build(tmp_path)

    assert index.find_cross_boundary_usages(tmp_path, "Foo", defining_file) == [using_file]


def test_import_index_finds_from_import_module_attribute_usage(tmp_path: Path) -> None:
    defining_file = tmp_path / "core" / "foo.py"
    defining_file.parent.mkdir(parents=True)
    defining_file.write_text("""
from dataclasses import dataclass

@dataclass
class Foo:
    value: str
""")
    using_file = tmp_path / "plugins" / "use.py"
    using_file.parent.mkdir(parents=True)
    using_file.write_text("""
from elspeth.core import foo

value: foo.Foo
""")

    index = ImportIndex.build(tmp_path)

    assert index.find_cross_boundary_usages(tmp_path, "Foo", defining_file) == [using_file]


def test_dict_pattern_guard_detects_qualified_typing_any(tmp_path: Path) -> None:
    test_file = tmp_path / "qualified_any.py"
    test_file.write_text("""
import typing


def build(payload: dict[str, typing.Any]) -> dict[str, typing.Any]:
    return payload
""")

    violations = find_dict_violations(test_file, whitelist=set(), matched_entries={})

    assert [(violation.context, violation.param_name) for violation in violations] == [
        ("build", "payload"),
        ("build", "return"),
    ]
    assert find_dict_patterns_in_file(test_file) == [
        f"{test_file}:build:payload",
        f"{test_file}:build:return",
    ]


def test_dict_pattern_guard_reports_parameter_annotation_lines(tmp_path: Path) -> None:
    test_file = tmp_path / "parameter_lines.py"
    test_file.write_text(
        "\n".join(
            [
                "from typing import Any",
                "",
                "def build(",
                "    payload: dict[str, Any],",
                "    *,",
                "    batches: list[dict[str, Any]],",
                ") -> None:",
                "    pass",
                "",
            ]
        )
    )

    violations = find_dict_violations(test_file, whitelist=set(), matched_entries={})

    assert [(violation.param_name, violation.line) for violation in violations] == [
        ("payload", 4),
        ("batches (list)", 6),
    ]


def test_dict_pattern_guard_uses_the_owned_ast_arg_contract() -> None:
    source = inspect.getsource(find_dict_violations)

    assert "hasattr" not in source


# =============================================================================
# Settings Alignment Tests
# =============================================================================


def test_find_settings_classes_finds_settings_suffix(tmp_path: Path) -> None:
    """Finds classes ending in 'Settings'."""
    test_file = tmp_path / "config.py"
    test_file.write_text("""
from pydantic import BaseModel

class RetrySettings(BaseModel):
    max_attempts: int

class RateLimitSettings(BaseModel):
    enabled: bool

class SomeConfig(BaseModel):  # Should NOT match - doesn't end in 'Settings'
    value: str
""")

    settings = find_settings_classes(test_file)
    names = {s[0] for s in settings}
    assert names == {"RetrySettings", "RateLimitSettings"}
    assert "SomeConfig" not in names


def test_find_settings_classes_returns_line_numbers(tmp_path: Path) -> None:
    """Returns correct line numbers for Settings classes."""
    test_file = tmp_path / "config.py"
    test_file.write_text("""
class FirstSettings:
    pass

class SecondSettings:
    pass
""")

    settings = find_settings_classes(test_file)
    # Line 2 and Line 5 (accounting for leading newline)
    assert len(settings) == 2
    assert settings[0] == ("FirstSettings", 2)
    assert settings[1] == ("SecondSettings", 5)


def test_find_settings_classes_handles_empty_file(tmp_path: Path) -> None:
    """Handles empty files gracefully."""
    test_file = tmp_path / "empty.py"
    test_file.write_text("")

    settings = find_settings_classes(test_file)
    assert settings == []


def test_find_settings_classes_handles_syntax_errors(tmp_path: Path) -> None:
    """Handles syntax errors gracefully."""
    test_file = tmp_path / "broken.py"
    test_file.write_text("class BrokenSettings(\n")  # Invalid syntax

    settings = find_settings_classes(test_file)
    assert settings == []


def test_check_settings_alignment_passes_with_mapping(tmp_path: Path) -> None:
    """Settings class with Runtime counterpart passes."""
    test_file = tmp_path / "config.py"
    test_file.write_text("""
class RetrySettings:
    pass
""")

    # Mock the alignment module to have RetrySettings in SETTINGS_TO_RUNTIME
    with (
        patch(
            "elspeth.contracts.config.alignment.SETTINGS_TO_RUNTIME",
            {"RetrySettings": "RuntimeRetryConfig"},
        ),
        patch("elspeth.contracts.config.alignment.EXEMPT_SETTINGS", set()),
    ):
        violations = check_settings_alignment(test_file)
        assert violations == []


def test_check_settings_alignment_passes_with_exempt(tmp_path: Path) -> None:
    """Settings class in EXEMPT_SETTINGS passes."""
    test_file = tmp_path / "config.py"
    test_file.write_text("""
class SourceSettings:
    pass
""")

    # Mock the alignment module to have SourceSettings in EXEMPT_SETTINGS
    with (
        patch("elspeth.contracts.config.alignment.SETTINGS_TO_RUNTIME", {}),
        patch("elspeth.contracts.config.alignment.EXEMPT_SETTINGS", {"SourceSettings"}),
    ):
        violations = check_settings_alignment(test_file)
        assert violations == []


def test_check_settings_alignment_detects_orphaned(tmp_path: Path) -> None:
    """Orphaned Settings class is detected as violation."""
    test_file = tmp_path / "config.py"
    test_file.write_text("""
class OrphanedSettings:
    pass
""")

    # Mock empty mappings - OrphanedSettings is not mapped or exempt
    with (
        patch("elspeth.contracts.config.alignment.SETTINGS_TO_RUNTIME", {}),
        patch("elspeth.contracts.config.alignment.EXEMPT_SETTINGS", set()),
    ):
        violations = check_settings_alignment(test_file)
        assert len(violations) == 1
        assert violations[0].class_name == "OrphanedSettings"
        assert violations[0].line == 2


def test_check_settings_alignment_multiple_classes(tmp_path: Path) -> None:
    """Mixed scenario: mapped, exempt, and orphaned."""
    test_file = tmp_path / "config.py"
    test_file.write_text("""
class MappedSettings:
    pass

class ExemptSettings:
    pass

class OrphanedSettings:
    pass
""")

    with (
        patch(
            "elspeth.contracts.config.alignment.SETTINGS_TO_RUNTIME",
            {"MappedSettings": "RuntimeMappedConfig"},
        ),
        patch("elspeth.contracts.config.alignment.EXEMPT_SETTINGS", {"ExemptSettings"}),
    ):
        violations = check_settings_alignment(test_file)
        # Only OrphanedSettings should be flagged
        assert len(violations) == 1
        assert violations[0].class_name == "OrphanedSettings"


def test_check_settings_alignment_uses_real_mappings() -> None:
    """Integration test: actual core/config.py with real mappings.

    This verifies that the current codebase passes the check.
    If this fails, a new Settings class was added without updating
    SETTINGS_TO_RUNTIME or EXEMPT_SETTINGS in alignment.py.
    """
    import pytest

    config_path = Path("src/elspeth/core/config.py")
    if not config_path.exists():
        pytest.skip("Running from different directory - core/config.py not found")

    violations = check_settings_alignment(config_path)
    assert violations == [], (
        f"Orphaned Settings classes found: {[v.class_name for v in violations]}. "
        "Add to SETTINGS_TO_RUNTIME or EXEMPT_SETTINGS in contracts/config/alignment.py"
    )


def test_settings_violation_dataclass() -> None:
    """SettingsViolation dataclass holds expected fields."""
    violation = SettingsViolation(
        class_name="TestSettings",
        file="/path/to/config.py",
        line=42,
    )
    assert violation.class_name == "TestSettings"
    assert violation.file == "/path/to/config.py"
    assert violation.line == 42


# =============================================================================
# Field Coverage Tests (from_settings() method checks)
# =============================================================================


def test_settings_access_visitor_finds_direct_access() -> None:
    """SettingsAccessVisitor finds settings.X accesses."""
    import ast

    code = """
def from_settings(cls, settings):
    return cls(
        field_a=settings.field_a,
        field_b=settings.field_b,
    )
"""
    tree = ast.parse(code)
    visitor = SettingsAccessVisitor("settings")
    visitor.visit(tree)

    assert visitor.accessed_fields == {"field_a", "field_b"}


def test_settings_access_visitor_finds_chained_access() -> None:
    """SettingsAccessVisitor finds nested access like settings.field.method()."""
    import ast

    code = """
def from_settings(cls, settings):
    # Access settings.nested_field and call a method on it
    value = settings.nested_field.some_method()
    return cls(value=value)
"""
    tree = ast.parse(code)
    visitor = SettingsAccessVisitor("settings")
    visitor.visit(tree)

    # Should capture just the direct attribute, not the chained ones
    assert "nested_field" in visitor.accessed_fields


def test_settings_access_visitor_handles_different_param_names() -> None:
    """SettingsAccessVisitor works with different parameter names."""
    import ast

    code = """
def from_settings(cls, config):
    return cls(field=config.field)
"""
    tree = ast.parse(code)
    visitor = SettingsAccessVisitor("config")
    visitor.visit(tree)

    assert visitor.accessed_fields == {"field"}


def test_extract_from_settings_accesses_finds_method(tmp_path: Path) -> None:
    """extract_from_settings_accesses finds from_settings method."""
    runtime_file = tmp_path / "runtime.py"
    runtime_file.write_text("""
@dataclass
class RuntimeTestConfig:
    field_a: int
    field_b: str

    @classmethod
    def from_settings(cls, settings):
        return cls(
            field_a=settings.field_a,
            field_b=settings.field_b,
        )
""")

    result = extract_from_settings_accesses(runtime_file)

    assert "RuntimeTestConfig" in result
    assert result["RuntimeTestConfig"] == {"field_a", "field_b"}


def test_extract_from_settings_accesses_handles_no_method(tmp_path: Path) -> None:
    """extract_from_settings_accesses handles classes without from_settings."""
    runtime_file = tmp_path / "runtime.py"
    runtime_file.write_text("""
@dataclass
class RuntimeTestConfig:
    field_a: int

    @classmethod
    def default(cls):
        return cls(field_a=1)
""")

    result = extract_from_settings_accesses(runtime_file)

    # Class exists but has no from_settings, so not in result
    assert "RuntimeTestConfig" not in result


def test_extract_from_settings_accesses_multiple_classes(tmp_path: Path) -> None:
    """extract_from_settings_accesses handles multiple Runtime classes."""
    runtime_file = tmp_path / "runtime.py"
    runtime_file.write_text("""
@dataclass
class RuntimeRetryConfig:
    max_attempts: int

    @classmethod
    def from_settings(cls, settings):
        return cls(max_attempts=settings.max_attempts)


@dataclass
class RuntimeCheckpointConfig:
    enabled: bool
    frequency: int

    @classmethod
    def from_settings(cls, settings):
        return cls(
            enabled=settings.enabled,
            frequency=settings.frequency,
        )
""")

    result = extract_from_settings_accesses(runtime_file)

    assert len(result) == 2
    assert result["RuntimeRetryConfig"] == {"max_attempts"}
    assert result["RuntimeCheckpointConfig"] == {"enabled", "frequency"}


def test_get_settings_class_fields_extracts_fields(tmp_path: Path) -> None:
    """get_settings_class_fields extracts field names from Settings class."""
    config_file = tmp_path / "config.py"
    config_file.write_text("""
from pydantic import BaseModel, Field

class TestSettings(BaseModel):
    field_a: int = Field(default=1)
    field_b: str = "default"
    field_c: bool
""")

    fields = get_settings_class_fields(config_file, "TestSettings")

    assert fields == {"field_a", "field_b", "field_c"}


def test_get_settings_class_fields_returns_empty_for_missing_class(tmp_path: Path) -> None:
    """get_settings_class_fields returns empty set for non-existent class."""
    config_file = tmp_path / "config.py"
    config_file.write_text("""
class OtherClass:
    pass
""")

    fields = get_settings_class_fields(config_file, "TestSettings")

    assert fields == set()


def test_check_from_settings_coverage_passes_full_coverage(tmp_path: Path) -> None:
    """check_from_settings_coverage passes when all fields are accessed."""
    config_file = tmp_path / "config.py"
    config_file.write_text("""
class TestSettings:
    field_a: int
    field_b: str
""")

    runtime_file = tmp_path / "runtime.py"
    runtime_file.write_text("""
class RuntimeTestConfig:
    @classmethod
    def from_settings(cls, settings):
        return cls(
            field_a=settings.field_a,
            field_b=settings.field_b,
        )
""")

    with patch(
        "elspeth.contracts.config.alignment.SETTINGS_TO_RUNTIME",
        {"TestSettings": "RuntimeTestConfig"},
    ):
        violations = check_from_settings_coverage(config_file, runtime_file)

    assert violations == []


def test_check_from_settings_coverage_detects_orphan(tmp_path: Path) -> None:
    """check_from_settings_coverage detects orphaned Settings fields."""
    config_file = tmp_path / "config.py"
    config_file.write_text("""
class TestSettings:
    field_a: int
    field_b: str
    orphaned_field: float
""")

    runtime_file = tmp_path / "runtime.py"
    runtime_file.write_text("""
class RuntimeTestConfig:
    @classmethod
    def from_settings(cls, settings):
        return cls(
            field_a=settings.field_a,
            field_b=settings.field_b,
            # orphaned_field is NOT accessed!
        )
""")

    with patch(
        "elspeth.contracts.config.alignment.SETTINGS_TO_RUNTIME",
        {"TestSettings": "RuntimeTestConfig"},
    ):
        violations = check_from_settings_coverage(config_file, runtime_file)

    assert len(violations) == 1
    assert violations[0].settings_class == "TestSettings"
    assert violations[0].runtime_class == "RuntimeTestConfig"
    assert violations[0].orphaned_field == "orphaned_field"


def test_check_from_settings_coverage_detects_multiple_orphans(tmp_path: Path) -> None:
    """check_from_settings_coverage detects multiple orphaned fields."""
    config_file = tmp_path / "config.py"
    config_file.write_text("""
class TestSettings:
    used_field: int
    orphan_a: str
    orphan_b: float
""")

    runtime_file = tmp_path / "runtime.py"
    runtime_file.write_text("""
class RuntimeTestConfig:
    @classmethod
    def from_settings(cls, settings):
        return cls(used_field=settings.used_field)
""")

    with patch(
        "elspeth.contracts.config.alignment.SETTINGS_TO_RUNTIME",
        {"TestSettings": "RuntimeTestConfig"},
    ):
        violations = check_from_settings_coverage(config_file, runtime_file)

    assert len(violations) == 2
    orphan_names = {v.orphaned_field for v in violations}
    assert orphan_names == {"orphan_a", "orphan_b"}


def test_check_from_settings_coverage_skips_unmapped_classes(tmp_path: Path) -> None:
    """check_from_settings_coverage skips classes not in SETTINGS_TO_RUNTIME."""
    config_file = tmp_path / "config.py"
    config_file.write_text("""
class UnmappedSettings:
    some_field: int
""")

    runtime_file = tmp_path / "runtime.py"
    runtime_file.write_text("""
class RuntimeUnmappedConfig:
    @classmethod
    def from_settings(cls, settings):
        # Doesn't access any fields
        return cls()
""")

    # Empty mapping - no Settings classes are mapped
    with patch(
        "elspeth.contracts.config.alignment.SETTINGS_TO_RUNTIME",
        {},
    ):
        violations = check_from_settings_coverage(config_file, runtime_file)

    # No violations because the class isn't in the mapping
    assert violations == []


def test_check_from_settings_coverage_real_codebase() -> None:
    """Integration test: actual codebase has full field coverage.

    This verifies that the current codebase passes the check.
    If this fails, a Settings field was added but not accessed
    in the corresponding from_settings() method.
    """
    import pytest

    config_path = Path("src/elspeth/core/config.py")
    runtime_path = Path("src/elspeth/contracts/config/runtime.py")

    if not config_path.exists() or not runtime_path.exists():
        pytest.skip("Running from different directory - required files not found")

    violations = check_from_settings_coverage(config_path, runtime_path)
    assert violations == [], (
        f"Settings field coverage violations found: "
        f"{[(v.settings_class, v.orphaned_field) for v in violations]}. "
        f"Access these fields in from_settings() or remove them from the Settings class."
    )


def test_field_coverage_violation_dataclass() -> None:
    """FieldCoverageViolation dataclass holds expected fields."""
    violation = FieldCoverageViolation(
        settings_class="TestSettings",
        runtime_class="RuntimeTestConfig",
        orphaned_field="orphaned",
        file="/path/to/runtime.py",
        line=42,
    )
    assert violation.settings_class == "TestSettings"
    assert violation.runtime_class == "RuntimeTestConfig"
    assert violation.orphaned_field == "orphaned"
    assert violation.file == "/path/to/runtime.py"
    assert violation.line == 42


# =============================================================================
# Field Mapping Validation Tests (check_field_name_mappings)
# =============================================================================


def test_field_mapping_visitor_finds_direct_mappings() -> None:
    """FieldMappingVisitor finds runtime_field=settings.settings_field patterns."""
    import ast

    code = """
def from_settings(cls, settings):
    return cls(
        field_a=settings.field_a,
        field_b=settings.field_b,
    )
"""
    tree = ast.parse(code)
    visitor = FieldMappingVisitor("settings")
    visitor.visit(tree)

    assert len(visitor.field_mappings) == 2
    assert ("field_a", "field_a") in visitor.field_mappings
    assert ("field_b", "field_b") in visitor.field_mappings


def test_field_mapping_visitor_finds_renamed_mappings() -> None:
    """FieldMappingVisitor captures renamed field mappings."""
    import ast

    code = """
def from_settings(cls, settings):
    return cls(
        base_delay=settings.initial_delay_seconds,
        max_delay=settings.max_delay_seconds,
    )
"""
    tree = ast.parse(code)
    visitor = FieldMappingVisitor("settings")
    visitor.visit(tree)

    assert len(visitor.field_mappings) == 2
    assert ("base_delay", "initial_delay_seconds") in visitor.field_mappings
    assert ("max_delay", "max_delay_seconds") in visitor.field_mappings


def test_field_mapping_visitor_ignores_non_settings_values() -> None:
    """FieldMappingVisitor ignores values that aren't settings.X."""
    import ast

    code = """
def from_settings(cls, settings):
    return cls(
        field_a=settings.field_a,
        field_b=42,  # Not settings.X
        field_c="constant",  # Not settings.X
        field_d=some_func(),  # Not settings.X
    )
"""
    tree = ast.parse(code)
    visitor = FieldMappingVisitor("settings")
    visitor.visit(tree)

    # Only field_a should be captured
    assert len(visitor.field_mappings) == 1
    assert ("field_a", "field_a") in visitor.field_mappings


def test_field_mapping_visitor_handles_different_param_names() -> None:
    """FieldMappingVisitor works with different parameter names."""
    import ast

    code = """
def from_settings(cls, config):
    return cls(field=config.field)
"""
    tree = ast.parse(code)
    visitor = FieldMappingVisitor("config")
    visitor.visit(tree)

    assert visitor.field_mappings == [("field", "field")]


def test_extract_from_settings_field_mappings_finds_method(tmp_path: Path) -> None:
    """extract_from_settings_field_mappings finds mappings in from_settings method."""
    runtime_file = tmp_path / "runtime.py"
    runtime_file.write_text("""
@dataclass
class RuntimeTestConfig:
    field_a: int
    field_b: str

    @classmethod
    def from_settings(cls, settings):
        return cls(
            field_a=settings.field_a,
            field_b=settings.field_b,
        )
""")

    result = extract_from_settings_field_mappings(runtime_file)

    assert "RuntimeTestConfig" in result
    assert ("field_a", "field_a") in result["RuntimeTestConfig"]
    assert ("field_b", "field_b") in result["RuntimeTestConfig"]


def test_extract_from_settings_field_mappings_finds_renames(tmp_path: Path) -> None:
    """extract_from_settings_field_mappings captures renamed fields."""
    runtime_file = tmp_path / "runtime.py"
    runtime_file.write_text("""
@dataclass
class RuntimeRetryConfig:
    base_delay: float
    max_delay: float

    @classmethod
    def from_settings(cls, settings):
        return cls(
            base_delay=settings.initial_delay_seconds,
            max_delay=settings.max_delay_seconds,
        )
""")

    result = extract_from_settings_field_mappings(runtime_file)

    assert "RuntimeRetryConfig" in result
    mappings = result["RuntimeRetryConfig"]
    assert ("base_delay", "initial_delay_seconds") in mappings
    assert ("max_delay", "max_delay_seconds") in mappings


def test_check_field_name_mappings_passes_correct_mapping(tmp_path: Path) -> None:
    """check_field_name_mappings passes when mappings match FIELD_MAPPINGS."""
    runtime_file = tmp_path / "runtime.py"
    runtime_file.write_text("""
@dataclass
class RuntimeTestConfig:
    base_delay: float
    max_delay: float

    @classmethod
    def from_settings(cls, settings):
        return cls(
            base_delay=settings.initial_delay_seconds,
            max_delay=settings.max_delay_seconds,
        )
""")

    with (
        patch(
            "elspeth.contracts.config.alignment.SETTINGS_TO_RUNTIME",
            {"TestSettings": "RuntimeTestConfig"},
        ),
        patch(
            "elspeth.contracts.config.alignment.FIELD_MAPPINGS",
            {
                "TestSettings": {
                    "initial_delay_seconds": "base_delay",
                    "max_delay_seconds": "max_delay",
                }
            },
        ),
    ):
        violations = check_field_name_mappings(runtime_file)

    assert violations == []


def test_check_field_name_mappings_detects_misroute(tmp_path: Path) -> None:
    """check_field_name_mappings detects when settings field maps to wrong runtime field.

    This is the key test case: catching "misrouted" fields where code
    maps a settings field to the wrong runtime field.

    Example: FIELD_MAPPINGS says initial_delay_seconds -> base_delay
    But code has: base_delay=settings.max_delay_seconds (WRONG!)
    """
    runtime_file = tmp_path / "runtime.py"
    # INTENTIONAL MISROUTE: base_delay uses max_delay_seconds instead of initial_delay_seconds
    runtime_file.write_text("""
@dataclass
class RuntimeTestConfig:
    base_delay: float
    max_delay: float

    @classmethod
    def from_settings(cls, settings):
        return cls(
            base_delay=settings.max_delay_seconds,  # WRONG! Should be initial_delay_seconds
            max_delay=settings.max_delay_seconds,   # Correct
        )
""")

    with (
        patch(
            "elspeth.contracts.config.alignment.SETTINGS_TO_RUNTIME",
            {"TestSettings": "RuntimeTestConfig"},
        ),
        patch(
            "elspeth.contracts.config.alignment.FIELD_MAPPINGS",
            {
                "TestSettings": {
                    "initial_delay_seconds": "base_delay",
                    "max_delay_seconds": "max_delay",
                }
            },
        ),
    ):
        violations = check_field_name_mappings(runtime_file)

    assert len(violations) == 1
    v = violations[0]
    assert v.runtime_class == "RuntimeTestConfig"
    assert v.runtime_field == "base_delay"
    assert v.settings_field == "max_delay_seconds"  # What code has (wrong)
    assert v.expected_settings_field == "initial_delay_seconds"  # What it should be


def test_check_field_name_mappings_detects_swapped_fields(tmp_path: Path) -> None:
    """check_field_name_mappings detects when two fields are swapped."""
    runtime_file = tmp_path / "runtime.py"
    # INTENTIONAL BUG: fields are swapped
    runtime_file.write_text("""
@dataclass
class RuntimeTestConfig:
    base_delay: float
    max_delay: float

    @classmethod
    def from_settings(cls, settings):
        return cls(
            base_delay=settings.max_delay_seconds,      # WRONG! Should be initial_delay_seconds
            max_delay=settings.initial_delay_seconds,   # WRONG! Should be max_delay_seconds
        )
""")

    with (
        patch(
            "elspeth.contracts.config.alignment.SETTINGS_TO_RUNTIME",
            {"TestSettings": "RuntimeTestConfig"},
        ),
        patch(
            "elspeth.contracts.config.alignment.FIELD_MAPPINGS",
            {
                "TestSettings": {
                    "initial_delay_seconds": "base_delay",
                    "max_delay_seconds": "max_delay",
                }
            },
        ),
    ):
        violations = check_field_name_mappings(runtime_file)

    # Both fields are misrouted
    assert len(violations) == 2
    violations_by_field = {v.runtime_field: v for v in violations}

    assert violations_by_field["base_delay"].settings_field == "max_delay_seconds"
    assert violations_by_field["base_delay"].expected_settings_field == "initial_delay_seconds"

    assert violations_by_field["max_delay"].settings_field == "initial_delay_seconds"
    assert violations_by_field["max_delay"].expected_settings_field == "max_delay_seconds"


def test_check_field_name_mappings_ignores_unmapped_classes(tmp_path: Path) -> None:
    """check_field_name_mappings ignores classes not in FIELD_MAPPINGS."""
    runtime_file = tmp_path / "runtime.py"
    runtime_file.write_text("""
@dataclass
class RuntimeConcurrencyConfig:
    max_workers: int

    @classmethod
    def from_settings(cls, settings):
        return cls(max_workers=settings.max_workers)
""")

    with (
        patch(
            "elspeth.contracts.config.alignment.SETTINGS_TO_RUNTIME",
            {"ConcurrencySettings": "RuntimeConcurrencyConfig"},
        ),
        patch(
            "elspeth.contracts.config.alignment.FIELD_MAPPINGS",
            {},  # No field mappings for this class
        ),
    ):
        violations = check_field_name_mappings(runtime_file)

    # No violations - class has no renamed fields in FIELD_MAPPINGS
    assert violations == []


def test_check_field_name_mappings_ignores_direct_name_fields(tmp_path: Path) -> None:
    """check_field_name_mappings ignores fields that aren't in FIELD_MAPPINGS.

    Fields with matching names (e.g., max_attempts=settings.max_attempts)
    don't need to be in FIELD_MAPPINGS and should not cause violations.
    """
    runtime_file = tmp_path / "runtime.py"
    runtime_file.write_text("""
@dataclass
class RuntimeRetryConfig:
    max_attempts: int
    base_delay: float

    @classmethod
    def from_settings(cls, settings):
        return cls(
            max_attempts=settings.max_attempts,          # Direct mapping - not in FIELD_MAPPINGS
            base_delay=settings.initial_delay_seconds,   # Renamed - in FIELD_MAPPINGS
        )
""")

    with (
        patch(
            "elspeth.contracts.config.alignment.SETTINGS_TO_RUNTIME",
            {"RetrySettings": "RuntimeRetryConfig"},
        ),
        patch(
            "elspeth.contracts.config.alignment.FIELD_MAPPINGS",
            {
                "RetrySettings": {
                    "initial_delay_seconds": "base_delay",
                    # max_attempts is NOT in FIELD_MAPPINGS (same name)
                }
            },
        ),
    ):
        violations = check_field_name_mappings(runtime_file)

    # No violations - base_delay correctly uses initial_delay_seconds
    # max_attempts is not in FIELD_MAPPINGS so not checked
    assert violations == []


def test_check_field_name_mappings_real_codebase() -> None:
    """Integration test: actual codebase has correct field mappings.

    This verifies that the current codebase passes the check.
    If this fails, a field mapping in from_settings() doesn't match
    the documented mapping in FIELD_MAPPINGS.
    """
    import pytest

    runtime_path = Path("src/elspeth/contracts/config/runtime.py")

    if not runtime_path.exists():
        pytest.skip("Running from different directory - required files not found")

    violations = check_field_name_mappings(runtime_path)
    assert violations == [], (
        f"Field mapping violations found: "
        f"{[(v.runtime_field, v.settings_field, v.expected_settings_field) for v in violations]}. "
        f"Fix the mapping in from_settings() or update FIELD_MAPPINGS."
    )


def test_field_mapping_violation_dataclass() -> None:
    """FieldMappingViolation dataclass holds expected fields."""
    violation = FieldMappingViolation(
        runtime_class="RuntimeTestConfig",
        runtime_field="base_delay",
        settings_field="max_delay_seconds",
        expected_settings_field="initial_delay_seconds",
        file="/path/to/runtime.py",
        line=42,
    )
    assert violation.runtime_class == "RuntimeTestConfig"
    assert violation.runtime_field == "base_delay"
    assert violation.settings_field == "max_delay_seconds"
    assert violation.expected_settings_field == "initial_delay_seconds"
    assert violation.file == "/path/to/runtime.py"
    assert violation.line == 42


# =============================================================================
# Hardcode Documentation Tests (check_hardcode_documentation)
# =============================================================================


def test_hardcode_literal_visitor_finds_plain_literals() -> None:
    """HardcodeLiteralVisitor finds plain literal values in keyword args."""
    import ast

    code = """
def from_settings(cls, settings):
    return cls(
        field_a=settings.field_a,
        jitter=1.0,
        magic_number=42,
        name="default",
        enabled=True,
    )
"""
    tree = ast.parse(code)
    visitor = HardcodeLiteralVisitor("settings")
    visitor.visit(tree)

    # Should find: jitter=1.0, magic_number=42, name="default", enabled=True
    # Should NOT find: field_a=settings.field_a
    assert len(visitor.hardcoded_literals) == 4
    literals_dict = dict(visitor.hardcoded_literals)
    assert literals_dict["jitter"] == 1.0
    assert literals_dict["magic_number"] == 42
    assert literals_dict["name"] == "default"
    assert literals_dict["enabled"] is True


def test_hardcode_literal_visitor_ignores_function_calls() -> None:
    """HardcodeLiteralVisitor ignores values wrapped in function calls."""
    import ast

    code = """
def from_settings(cls, settings):
    return cls(
        jitter=float(INTERNAL_DEFAULTS["retry"]["jitter"]),
        base_delay=float(settings.initial_delay_seconds),
        computed=some_func(42),
    )
"""
    tree = ast.parse(code)
    visitor = HardcodeLiteralVisitor("settings")
    visitor.visit(tree)

    # Should NOT find any - all are function calls
    assert len(visitor.hardcoded_literals) == 0


def test_hardcode_literal_visitor_ignores_subscripts() -> None:
    """HardcodeLiteralVisitor ignores subscript values like dict["key"]."""
    import ast

    code = """
def from_settings(cls, settings):
    return cls(
        jitter=INTERNAL_DEFAULTS["retry"]["jitter"],
        value=some_dict["key"],
    )
"""
    tree = ast.parse(code)
    visitor = HardcodeLiteralVisitor("settings")
    visitor.visit(tree)

    # Should NOT find any - all are subscripts
    assert len(visitor.hardcoded_literals) == 0


def test_hardcode_literal_visitor_finds_negative_numbers() -> None:
    """HardcodeLiteralVisitor finds negative number literals."""
    import ast

    code = """
def from_settings(cls, settings):
    return cls(
        offset=-1.5,
        countdown=-42,
    )
"""
    tree = ast.parse(code)
    visitor = HardcodeLiteralVisitor("settings")
    visitor.visit(tree)

    assert len(visitor.hardcoded_literals) == 2
    literals_dict = dict(visitor.hardcoded_literals)
    assert literals_dict["offset"] == -1.5
    assert literals_dict["countdown"] == -42


def test_extract_from_settings_hardcodes_finds_method(tmp_path: Path) -> None:
    """extract_from_settings_hardcodes finds hardcodes in from_settings method."""
    runtime_file = tmp_path / "runtime.py"
    runtime_file.write_text("""
@dataclass
class RuntimeTestConfig:
    field_a: int
    jitter: float

    @classmethod
    def from_settings(cls, settings):
        return cls(
            field_a=settings.field_a,
            jitter=1.0,
        )
""")

    result = extract_from_settings_hardcodes(runtime_file)

    assert "RuntimeTestConfig" in result
    assert len(result["RuntimeTestConfig"]) == 1
    assert ("jitter", 1.0) in result["RuntimeTestConfig"]


def test_extract_from_settings_hardcodes_multiple_classes(tmp_path: Path) -> None:
    """extract_from_settings_hardcodes handles multiple Runtime classes."""
    runtime_file = tmp_path / "runtime.py"
    runtime_file.write_text("""
@dataclass
class RuntimeRetryConfig:
    max_attempts: int
    jitter: float

    @classmethod
    def from_settings(cls, settings):
        return cls(
            max_attempts=settings.max_attempts,
            jitter=1.0,
        )


@dataclass
class RuntimeCheckpointConfig:
    enabled: bool
    buffer_size: int

    @classmethod
    def from_settings(cls, settings):
        return cls(
            enabled=settings.enabled,
            buffer_size=1000,
        )
""")

    result = extract_from_settings_hardcodes(runtime_file)

    assert len(result) == 2
    assert ("jitter", 1.0) in result["RuntimeRetryConfig"]
    assert ("buffer_size", 1000) in result["RuntimeCheckpointConfig"]


def test_check_hardcode_documentation_passes_documented(tmp_path: Path) -> None:
    """check_hardcode_documentation passes when hardcode is documented."""
    runtime_file = tmp_path / "runtime.py"
    runtime_file.write_text("""
@dataclass
class RuntimeRetryConfig:
    max_attempts: int
    jitter: float

    @classmethod
    def from_settings(cls, settings):
        return cls(
            max_attempts=settings.max_attempts,
            jitter=1.0,  # Documented in INTERNAL_DEFAULTS["retry"]["jitter"]
        )
""")

    with patch(
        "elspeth.contracts.config.defaults.INTERNAL_DEFAULTS",
        {"retry": {"jitter": 1.0}},
    ):
        violations = check_hardcode_documentation(runtime_file)

    assert violations == []


def test_check_hardcode_documentation_detects_undocumented(tmp_path: Path) -> None:
    """check_hardcode_documentation detects undocumented hardcode (the key test).

    This is the primary test case - catching hardcoded literals that are NOT
    documented in INTERNAL_DEFAULTS.
    """
    runtime_file = tmp_path / "runtime.py"
    runtime_file.write_text("""
@dataclass
class RuntimeRetryConfig:
    max_attempts: int
    jitter: float
    magic_number: int

    @classmethod
    def from_settings(cls, settings):
        return cls(
            max_attempts=settings.max_attempts,
            jitter=1.0,
            magic_number=42,  # UNDOCUMENTED - should be violation!
        )
""")

    with patch(
        "elspeth.contracts.config.defaults.INTERNAL_DEFAULTS",
        {"retry": {"jitter": 1.0}},  # magic_number is NOT documented
    ):
        violations = check_hardcode_documentation(runtime_file)

    assert len(violations) == 1
    v = violations[0]
    assert v.runtime_class == "RuntimeRetryConfig"
    assert v.runtime_field == "magic_number"
    assert "42" in v.literal_value
    assert v.subsystem == "retry"


def test_check_hardcode_documentation_detects_wrong_value(tmp_path: Path) -> None:
    """check_hardcode_documentation detects when documented value differs from code."""
    runtime_file = tmp_path / "runtime.py"
    runtime_file.write_text("""
@dataclass
class RuntimeRetryConfig:
    jitter: float

    @classmethod
    def from_settings(cls, settings):
        return cls(
            jitter=2.0,  # Code has 2.0 but documented as 1.0!
        )
""")

    with patch(
        "elspeth.contracts.config.defaults.INTERNAL_DEFAULTS",
        {"retry": {"jitter": 1.0}},  # Documented as 1.0
    ):
        violations = check_hardcode_documentation(runtime_file)

    assert len(violations) == 1
    v = violations[0]
    assert v.runtime_class == "RuntimeRetryConfig"
    assert v.runtime_field == "jitter"
    assert "2.0" in v.literal_value
    assert "1.0" in v.literal_value  # Should mention the documented value


def test_check_hardcode_documentation_no_subsystem_mapping(tmp_path: Path) -> None:
    """check_hardcode_documentation flags hardcodes in classes without subsystem mapping."""
    runtime_file = tmp_path / "runtime.py"
    # RuntimeUnknownConfig is not in RUNTIME_TO_SUBSYSTEM
    runtime_file.write_text("""
@dataclass
class RuntimeUnknownConfig:
    magic: int

    @classmethod
    def from_settings(cls, settings):
        return cls(
            magic=42,  # No subsystem mapping - all hardcodes are violations
        )
""")

    with patch(
        "elspeth.contracts.config.defaults.INTERNAL_DEFAULTS",
        {},
    ):
        violations = check_hardcode_documentation(runtime_file)

    assert len(violations) == 1
    v = violations[0]
    assert v.runtime_class == "RuntimeUnknownConfig"
    assert "(no subsystem mapping)" in v.subsystem


def test_check_hardcode_documentation_ignores_classes_without_hardcodes(tmp_path: Path) -> None:
    """check_hardcode_documentation passes classes with no hardcoded literals."""
    runtime_file = tmp_path / "runtime.py"
    runtime_file.write_text("""
@dataclass
class RuntimeRetryConfig:
    max_attempts: int
    base_delay: float

    @classmethod
    def from_settings(cls, settings):
        return cls(
            max_attempts=settings.max_attempts,
            base_delay=settings.initial_delay_seconds,
        )
""")

    with patch(
        "elspeth.contracts.config.defaults.INTERNAL_DEFAULTS",
        {"retry": {}},
    ):
        violations = check_hardcode_documentation(runtime_file)

    assert violations == []


def test_check_hardcode_documentation_real_codebase() -> None:
    """Integration test: actual codebase has documented hardcodes.

    This verifies that the current codebase passes the check.
    If this fails, a hardcoded literal was added to from_settings()
    without documenting it in INTERNAL_DEFAULTS.
    """
    import pytest

    runtime_path = Path("src/elspeth/contracts/config/runtime.py")

    if not runtime_path.exists():
        pytest.skip("Running from different directory - required files not found")

    violations = check_hardcode_documentation(runtime_path)
    assert violations == [], (
        f"Undocumented hardcode violations found: "
        f"{[(v.runtime_class, v.runtime_field, v.literal_value) for v in violations]}. "
        f"Add these to INTERNAL_DEFAULTS in contracts/config/defaults.py."
    )


def test_hardcode_violation_dataclass() -> None:
    """HardcodeViolation dataclass holds expected fields."""
    violation = HardcodeViolation(
        runtime_class="RuntimeRetryConfig",
        runtime_field="magic_number",
        literal_value="42",
        subsystem="retry",
        file="/path/to/runtime.py",
        line=0,
    )
    assert violation.runtime_class == "RuntimeRetryConfig"
    assert violation.runtime_field == "magic_number"
    assert violation.literal_value == "42"
    assert violation.subsystem == "retry"
    assert violation.file == "/path/to/runtime.py"
    assert violation.line == 0


# =============================================================================
# dict[str, Any] alias resolution
#
# `_is_dict_str_any` matches the SPELLING `dict[str, Any]`, so a module-level
# alias of that type reads as a typed annotation and scans as nothing at all.
# Six real returns hid behind three such aliases in mcp/types.py until 2026-09-04.
# =============================================================================


def _alias_package(tmp_path: Path) -> Path:
    """Write a package whose `types.py` defines a dict[str, Any] alias."""
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "types.py").write_text(
        "\n".join(
            [
                "from typing import Any",
                "",
                "RunDetail = dict[str, Any]",
                "",
            ]
        )
    )
    return package


def test_dict_alias_index_resolves_an_alias_through_the_import_that_carries_it(tmp_path: Path) -> None:
    """A name bound to dict[str, Any] is scanned like the pattern it aliases."""
    package = _alias_package(tmp_path)
    consumer = package / "reader.py"
    consumer.write_text(
        "\n".join(
            [
                "from pkg.types import RunDetail",
                "",
                "def get_run() -> RunDetail:",
                "    return {}",
                "",
            ]
        )
    )

    index = DictAliasIndex.build(package)
    aliases = index.names_in_scope(consumer, package)
    assert aliases == frozenset({"RunDetail"})

    violations = find_dict_violations(consumer, whitelist=set(), matched_entries={}, aliases=aliases)
    assert [(violation.context, violation.param_name) for violation in violations] == [("get_run", "return")]

    # Without the alias set the same annotation is invisible — this is the defect
    # the index exists to close, asserted rather than described.
    assert find_dict_violations(consumer, whitelist=set(), matched_entries={}) == []


def test_dict_alias_resolution_covers_the_optional_and_list_wrappers(tmp_path: Path) -> None:
    """`RunDetail | None` and `list[RunDetail]` resolve like their spelled-out forms."""
    package = _alias_package(tmp_path)
    consumer = package / "reader.py"
    consumer.write_text(
        "\n".join(
            [
                "from pkg.types import RunDetail",
                "",
                "def get_run() -> RunDetail | None:",
                "    return None",
                "",
                "def list_runs() -> list[RunDetail]:",
                "    return []",
                "",
                "def ingest(payload: RunDetail) -> None:",
                "    return None",
                "",
            ]
        )
    )

    index = DictAliasIndex.build(package)
    violations = find_dict_violations(consumer, whitelist=set(), matched_entries={}, aliases=index.names_in_scope(consumer, package))

    assert [(violation.context, violation.param_name) for violation in violations] == [
        ("get_run", "return"),
        ("list_runs", "return (list)"),
        ("ingest", "payload"),
    ]


def test_dict_alias_does_not_leak_into_a_module_that_never_imported_it(tmp_path: Path) -> None:
    """Resolution is by import, not by name — a same-named type elsewhere is untouched.

    The cheap implementation of this index is a tree-wide set of alias NAMES. That
    version reports any annotation spelled `RunDetail` anywhere, including a module
    that binds the name to a real TypedDict. This test is what separates the two.
    """
    package = _alias_package(tmp_path)
    stranger = package / "stranger.py"
    stranger.write_text(
        "\n".join(
            [
                "from typing import TypedDict",
                "",
                "class RunDetail(TypedDict):",
                "    run_id: str",
                "",
                "def get_run() -> RunDetail:",
                '    return {"run_id": ""}',
                "",
            ]
        )
    )

    index = DictAliasIndex.build(package)
    assert index.names_in_scope(stranger, package) == frozenset()
    assert find_dict_violations(stranger, whitelist=set(), matched_entries={}, aliases=index.names_in_scope(stranger, package)) == []


def test_dict_alias_index_records_the_forms_it_deliberately_cannot_resolve(tmp_path: Path) -> None:
    """The index's documented holes, asserted so they stay documented.

    Each form below is a real way to launder `dict[str, Any]` past this scan. None
    is resolved, and that is a stated scope limit rather than an aspiration: a
    future widening must delete the matching assertion here, which is the point.
    """
    package = _alias_package(tmp_path)
    evader = package / "evader.py"
    evader.write_text(
        "\n".join(
            [
                "from typing import Any",
                "import pkg.types",
                "from pkg.types import RunDetail as Renamed",
                "",
                "def _local_alias() -> None:",
                "    Inner = dict[str, Any]",  # alias bound in a function body
                "    def nested() -> Inner:",
                "        return {}",
                "",
                "def via_attribute() -> pkg.types.RunDetail:",  # attribute access
                "    return {}",
                "",
                "def via_rename() -> Renamed:",  # renamed on import
                "    return {}",
                "",
                'def via_string() -> "RunDetail":',  # string forward reference
                "    return {}",
                "",
            ]
        )
    )

    index = DictAliasIndex.build(package)
    aliases = index.names_in_scope(evader, package)

    # `Inner` is function-local, so it is not importable and is not indexed.
    assert "Inner" not in aliases
    # `RunDetail` is imported only under a new name, which this index does not follow.
    assert aliases == frozenset()
    assert find_dict_violations(evader, whitelist=set(), matched_entries={}, aliases=aliases) == []


# =============================================================================
# Scope of the dict[str, Any] scan
#
# These tests record what the scan does NOT look at. A green run is evidence
# about parameters and returns spelled `dict[str, Any]`, and about nothing else.
# Widening the scanner should turn these red — that is their function.
# =============================================================================


def test_dict_pattern_scan_is_blind_to_variable_and_class_attribute_annotations(tmp_path: Path) -> None:
    """`ast.AnnAssign` sites are not scanned. Measured 2026-09-04: 293 exist in src/elspeth."""
    test_file = tmp_path / "annassign.py"
    test_file.write_text(
        "\n".join(
            [
                "from typing import Any",
                "",
                "MODULE_LEVEL: dict[str, Any] = {}",
                "",
                "class Holder:",
                "    attribute: dict[str, Any] = {}",
                "",
                "    def method(self) -> None:",
                "        local: dict[str, Any] = {}",
                "        del local",
                "",
            ]
        )
    )

    assert find_dict_violations(test_file, whitelist=set(), matched_entries={}) == []


def test_dict_pattern_scan_is_blind_to_positional_only_and_variadic_parameters(tmp_path: Path) -> None:
    """posonlyargs, *args and **kwargs are not scanned.

    LATENT at 2026-09-04: zero such annotated sites exist in src/elspeth, so this
    records a hole rather than a live bypass. It is pinned because the cost of
    finding out later is a silently unscanned parameter, not because it is firing.
    """
    test_file = tmp_path / "variadic.py"
    test_file.write_text(
        "\n".join(
            [
                "from typing import Any",
                "",
                "def positional_only(payload: dict[str, Any], /) -> None:",
                "    return None",
                "",
                "def variadic(*args: dict[str, Any], **kwargs: dict[str, Any]) -> None:",
                "    return None",
                "",
            ]
        )
    )

    assert find_dict_violations(test_file, whitelist=set(), matched_entries={}) == []

    # The same annotation in an ordinary parameter IS reported — so the blindness
    # above is about parameter KIND, not about the annotation being unrecognised.
    control = tmp_path / "control.py"
    control.write_text(
        "\n".join(
            [
                "from typing import Any",
                "",
                "def ordinary(payload: dict[str, Any]) -> None:",
                "    return None",
                "",
            ]
        )
    )
    assert [violation.param_name for violation in find_dict_violations(control, whitelist=set(), matched_entries={})] == ["payload"]


def test_dict_pattern_scan_covers_only_dict_containers_with_an_any_value(tmp_path: Path) -> None:
    """Mapping forms and object-valued mappings are out of scope, by design.

    `dict[str, object]` and `Mapping[str, object]` already force narrowing at every
    use site, so excluding them is deliberate. `Mapping[str, Any]` does NOT force
    narrowing and is excluded anyway — measured 2026-09-04 at 729 sites in
    src/elspeth, the largest single form this scan cannot see.
    """
    test_file = tmp_path / "containers.py"
    test_file.write_text(
        "\n".join(
            [
                "from collections.abc import Mapping, MutableMapping",
                "from typing import Any",
                "",
                "def read_mapping(payload: Mapping[str, Any]) -> Mapping[str, Any]:",
                "    return payload",
                "",
                "def read_mutable(payload: MutableMapping[str, Any]) -> None:",
                "    return None",
                "",
                "def read_object(payload: dict[str, object]) -> Mapping[str, object]:",
                "    return payload",
                "",
            ]
        )
    )

    assert find_dict_violations(test_file, whitelist=set(), matched_entries={}) == []


# =============================================================================
# Soft-mapping census (elspeth-10d605be55)
#
# The whitelist gate above counts one SPELLING in two positions. The census
# counts every soft mapping form in every annotation position, so that a
# rewrite between forms is a swap the pin makes visible, not a retirement the
# scoreboard credits. These tests pin what the census counts, what it scores
# as a boundary conversion, and that any drift from the pin fails.
# =============================================================================


def _census_forms(sites: list[CensusSite]) -> list[tuple[str, str, str, bool]]:
    return [(site.context, site.position, site.form, site.boundary) for site in sites]


def test_soft_mapping_census_recognises_every_form_and_spelling(tmp_path: Path) -> None:
    """The five forms, under both the builtin and typing spellings, each count once."""
    test_file = tmp_path / "forms.py"
    test_file.write_text(
        "\n".join(
            [
                "import typing",
                "from collections.abc import Mapping, MutableMapping",
                "from typing import Any, Dict",
                "",
                "def a(p: dict[str, Any]) -> None: ...",
                "def b(p: Dict[str, typing.Any]) -> None: ...",
                "def c(p: Mapping[str, Any]) -> None: ...",
                "def d(p: MutableMapping[str, Any]) -> None: ...",
                "def e(p: dict[str, object]) -> None: ...",
                "def f(p: Mapping[str, object]) -> None: ...",
                "def g(p: typing.Mapping[str, Any]) -> None: ...",
                "",
            ]
        )
    )

    assert _census_forms(census_file(test_file)) == [
        ("a", "param:p", "dict[str, Any]", False),
        ("b", "param:p", "dict[str, Any]", False),
        ("c", "param:p", "Mapping[str, Any]", False),
        ("d", "param:p", "MutableMapping[str, Any]", False),
        ("e", "param:p", "dict[str, object]", False),
        ("f", "param:p", "Mapping[str, object]", False),
        ("g", "param:p", "Mapping[str, Any]", False),
    ]
    assert set(SOFT_MAPPING_FORMS) == {
        "dict[str, Any]",
        "Mapping[str, Any]",
        "MutableMapping[str, Any]",
        "dict[str, object]",
        "Mapping[str, object]",
    }


def test_soft_mapping_census_counts_every_annotation_position(tmp_path: Path) -> None:
    """Positional-only, variadic, keyword-only, return, and AnnAssign at every scope all count.

    Each of these is a position the whitelist gate deliberately does not scan
    (see the scope pins above). The census exists precisely so a soft site in
    one of them is not invisible.
    """
    test_file = tmp_path / "positions.py"
    test_file.write_text(
        "\n".join(
            [
                "from collections.abc import Mapping",
                "from typing import Any",
                "",
                "MODULE_LEVEL: dict[str, Any] = {}",
                "",
                "class Holder:",
                "    attribute: Mapping[str, Any] = {}",
                "",
                "    def method(self, /, first: dict[str, object], *rest: Mapping[str, object], key: dict[str, Any], **extra: Mapping[str, Any]) -> dict[str, Any]:",
                "        local: MutableMapping[str, Any] = {}",
                "        return local",
                "",
            ]
        )
    )

    assert _census_forms(census_file(test_file)) == [
        ("<module>", "annassign:MODULE_LEVEL", "dict[str, Any]", False),
        ("Holder", "annassign:attribute", "Mapping[str, Any]", False),
        ("Holder.method", "param:first", "dict[str, object]", False),
        ("Holder.method", "param:*rest", "Mapping[str, object]", False),
        ("Holder.method", "param:key", "dict[str, Any]", False),
        ("Holder.method", "param:**extra", "Mapping[str, Any]", False),
        ("Holder.method", "return", "dict[str, Any]", False),
        ("Holder.method", "annassign:local", "MutableMapping[str, Any]", False),
    ]


def test_soft_mapping_census_counts_every_occurrence_inside_one_annotation(tmp_path: Path) -> None:
    """A union carrying two soft forms is two occurrences; a wrapper carrying one is one.

    Per-occurrence counting is what makes a form-to-form rewrite a visible swap:
    `dict[str, Any] | None` -> `Mapping[str, Any] | None` moves one count from
    one column to the other in the same file, which the pin refuses.
    """
    test_file = tmp_path / "nested.py"
    test_file.write_text(
        "\n".join(
            [
                "from collections.abc import Mapping",
                "from typing import Any",
                "",
                "def both(p: dict[str, Any] | Mapping[str, Any] | None) -> None: ...",
                "def wrapped(p: list[dict[str, Any]]) -> tuple[Mapping[str, object], ...]: ...",
                "def inner(p: dict[str, dict[str, Any]]) -> None: ...",
                "",
            ]
        )
    )

    assert _census_forms(census_file(test_file)) == [
        ("both", "param:p", "dict[str, Any]", False),
        ("both", "param:p", "Mapping[str, Any]", False),
        ("wrapped", "param:p", "dict[str, Any]", False),
        ("wrapped", "return", "Mapping[str, object]", False),
        ("inner", "param:p", "dict[str, Any]", False),
    ]


def test_soft_mapping_census_ignores_hard_mappings(tmp_path: Path) -> None:
    """A mapping with a hard value type, a non-str key, or a non-mapping container is not soft."""
    test_file = tmp_path / "hard.py"
    test_file.write_text(
        "\n".join(
            [
                "from collections.abc import Mapping, Sequence",
                "from typing import Any",
                "",
                "def a(p: dict[str, int]) -> Mapping[str, str]: ...",
                "def b(p: dict[int, Any]) -> Mapping[bytes, object]: ...",
                "def c(p: Sequence[Any]) -> list[object]: ...",
                "def d(p: dict) -> Mapping: ...",
                "def e(p: dict[str, list[Any]]) -> None: ...",
                "",
            ]
        )
    )

    assert census_file(test_file) == []


def test_soft_mapping_census_scores_a_trust_boundary_source_param_as_boundary(tmp_path: Path) -> None:
    """The parameter a @trust_boundary names as its source is a boundary conversion, not a soft site.

    A Tier-3 parse that constructs an owned type is how a soft site is retired
    honestly (ADR-032), so it scores as a removal. Only the named source_param
    qualifies: the function's other soft parameters and its return stay soft,
    and a decorator naming a different parameter changes nothing.
    """
    test_file = tmp_path / "boundary.py"
    test_file.write_text(
        "\n".join(
            [
                "from collections.abc import Mapping",
                "from typing import Any",
                "from elspeth.contracts.trust_boundary import trust_boundary",
                "",
                "@trust_boundary(tier=3, source='llm', source_param='options', suppresses=('R5',), invariant='x')",
                "def parse(options: dict[str, Any], hint: Mapping[str, Any]) -> dict[str, Any]:",
                "    return {}",
                "",
                "@trust_boundary(tier=3, source='llm', source_param='other', suppresses=('R5',), invariant='x')",
                "def elsewhere(options: dict[str, Any], other: str) -> None:",
                "    return None",
                "",
                "def plain(options: dict[str, Any]) -> None:",
                "    return None",
                "",
            ]
        )
    )

    assert _census_forms(census_file(test_file)) == [
        ("parse", "param:options", "dict[str, Any]", True),
        ("parse", "param:hint", "Mapping[str, Any]", False),
        ("parse", "return", "dict[str, Any]", False),
        ("elsewhere", "param:options", "dict[str, Any]", False),
        ("plain", "param:options", "dict[str, Any]", False),
    ]

    table = tabulate_census(census_file(test_file))
    assert table == {str(test_file): {"dict[str, Any]": 3, "Mapping[str, Any]": 1, "boundary": 1}}
    assert census_totals(table) == {
        "soft": 4,
        "boundary": 1,
        "dict[str, Any]": 3,
        "Mapping[str, Any]": 1,
        "MutableMapping[str, Any]": 0,
        "dict[str, object]": 0,
        "Mapping[str, object]": 0,
    }


def test_soft_mapping_census_resolves_dict_aliases(tmp_path: Path) -> None:
    """A name bound to dict[str, Any] counts as dict[str, Any] in the file that imports it."""
    package = _alias_package(tmp_path)
    consumer = package / "reader.py"
    consumer.write_text(
        "\n".join(
            [
                "from pkg.types import RunDetail",
                "",
                "def get_run() -> RunDetail | None:",
                "    return None",
                "",
            ]
        )
    )

    index = DictAliasIndex.build(package)
    aliases = index.names_in_scope(consumer, package)
    assert _census_forms(census_file(consumer, aliases)) == [("get_run", "return", "dict[str, Any]", False)]
    # Without the alias set the site is invisible — the laundering the index closes.
    assert census_file(consumer) == []


def test_compare_census_reports_a_rewrite_as_a_swap_not_a_retirement() -> None:
    """Every per-file, per-form difference from the pin is a drift, in either direction.

    A dict -> Mapping rewrite is two drifts in one file: the pin does not net
    them out, so the re-pin diff shows the swap. A genuine conversion is one
    drift (the count falls) and the soft total falls with it.
    """
    pinned = {"a.py": {"dict[str, Any]": 2, "Mapping[str, Any]": 1}, "b.py": {"dict[str, object]": 1}}

    assert compare_census(pinned, census_totals(pinned), pinned) == []

    rewrite = {"a.py": {"dict[str, Any]": 1, "Mapping[str, Any]": 2}, "b.py": {"dict[str, object]": 1}}
    assert compare_census(pinned, census_totals(pinned), rewrite) == [
        CensusDrift(file="a.py", form="Mapping[str, Any]", pinned=1, live=2),
        CensusDrift(file="a.py", form="dict[str, Any]", pinned=2, live=1),
    ]

    conversion = {"a.py": {"dict[str, Any]": 1, "Mapping[str, Any]": 1}, "b.py": {"dict[str, object]": 1}}
    assert compare_census(pinned, census_totals(pinned), conversion) == [
        CensusDrift(file="a.py", form="dict[str, Any]", pinned=2, live=1),
    ]

    new_file = {**pinned, "c.py": {"Mapping[str, object]": 1}}
    assert compare_census(pinned, census_totals(pinned), new_file) == [
        CensusDrift(file="c.py", form="Mapping[str, object]", pinned=0, live=1),
    ]

    removed_file = {"a.py": pinned["a.py"]}
    assert compare_census(pinned, census_totals(pinned), removed_file) == [
        CensusDrift(file="b.py", form="dict[str, object]", pinned=1, live=0),
    ]

    # A hand-edited totals block that no longer matches its own files block is a
    # drift too, so the header cannot be made to lie about the score.
    forged = {**census_totals(pinned), "soft": 1}
    assert compare_census(pinned, forged, pinned) == [
        CensusDrift(file="<totals>", form="soft", pinned=1, live=4),
    ]


def test_census_round_trips_through_yaml_with_the_method_stated(tmp_path: Path) -> None:
    """The pin file carries the counting rule in its header and reloads to the same table."""
    table = {"z.py": {"dict[str, Any]": 1, "boundary": 2}, "a.py": {"Mapping[str, object]": 3}}
    census_path = tmp_path / "census.yaml"

    write_census(census_path, table)
    text = census_path.read_text()
    assert CENSUS_METHOD in text
    assert text.index("a.py") < text.index("z.py")  # deterministic ordering

    loaded, stored_totals = load_census(census_path)
    assert loaded == table
    assert stored_totals == census_totals(table)
    assert compare_census(loaded, stored_totals, table) == []


def test_check_soft_mapping_census_fails_on_drift_and_repins_on_request(tmp_path: Path) -> None:
    """End to end: a missing pin fails, --write-census creates it, a new soft site fails, re-pin passes."""
    src_dir = tmp_path / "src" / "pkg"
    src_dir.mkdir(parents=True)
    module = src_dir / "mod.py"
    module.write_text("from typing import Any\n\ndef f(p: dict[str, Any]) -> None: ...\n")
    census_path = tmp_path / "census.yaml"

    missing = check_soft_mapping_census(src_dir, census_path, DictAliasIndex.build(src_dir), write=False)
    assert missing.ok is False
    assert any("--write-census" in line for line in missing.lines)

    written = check_soft_mapping_census(src_dir, census_path, DictAliasIndex.build(src_dir), write=True)
    assert written.ok is True
    assert written.totals["soft"] == 1

    clean = check_soft_mapping_census(src_dir, census_path, DictAliasIndex.build(src_dir), write=False)
    assert clean.ok is True

    module.write_text(
        "from collections.abc import Mapping\nfrom typing import Any\n\ndef f(p: dict[str, Any]) -> None: ...\n\nX: Mapping[str, Any] = {}\n"
    )
    drifted = check_soft_mapping_census(src_dir, census_path, DictAliasIndex.build(src_dir), write=False)
    assert drifted.ok is False
    assert drifted.drifts == (CensusDrift(file=str(module), form="Mapping[str, Any]", pinned=0, live=1),)
    assert any("annassign:X" in line for line in drifted.lines)

    repinned = check_soft_mapping_census(src_dir, census_path, DictAliasIndex.build(src_dir), write=True)
    assert repinned.ok is True
    assert repinned.totals["soft"] == 2
