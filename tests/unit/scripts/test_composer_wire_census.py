"""Discriminating probes for registry-bound MODEL ownership."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import FunctionType, ModuleType

import pytest
from scripts.cicd.composer_wire_census import CensusError, _model_for_handler, census_model_wire, census_taught_wire


@pytest.fixture
def module_factory(tmp_path: Path) -> Iterator[Callable[[str, str], ModuleType]]:
    names: list[str] = []

    def build(name: str, source: str) -> ModuleType:
        path = tmp_path / f"{name}.py"
        path.write_text("from pydantic import BaseModel, Field\n" + source)
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        names.append(name)
        spec.loader.exec_module(module)
        return module

    yield build
    for name in names:
        del sys.modules[name]


def handler(module: ModuleType) -> FunctionType:
    function = vars(module)["arbitrary_name"]
    assert isinstance(function, FunctionType)
    return function


def test_alternate_name_and_module_binding(module_factory: Callable[[str, str], ModuleType]) -> None:
    first = module_factory(
        "census_first", "class Model(BaseModel):\n    first: str\ndef arbitrary_name(payload):\n    return Model.model_validate(payload)\n"
    )
    second = module_factory(
        "census_second",
        "class Model(BaseModel):\n    second: str\ndef arbitrary_name(payload):\n    return Model.model_validate(payload)\n",
    )
    one, _ = _model_for_handler(handler(first))
    two, _ = _model_for_handler(handler(second))
    assert one is vars(first)["Model"]
    assert two is vars(second)["Model"]
    assert one is not two


def test_forwarded_input_and_unused_nested_function(module_factory: Callable[[str, str], ModuleType]) -> None:
    module = module_factory(
        "census_forward",
        """
class Model(BaseModel):
    actual: str
class Other(BaseModel):
    unrelated: str
def validate(payload):
    return Model.model_validate(payload)
def arbitrary_name(arguments):
    def unused():
        return Other.model_validate(arguments)
    Other.model_validate({'unrelated': 'internal'})
    return validate(arguments)
""",
    )
    model, sites = _model_for_handler(handler(module))
    assert model is vars(module)["Model"]
    assert any("validate" in site for site in sites)


@pytest.mark.parametrize(
    "body",
    [
        "return Model.model_validate(dict(arguments))",
        "return models[0].model_validate(arguments)",
        "Model = Other\n    return Model.model_validate(arguments)",
        "Model.model_validate(arguments)\n    return Other.model_validate(arguments)",
        "def nested(payload):\n        return Model.model_validate(payload)\n    return nested(arguments)",
        "return Model.model_validate_json(arguments)",
    ],
)
def test_unsupported_or_ambiguous_validation_refused(module_factory: Callable[[str, str], ModuleType], body: str) -> None:
    module = module_factory(
        "census_unsupported",
        "class Model(BaseModel):\n    a: str\nclass Other(BaseModel):\n    b: str\nmodels = [Model]\ndef arbitrary_name(arguments):\n    "
        + body
        + "\n",
    )
    with pytest.raises(CensusError):
        _model_for_handler(handler(module))


def test_validation_alias_refused(module_factory: Callable[[str, str], ModuleType]) -> None:
    module = module_factory(
        "census_alias",
        "class Model(BaseModel):\n    a: str = Field(alias='wire_a')\ndef arbitrary_name(arguments):\n    return Model.model_validate(arguments)\n",
    )
    with pytest.raises(CensusError, match="alias"):
        _model_for_handler(handler(module))


def test_live_catalog_includes_zero_knob_tools_and_absence() -> None:
    from elspeth.web.composer.tools._dispatch import get_tool_definitions

    rows = census_model_wire()
    assert set(rows) == {definition["name"] for definition in get_tool_definitions()}
    assert rows["list_blobs"].shipped == frozenset()
    assert rows["list_blobs"].model_class is not None
    assert rows["get_pipeline_state"].model_class is not None
    assert rows["set_pipeline"].model_class is not None
    assert rows["request_advisor_hint"].model_class is not None
    assert rows["validate_secret_ref"].model_class is not None


def test_module_qualified_model_receiver(module_factory: Callable[[str, str], ModuleType]) -> None:
    models = module_factory("census_models", "class Model(BaseModel):\n    wire: str\n")
    module = module_factory(
        "census_qualified",
        "import census_models\ndef arbitrary_name(arguments):\n    return census_models.Model.model_validate(arguments)\n",
    )
    model, _ = _model_for_handler(handler(module))
    assert model is vars(models)["Model"]


def test_non_model_validation_receiver_refused(module_factory: Callable[[str, str], ModuleType]) -> None:
    module = module_factory(
        "census_non_model",
        "class Model:\n    pass\ndef arbitrary_name(arguments):\n    return Model.model_validate(arguments)\n",
    )
    with pytest.raises(CensusError, match="not a BaseModel"):
        _model_for_handler(handler(module))


def test_redaction_model_is_a_separate_observation(module_factory: Callable[[str, str], ModuleType]) -> None:
    from scripts.cicd.composer_wire_census import census_redaction_models

    from elspeth.web.composer.redaction import SetPipelineArgumentsModel

    assert census_redaction_models()["set_pipeline"] is SetPipelineArgumentsModel
    module = module_factory("census_without_admission", "def arbitrary_name(arguments):\n    return None\n")
    assert _model_for_handler(handler(module))[0] is None


@pytest.mark.parametrize(
    "body",
    [
        "copied = {'a': arguments['a']}\n    return validate(copied)",
        "return validate(dict(arguments))",
        "return validate({**arguments})",
        "return validate(arguments.copy())",
        "return validate([arguments][0])",
        "alias = arguments\n    return validate(alias)",
        "def nested():\n        return Model.model_validate(arguments)\n    return nested()",
    ],
)
def test_unsupported_processing_is_unresolved(
    module_factory: Callable[[str, str], ModuleType], monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    from dataclasses import replace

    from scripts.cicd import composer_wire_census as census

    from elspeth.contracts.freeze import deep_thaw
    from elspeth.web.composer.tools._registry import _REGISTERED_TOOLS

    module = module_factory(
        "census_processing",
        "class Model(BaseModel):\n    a: str\ndef validate(payload):\n    return Model.model_validate(payload)\n"
        "def arbitrary_name(arguments):\n    " + body + "\n",
    )
    declaration = next(item for item in _REGISTERED_TOOLS if item.name == "list_blobs")
    replacement = replace(declaration, handler=handler(module), json_schema=deep_thaw(declaration.json_schema))
    monkeypatch.setattr(census, "_REGISTERED_TOOLS", tuple(replacement if item is declaration else item for item in _REGISTERED_TOOLS))
    assert census.census_model_wire()["list_blobs"].site.startswith("unresolved:")


@pytest.mark.parametrize("change", ["duplicate", "missing_handler", "missing_advisor", "unexpected"])
def test_catalog_universe_must_match_handlers_and_explicit_interceptions(monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    from scripts.cicd import composer_wire_census as census

    from elspeth.web.composer.tools._dispatch import get_tool_definitions

    definitions = get_tool_definitions()
    if change == "duplicate":
        definitions.append(definitions[0])
    elif change == "missing_handler":
        definitions = [definition for definition in definitions if definition["name"] != "wire_secret_ref"]
    elif change == "missing_advisor":
        definitions = [definition for definition in definitions if definition["name"] != "request_advisor_hint"]
    else:
        definitions.append({**definitions[0], "name": "unexpected_tool"})
    monkeypatch.setattr(census, "get_tool_definitions", lambda: definitions)
    with pytest.raises(CensusError):
        census.census_model_wire()


@pytest.mark.parametrize(
    ("import_statement", "receiver"),
    [
        ("from census_actual import Model", "Model"),
        ("import census_actual as models", "models.Model"),
        ("import census_actual", "census_actual.Model"),
    ],
)
def test_local_import_cannot_resolve_to_wrong_global_model(
    module_factory: Callable[[str, str], ModuleType], import_statement: str, receiver: str
) -> None:
    actual = module_factory("census_actual", "class Model(BaseModel):\n    actual: str\n")
    module = module_factory(
        "census_import_shadow",
        "class Model(BaseModel):\n    wrong: str\n"
        "import sys\nmodels = sys.modules[__name__]\ncensus_actual = models\n"
        f"def arbitrary_name(arguments):\n    {import_statement}\n    return {receiver}.model_validate(arguments)\n",
    )
    result = handler(module)({"actual": "runtime evidence"})
    assert type(result) is vars(actual)["Model"]
    assert type(result) is not vars(module)["Model"]
    with pytest.raises(CensusError, match="local binding"):
        _model_for_handler(handler(module))


def test_identity_bound_typing_cast_preserves_child_validation(module_factory: Callable[[str, str], ModuleType]) -> None:
    module = module_factory(
        "census_real_cast",
        "from typing import cast as transparent\n"
        "class Model(BaseModel):\n    actual: str\n"
        "def arbitrary_name(arguments):\n    return transparent(Model, Model.model_validate(arguments))\n",
    )
    model, _ = _model_for_handler(handler(module))
    assert model is vars(module)["Model"]


def test_fake_cast_alias_is_not_transparent(module_factory: Callable[[str, str], ModuleType]) -> None:
    module = module_factory(
        "census_fake_cast",
        "class Model(BaseModel):\n    actual: str\n"
        "def fake_cast(kind, payload):\n    return payload\n"
        "cast = fake_cast\n"
        "def arbitrary_name(arguments):\n    return cast(Model, Model.model_validate(arguments))\n",
    )
    with pytest.raises(CensusError, match="transformed input forwarding"):
        _model_for_handler(handler(module))


def test_taught_census_matches_live_argument_schemas() -> None:
    from elspeth.web.composer.tools._dispatch import get_tool_definitions

    rows = census_taught_wire()
    assert {name: row.shipped for name, row in rows.items()} == {
        definition["name"]: frozenset(definition["parameters"]["properties"]) for definition in get_tool_definitions()
    }
    assert rows["list_blobs"].shipped == frozenset()
    assert rows["upsert_node"].taught == rows["upsert_node"].shipped


def test_direct_cli_needs_only_the_two_source_roots(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join((str(root / "src"), str(root / "elspeth-lints/src")))}
    environment["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    result = subprocess.run(
        [sys.executable, str(root / "scripts/cicd/composer_wire_census.py")],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    expected = census_taught_wire()
    assert {row["tool"] for row in rows} == set(expected)
    assert all(row["untaught"] == [] and row["stale_argument_declarations"] == [] for row in rows)
    # The existing MODEL interface survives the TAUGHT extension.
    assert all("model_class" in row and "shipped_not_model" in row and "site" in row for row in rows)


def test_complete_candidate_copy_is_proven(module_factory: Callable[[str, str], ModuleType]) -> None:
    module = module_factory(
        "census_complete_copy",
        """
class Model(BaseModel):
    a: str
def arbitrary_name(arguments):
    copied = dict(arguments)
    return Model.model_validate(copied)
""",
    )
    assert _model_for_handler(handler(module))[0] is vars(module)["Model"]


@pytest.mark.parametrize("change", ["copied['injected'] = 1", "copied.clear()", "copied = {}"])
def test_mutated_candidate_copy_is_refused(module_factory: Callable[[str, str], ModuleType], change: str) -> None:
    module = module_factory(
        "census_mutated_copy",
        "class Model(BaseModel):\n    a: str\ndef arbitrary_name(arguments):\n    copied = dict(arguments)\n    "
        + change
        + "\n    return Model.model_validate(copied)\n",
    )
    with pytest.raises(CensusError):
        _model_for_handler(handler(module))


@pytest.mark.parametrize("change", ["arguments['a'] = 'replacement'", "arguments.pop('extra', None)"])
def test_mutated_original_input_is_refused(module_factory: Callable[[str, str], ModuleType], change: str) -> None:
    module = module_factory(
        "census_mutated_original",
        "class Model(BaseModel):\n    a: str\ndef arbitrary_name(arguments):\n    "
        + change
        + "\n    return Model.model_validate(arguments)\n",
    )
    with pytest.raises(CensusError):
        _model_for_handler(handler(module))


def test_read_tracks_admitted_instance_and_raw_helper(module_factory: Callable[[str, str], ModuleType]) -> None:
    from scripts.cicd.composer_wire_census import _reads_for_handler

    module = module_factory(
        "read_owned",
        """
class Model(BaseModel):
    first: str
    second: str
def child(payload):
    return payload.get('hidden')
def arbitrary_name(arguments):
    admitted = Model.model_validate(arguments)
    independent = Model.model_validate({'first':'a', 'second':'b'})
    def unused():
        return admitted.second
    return admitted.first, independent.second, child(arguments)
""",
    )
    evidence, presence, unresolved = _reads_for_handler(handler(module))
    assert {item.field for item in evidence} == {"first", "hidden"}
    assert not presence
    assert not unresolved
    assert any("child" in item.site for item in evidence)


@pytest.mark.parametrize(
    "body,expected",
    [
        ("return Model.model_validate(arguments).first", {"first"}),
        ("validated = Model.model_validate(arguments)\n    unused = validated.first", {"first"}),
        ("validated = Model.model_validate(arguments)\n    print(validated.first)", {"first"}),
        ("return arguments['raw']", {"raw"}),
        ("return 'raw' in arguments", set()),
    ],
)
def test_read_is_extraction_not_causal_use(module_factory: Callable[[str, str], ModuleType], body: str, expected: set[str]) -> None:
    from scripts.cicd.composer_wire_census import _reads_for_handler

    module = module_factory(
        "read_extraction", "class Model(BaseModel):\n    first: str\ndef arbitrary_name(arguments):\n    " + body + "\n"
    )
    evidence, presence, unresolved = _reads_for_handler(handler(module))
    assert {item.field for item in evidence} == expected
    assert {item.field for item in presence} == ({"raw"} if " in arguments" in body else set())
    assert not unresolved


@pytest.mark.parametrize(
    "body,reason",
    [
        ("alias = arguments\n    return alias['hidden']", "alias"),
        ("return arguments[key]", "dynamic"),
        ("return arguments.get(key)", "dynamic"),
        ("return Model.model_validate(arguments).model_dump()", "bulk"),
        ("return Model.model_validate(arguments).model_copy(deep=True)", "copy"),
        ("parsed = Model.model_validate(arguments)\n    parsed = Model(first='foreign')\n    return parsed.first", "rebound"),
        ("def child():\n        return arguments['hidden']\n    child = lambda: None\n    return child()", "closure"),
        ("return (lambda: arguments['hidden'])()", "closure"),
        ("child = lambda: arguments['hidden']\n    return child()", "closure"),
        ("return getattr(Model.model_validate(arguments), key)", "unsupported"),
        ("return dict(Model.model_validate(arguments)).first", "bulk"),
        ("copied = {**arguments}\n    return Model.model_validate(copied).first", "construction"),
        ("copied = dict(arguments)\n    copied.clear()\n    return Model.model_validate(copied).first", "mutated"),
    ],
)
def test_read_unresolved_provenance_is_not_silent(module_factory: Callable[[str, str], ModuleType], body: str, reason: str) -> None:
    from scripts.cicd.composer_wire_census import _reads_for_handler

    module = module_factory(
        "read_unresolved", "class Model(BaseModel):\n    first: str\ndef arbitrary_name(arguments):\n    " + body + "\n"
    )
    evidence, _, unresolved = _reads_for_handler(handler(module))
    assert not evidence
    assert any(reason in item for item in unresolved), unresolved


def test_read_keeps_partial_extractions_when_bulk_is_unresolved(module_factory: Callable[[str, str], ModuleType]) -> None:
    from scripts.cicd.composer_wire_census import _reads_for_handler

    module = module_factory(
        "read_partial",
        "class Model(BaseModel):\n    first: str\n    second: str\ndef arbitrary_name(arguments):\n"
        "    parsed = Model.model_validate(arguments)\n    print(parsed.first)\n    return parsed.model_dump()\n",
    )
    evidence, _, unresolved = _reads_for_handler(handler(module))
    assert {item.field for item in evidence} == {"first"}
    assert any("bulk" in item for item in unresolved)


def test_read_complete_raw_copy_and_renamed_model_forwarding(module_factory: Callable[[str, str], ModuleType]) -> None:
    from scripts.cicd.composer_wire_census import _reads_for_handler

    module = module_factory(
        "read_copy",
        """
from typing import cast as identity
class Model(BaseModel):
    first: str
    second: str
def child(other_name):
    return other_name.first
def arbitrary_name(arguments):
    copied = dict(arguments)
    parsed = identity(Model, Model.model_validate(copied))
    return child(other_name=parsed)
""",
    )
    evidence, _, unresolved = _reads_for_handler(handler(module))
    assert {item.field for item in evidence} == {"first"}
    assert not unresolved


def test_read_owned_method_projects_fields_but_foreign_method_does_not(module_factory: Callable[[str, str], ModuleType]) -> None:
    from scripts.cicd.composer_wire_census import _reads_for_handler

    module = module_factory(
        "read_method",
        """
class Model(BaseModel):
    first: str
    second: str
    def projection(self):
        return {'first': self.first}
def arbitrary_name(arguments):
    parsed = Model.model_validate(arguments)
    foreign = Model(first='x',second='y')
    return parsed.projection(), foreign.second
""",
    )
    evidence, _, unresolved = _reads_for_handler(handler(module))
    assert {item.field for item in evidence} == {"first"}
    assert not unresolved


def test_read_helper_foreign_return_does_not_inherit_input_identity(module_factory: Callable[[str, str], ModuleType]) -> None:
    from scripts.cicd.composer_wire_census import _reads_for_handler

    module = module_factory(
        "read_foreign_return",
        """
class Model(BaseModel):
    first: str
def child(payload):
    Model.model_validate(payload)
    return Model(first='foreign')
def arbitrary_name(arguments):
    result = child(arguments)
    return result.first
""",
    )
    evidence, _, unresolved = _reads_for_handler(handler(module))
    assert not evidence
    assert not unresolved


def test_read_live_catalog_and_empty_tool_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.cicd import composer_wire_census as census

    definitions = census.get_tool_definitions()
    rows = census.census_read_wire()
    assert set(rows) == {definition["name"] for definition in definitions}
    empty = next(definition for definition in definitions if not definition["parameters"]["properties"])
    assert not rows[empty["name"]].read
    empty["parameters"]["properties"]["injected"] = {"type": "string"}
    monkeypatch.setattr(census, "get_tool_definitions", lambda: definitions)
    changed = census.census_read_wire()[empty["name"]]
    assert changed.shipped - changed.read == {"injected"}


@pytest.mark.parametrize("update,expected", [("{}", {"first", "second"}), ("{'first':'server'}", {"second"})])
def test_read_model_copy_drops_overwritten_slot_provenance(
    module_factory: Callable[[str, str], ModuleType], update: str, expected: set[str]
) -> None:
    from scripts.cicd.composer_wire_census import _reads_for_handler

    module = module_factory(
        "read_model_copy",
        "class Model(BaseModel):\n    first: str\n    second: str\n"
        "def child(payload):\n    return payload.first, payload.second\n"
        "def arbitrary_name(arguments):\n    parsed = Model.model_validate(arguments)\n"
        f"    return child(parsed.model_copy(update={update}))\n",
    )
    evidence, _, unresolved = _reads_for_handler(handler(module))
    assert {item.field for item in evidence} == expected
    assert not unresolved


@pytest.mark.parametrize("update", ["updates", "{'unknown':'x'}", "{**updates}"])
def test_read_unknown_model_copy_stays_unresolved(module_factory: Callable[[str, str], ModuleType], update: str) -> None:
    from scripts.cicd.composer_wire_census import _reads_for_handler

    module = module_factory(
        "read_opaque_copy",
        "class Model(BaseModel):\n    first: str\ndef arbitrary_name(arguments):\n"
        f"    return Model.model_validate(arguments).model_copy(update={update}).first\n",
    )
    evidence, _, unresolved = _reads_for_handler(handler(module))
    assert not evidence
    assert any("copy" in finding for finding in unresolved)


@pytest.mark.parametrize("invocation,expected", [("child()", {"first"}), ("None", set())])
def test_read_only_invoked_unshadowed_closure_receives_provenance(
    module_factory: Callable[[str, str], ModuleType], invocation: str, expected: set[str]
) -> None:
    from scripts.cicd.composer_wire_census import _reads_for_handler

    module = module_factory(
        "read_closure",
        "class Model(BaseModel):\n    first: str\n    second: str\ndef arbitrary_name(arguments):\n"
        "    parsed = Model.model_validate(arguments)\n    foreign = Model(first='a', second='b')\n"
        "    def child():\n        return parsed.first, foreign.second\n"
        f"    return {invocation}\n",
    )
    evidence, _, unresolved = _reads_for_handler(handler(module))
    assert {item.field for item in evidence} == expected
    assert not unresolved


def test_read_closure_mutated_capture_is_unresolved(module_factory: Callable[[str, str], ModuleType]) -> None:
    from scripts.cicd.composer_wire_census import _reads_for_handler

    module = module_factory(
        "read_mutated_capture",
        "class Model(BaseModel):\n    first: str\ndef arbitrary_name(arguments):\n"
        "    parsed = Model.model_validate(arguments)\n    def child():\n"
        "        nonlocal parsed\n        parsed = Model(first='foreign')\n        return parsed.first\n"
        "    return child()\n",
    )
    evidence, _, unresolved = _reads_for_handler(handler(module))
    assert not evidence
    assert any("rebound" in finding for finding in unresolved)


def test_public_read_census_contract_covers_live_registry() -> None:
    from scripts.cicd import composer_wire_census as census

    assert "census_read_wire" in vars(census), "READ census API is missing"
    assert callable(census.census_read_wire)
    rows = census.census_read_wire()
    definitions = census.get_tool_definitions()
    assert set(rows) == {definition["name"] for definition in definitions}
    for row in rows.values():
        assert row.read == frozenset(item.field for item in row.extractions)
        assert row.read == row.shipped, (row.tool, row.shipped - row.read, row.read - row.shipped)
        assert not row.unresolved, (row.tool, row.unresolved)
        assert all(item.site and item.callers for item in row.extractions)


def test_read_overridden_model_copy_is_not_pydantic_copy(module_factory: Callable[[str, str], ModuleType]) -> None:
    from scripts.cicd.composer_wire_census import _reads_for_handler

    module = module_factory(
        "read_fake_model_copy",
        "class Model(BaseModel):\n    first: str\n"
        "    def model_copy(self, *, update):\n        return Model(first='foreign')\n"
        "def arbitrary_name(arguments):\n    return Model.model_validate(arguments).model_copy(update={}).first\n",
    )
    evidence, _, unresolved = _reads_for_handler(handler(module))
    assert not evidence
    assert any("copy" in finding for finding in unresolved)


def test_read_closure_import_shadow_does_not_borrow_capture(module_factory: Callable[[str, str], ModuleType]) -> None:
    from scripts.cicd.composer_wire_census import _reads_for_handler

    module = module_factory(
        "read_import_shadow",
        "class Model(BaseModel):\n    first: str\ndef arbitrary_name(arguments):\n"
        "    parsed = Model.model_validate(arguments)\n    def child():\n"
        "        from elsewhere import parsed\n        return parsed.first\n    return child()\n",
    )
    evidence, _, unresolved = _reads_for_handler(handler(module))
    assert not evidence
    assert any("rebound" in finding for finding in unresolved)


def test_read_decorated_closure_has_no_original_body_guarantee(module_factory: Callable[[str, str], ModuleType]) -> None:
    from scripts.cicd.composer_wire_census import _reads_for_handler

    module = module_factory(
        "read_decorated_closure",
        "def replacement(function):\n    return lambda: None\n"
        "def arbitrary_name(arguments):\n    @replacement\n    def child():\n"
        "        return arguments['hidden']\n    return child()\n",
    )
    assert handler(module)({"hidden": "not read"}) is None
    evidence, _, unresolved = _reads_for_handler(handler(module))
    assert not evidence
    assert any("closure" in finding for finding in unresolved)


def test_read_overridden_model_validation_cannot_create_origin(module_factory: Callable[[str, str], ModuleType]) -> None:
    from scripts.cicd.composer_wire_census import _reads_for_handler

    module = module_factory(
        "read_overridden_validation",
        "class Model(BaseModel):\n    first: str\n    @classmethod\n"
        "    def model_validate(cls, obj):\n        return cls(first='foreign')\n"
        "def arbitrary_name(arguments):\n    return Model.model_validate(arguments).first\n",
    )
    assert handler(module)({"first": "input"}) == "foreign"
    evidence, _, unresolved = _reads_for_handler(handler(module))
    assert not evidence
    assert any("overridden" in finding for finding in unresolved)
    with pytest.raises(CensusError, match="overridden"):
        _model_for_handler(handler(module))


@pytest.mark.parametrize(
    "child,call",
    [
        ("async def child(payload):\n    return payload.first\n", "child(parsed)"),
        ("def child(payload):\n    yield payload.first\n", "child(parsed)"),
    ],
)
def test_read_dormant_helper_body_is_unresolved(module_factory: Callable[[str, str], ModuleType], child: str, call: str) -> None:
    from scripts.cicd.composer_wire_census import _reads_for_handler

    module = module_factory(
        "read_dormant_helper",
        "class Model(BaseModel):\n    first: str\n"
        + child
        + "def arbitrary_name(arguments):\n    parsed = Model.model_validate(arguments)\n"
        + f"    return {call}\n",
    )
    dormant = handler(module)({"first": "unread"})
    dormant.close()
    evidence, _, unresolved = _reads_for_handler(handler(module))
    assert not evidence
    assert any("deferred" in finding for finding in unresolved)


def test_read_local_generator_body_is_unresolved(module_factory: Callable[[str, str], ModuleType]) -> None:
    from scripts.cicd.composer_wire_census import _reads_for_handler

    module = module_factory(
        "read_dormant_local",
        "class Model(BaseModel):\n    first: str\ndef arbitrary_name(arguments):\n"
        "    parsed = Model.model_validate(arguments)\n    def child():\n        yield parsed.first\n"
        "    return child()\n",
    )
    dormant = handler(module)({"first": "unread"})
    dormant.close()
    evidence, _, unresolved = _reads_for_handler(handler(module))
    assert not evidence
    assert any("closure" in finding for finding in unresolved)


def test_read_awaited_helper_extracts_original_input(module_factory: Callable[[str, str], ModuleType]) -> None:
    from scripts.cicd.composer_wire_census import _reads_for_handler

    module = module_factory(
        "read_awaited_helper",
        "class Model(BaseModel):\n    first: str\nasync def child(payload):\n    return payload.first\n"
        "async def arbitrary_name(arguments):\n    return await child(Model.model_validate(arguments))\n",
    )
    evidence, _, unresolved = _reads_for_handler(handler(module))
    assert {item.field for item in evidence} == {"first"}
    assert not unresolved


@pytest.mark.parametrize("forward", [True, False])
def test_wrapper_must_forward_actual_original_input(module_factory: Callable[[str, str], ModuleType], forward: bool) -> None:
    from scripts.cicd.composer_wire_census import _reads_for_handler

    call = "function(*args, **kwargs)" if forward else "function({'first': 'foreign'})"
    module = module_factory(
        "read_wrapper",
        "from functools import wraps\nclass Model(BaseModel):\n    first: str\n"
        "def decorate(function):\n    @wraps(function)\n    def wrapper(*args, **kwargs):\n"
        f"        return {call}\n    return wrapper\n"
        "@decorate\ndef child(payload):\n    return Model.model_validate(payload).first\n"
        "def arbitrary_name(arguments):\n    return child(arguments)\n",
    )
    assert handler(module)({"first": "original"}) == ("original" if forward else "foreign")
    evidence, _, unresolved = _reads_for_handler(handler(module))
    if forward:
        assert {item.field for item in evidence} == {"first"}
        assert not unresolved
        assert _model_for_handler(handler(module))[0] is vars(module)["Model"]
    else:
        assert not evidence
        assert any("wrapper" in finding for finding in unresolved)
        with pytest.raises(CensusError, match="wrapper"):
            _model_for_handler(handler(module))


def test_read_captured_closure_alias_is_explicitly_unresolved(module_factory: Callable[[str, str], ModuleType]) -> None:
    from scripts.cicd.composer_wire_census import _reads_for_handler

    module = module_factory(
        "read_alias_closure",
        "def arbitrary_name(arguments):\n    def child():\n        return arguments['hidden']\n    run = child\n    return run()\n",
    )
    assert handler(module)({"hidden": "original"}) == "original"
    evidence, _, unresolved = _reads_for_handler(handler(module))
    assert not evidence
    assert any("closure alias" in finding for finding in unresolved)


@pytest.mark.parametrize("kind", ["async", "generator", "awaited"])
@pytest.mark.parametrize("wrapped", [False, True])
def test_admission_and_read_require_invoked_helper_body(module_factory: Callable[[str, str], ModuleType], kind: str, wrapped: bool) -> None:
    from scripts.cicd.composer_wire_census import _reads_for_handler

    decoration = (
        "from functools import wraps\ndef decorate(function):\n    @wraps(function)\n"
        "    def wrapper(*args, **kwargs):\n        return function(*args, **kwargs)\n    return wrapper\n"
    )
    prefix = "async " if kind != "generator" else ""
    operation = "yield" if kind == "generator" else "return"
    module = module_factory(
        "deferred_admission",
        "class Model(BaseModel):\n    first: str\n"
        + decoration
        + ("@decorate\n" if wrapped else "")
        + f"{prefix}def child(payload):\n    {operation} Model.model_validate(payload).first\n"
        + ("async " if kind == "awaited" else "")
        + "def arbitrary_name(arguments):\n    return "
        + ("await " if kind == "awaited" else "")
        + "child(arguments)\n",
    )
    evidence, _, unresolved = _reads_for_handler(handler(module))
    if kind == "awaited":
        assert _model_for_handler(handler(module))[0] is vars(module)["Model"]
        assert {item.field for item in evidence} == {"first"}
        assert not unresolved
    else:
        dormant = handler(module)({"first": "unread"})
        dormant.close()
        with pytest.raises(CensusError, match="deferred"):
            _model_for_handler(handler(module))
        assert not evidence
        assert any("deferred" in finding for finding in unresolved)
