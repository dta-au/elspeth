"""Composer-surface help text: the ``composer_description`` / ``composer_placeholder`` seam.

``Field(description=...)`` is the CLI/YAML truth for a plugin field. The web form
is a different audience, and for some fields the web surface is strictly narrower
than YAML, so a field may carry a ``composer_description`` in
``json_schema_extra`` that replaces ``description`` on that surface only. These
tests pin the substitution rules and — for the two fields that use it today —
prove the help text still describes something the runtime accepts.
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, Field

from elspeth.contracts.schema import FIELD_TYPE_MAP, SchemaConfig
from elspeth.plugins.infrastructure.config_base import (
    COMPOSER_PATH_DESCRIPTION,
    COMPOSER_SCHEMA_DESCRIPTION,
    COMPOSER_SCHEMA_EXAMPLE,
    COMPOSER_SCHEMA_PLACEHOLDER,
    DataPluginConfig,
    LocalFileSinkConfig,
    PathConfig,
)
from elspeth.plugins.sinks.json_sink import JSONSinkConfig
from elspeth.web.catalog.knob_schema import (
    KnobField,
    KnobSchema,
    KnobSchemaLoweringError,
    lower_model_to_knob_schema,
    validate_knob_schema,
)


def _lower(model_cls: type[BaseModel]) -> dict[str, KnobField]:
    schema = lower_model_to_knob_schema(model_cls, plugin_kind="source", plugin_name="fixture")
    return {field["name"]: field for field in schema["fields"]}


class _HelpTextModel(BaseModel):
    overridden: str = Field(
        description="CLI truth for a hand-authored settings file.",
        json_schema_extra={"composer_description": "Web-form truth."},
    )
    plain: str = Field(description="CLI truth only.")
    bare: str = Field()
    hinted: str = Field(
        description="CLI truth only.",
        json_schema_extra={"composer_placeholder": "type-it-like-this"},
    )


def test_composer_description_replaces_description() -> None:
    fields = _lower(_HelpTextModel)

    assert fields["overridden"]["description"] == "Web-form truth."


def test_absent_composer_description_leaves_description_untouched() -> None:
    fields = _lower(_HelpTextModel)

    assert fields["plain"]["description"] == "CLI truth only."
    assert "description" not in fields["bare"]


def test_placeholder_emitted_only_when_composer_placeholder_is_declared() -> None:
    fields = _lower(_HelpTextModel)

    assert fields["hinted"]["placeholder"] == "type-it-like-this"
    for name in ("overridden", "plain", "bare"):
        assert "placeholder" not in fields[name]


@pytest.mark.parametrize("key", ["composer_description", "composer_placeholder"])
@pytest.mark.parametrize("value", [42, "", None, ["text"]])
def test_non_string_composer_help_crashes_at_lowering(key: str, value: object) -> None:
    """A malformed extra is our own mistake in a model we author — crash, never degrade.

    Silently falling back to ``description`` would ship a composer surface
    nobody notices is wrong.
    """
    model_cls: type[BaseModel] = type(
        "MalformedHelpModel",
        (BaseModel,),
        {
            "__annotations__": {"knob": str},
            "knob": Field(description="CLI truth.", json_schema_extra={key: value}),
        },
    )

    with pytest.raises(TypeError, match=key):
        lower_model_to_knob_schema(model_cls, plugin_kind="source", plugin_name="fixture")


def test_path_knob_carries_the_web_surface_help() -> None:
    """PathConfig.path keeps its YAML description and gains web-only guidance."""
    fields = _lower(PathConfig)

    assert fields["path"]["description"] == COMPOSER_PATH_DESCRIPTION
    assert PathConfig.model_fields["path"].description is not None
    assert PathConfig.model_fields["path"].description != COMPOSER_PATH_DESCRIPTION
    # The two legal web values are both named, so a user who sees only this text
    # can act on it.
    assert "blob:<uuid>" in COMPOSER_PATH_DESCRIPTION
    assert "blobs/<session_id>/" in COMPOSER_PATH_DESCRIPTION


def test_schema_knob_carries_the_worked_example() -> None:
    """The ``schema`` knob is a bare JSON textarea; the help text must carry the grammar."""
    fields = _lower(DataPluginConfig)

    # The knob is lowered under the field's alias, not its internal name.
    assert fields["schema"]["description"] == COMPOSER_SCHEMA_DESCRIPTION
    assert DataPluginConfig.model_fields["schema_config"].description != COMPOSER_SCHEMA_DESCRIPTION


def test_schema_help_text_lists_the_authoritative_field_types() -> None:
    """A new legal type must not appear in the parser while the help text lists the old set."""
    for field_type in FIELD_TYPE_MAP:
        assert field_type in COMPOSER_SCHEMA_DESCRIPTION

    for mode in ("fixed", "flexible", "observed"):
        assert f'"{mode}"' in COMPOSER_SCHEMA_DESCRIPTION


def test_schema_help_example_is_accepted_by_the_parser() -> None:
    """Anti-rot: the example the help text shows must parse, with the optionality it claims.

    The example is imported from the module that renders it into the help text,
    so this cannot pass while the advertised text drifts to a shape the runtime
    rejects. It travels through ``json.loads(json.dumps(...))`` because a user
    types it into the form as JSON, not as a Python literal.
    """
    assert json.dumps(COMPOSER_SCHEMA_EXAMPLE) in COMPOSER_SCHEMA_DESCRIPTION

    parsed = SchemaConfig.from_dict(json.loads(json.dumps(COMPOSER_SCHEMA_EXAMPLE)))

    assert parsed.mode == "fixed"
    assert parsed.fields is not None
    assert [(field.name, field.field_type, field.required, field.nullable) for field in parsed.fields] == [
        ("doc_id", "str", True, False),
        ("page_count", "int", True, False),
        # The trailing "?" the help text calls "optional" makes the field both
        # absent-tolerant and null-tolerant.
        ("note", "str", False, True),
    ]


def test_schema_help_placeholder_is_accepted_by_the_parser() -> None:
    """The compact placeholder shape must parse too — it is what a user copies."""
    parsed = SchemaConfig.from_dict(json.loads(COMPOSER_SCHEMA_PLACEHOLDER))

    assert parsed.mode == "fixed"
    assert parsed.fields is not None
    assert [(field.name, field.required) for field in parsed.fields] == [("doc_id", True), ("note", False)]


def test_schema_placeholder_is_attached_to_the_schema_knob() -> None:
    """F5-5b landed atomically: the schema knob now carries the placeholder.

    ``placeholder`` is inside the closed knob-field vocabularies in
    ``web/composer/guided/protocol.py`` (``_validate_knob_schema``) and the
    frontend guided decoder, so emitting it on ``schema`` — a field on 46 of 47
    data plugins — is safe for every guided schema_form turn (construction,
    durable load, and replay).
    """
    extra = DataPluginConfig.model_fields["schema_config"].json_schema_extra
    assert isinstance(extra, dict)
    assert extra["composer_placeholder"] == COMPOSER_SCHEMA_PLACEHOLDER
    assert _lower(DataPluginConfig)["schema"]["placeholder"] == COMPOSER_SCHEMA_PLACEHOLDER


# ── composer_required_when: conditional requiredness (R2-F2) ─────────────────
#
# Pydantic field-level requiredness is the only requiredness the lowering knew
# about, so ``collision_policy`` (``default=None``) lowered ``required: False``
# while the composer rejects a runnable file sink that omits it
# (``web/composer/tools/_common.py`` —
# ``validate_composer_file_sink_collision_policy``). The form therefore let the
# user press Continue into a guaranteed rejection. ``required_when`` carries the
# composer's own rule onto the knob so the form can gate on it.


def test_collision_policy_lowers_required_when_mode_is_write() -> None:
    """The composer's mode='write' collision rule reaches the form as a predicate."""
    fields = _lower(LocalFileSinkConfig)

    assert fields["collision_policy"]["required_when"] == {"field": "mode", "equals": "write"}
    # Pydantic-level requiredness is unchanged: YAML may still omit the field.
    assert fields["collision_policy"]["required"] is False


def test_collision_policy_help_names_auto_increment_as_the_safe_default() -> None:
    """The correct guidance existed only as an LLM-only hint; the form needs it too."""
    description = _lower(LocalFileSinkConfig)["collision_policy"]["description"]

    assert "auto_increment" in description
    assert "safe default" in description
    # The stale "Optional" framing must be gone from both surfaces.
    assert "Optional" not in description
    yaml_description = LocalFileSinkConfig.model_fields["collision_policy"].description
    assert yaml_description is not None
    assert not yaml_description.startswith("Optional")
    assert "mode='write'" in yaml_description
    assert "'append_or_create' only with mode='append'" in yaml_description


def test_required_when_may_reference_a_later_declared_field() -> None:
    """Unlike visible_when, a required_when target need not be declared first.

    ``mode`` is declared on the concrete sink subclass and therefore lowers
    AFTER ``collision_policy`` (which lives on ``LocalFileSinkConfig``). A
    required_when predicate only reads sibling form state — every field is
    rendered — so declaration order is irrelevant and the ordering rule
    visible_when enforces must not be ported.
    """
    schema = lower_model_to_knob_schema(JSONSinkConfig, plugin_kind="sink", plugin_name="json")
    names = [field["name"] for field in schema["fields"]]
    assert names.index("collision_policy") < names.index("mode")

    validate_knob_schema(schema, plugin_kind="sink", plugin_name="json")


def test_required_when_target_must_exist() -> None:
    """A predicate naming no field at all is a lowering bug, not a silent no-op."""
    schema: KnobSchema = {
        "fields": [
            {
                "name": "knob",
                "label": "Knob",
                "kind": "text",
                "required": False,
                "nullable": False,
                "required_when": {"field": "nonexistent", "equals": "x"},
            }
        ]
    }

    with pytest.raises(KnobSchemaLoweringError, match="required_when"):
        validate_knob_schema(schema, plugin_kind="sink", plugin_name="fixture")


@pytest.mark.parametrize("value", [42, "mode", {"field": "mode"}, {"field": "mode", "equals": "write", "or": 1}])
def test_malformed_composer_required_when_crashes_at_lowering(value: object) -> None:
    """A malformed extra is our own mistake in a model we author — crash, never degrade."""
    model_cls: type[BaseModel] = type(
        "MalformedRequiredWhenModel",
        (BaseModel,),
        {
            "__annotations__": {"knob": str},
            "knob": Field(default=None, json_schema_extra={"composer_required_when": value}),
        },
    )

    with pytest.raises(TypeError, match="composer_required_when"):
        lower_model_to_knob_schema(model_cls, plugin_kind="sink", plugin_name="fixture")
