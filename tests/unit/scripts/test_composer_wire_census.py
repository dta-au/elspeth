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
