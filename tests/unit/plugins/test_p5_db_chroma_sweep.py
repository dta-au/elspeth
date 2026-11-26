"""P5 sweep: DatabaseSink admits exactly the write modes that work.

DatabaseSink typed ``if_exists`` as ``Literal["append", "replace"]`` but refused
``replace`` at effect validation. The config type is the contract: a value it
admits must work, so the dead value is refused at config validation and no
guidance names it. (The ChromaSink half of this sweep left with the plugin in
0.8.18.)
"""

from __future__ import annotations

from typing import Any

from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.sinks.database_sink import DatabaseSink, DatabaseSinkConfig

_EFFECT_LEDGER = {"table": "_elspeth_sink_effects", "schema_version": 1, "permissions": ["insert", "select"]}


def _database_options(**overrides: object) -> dict[str, Any]:
    options: dict[str, Any] = {
        "url": "sqlite:///:memory:",
        "table": "output",
        "schema": {"mode": "observed"},
        "effect_ledger": _EFFECT_LEDGER,
    }
    options.update(overrides)
    return options


def _config_refused(config_model: type[DatabaseSinkConfig], options: dict[str, Any], plugin: str) -> bool:
    try:
        config_model.from_dict(options, plugin_name=plugin)
    except PluginConfigError:
        return True
    return False


# ---------------------------------------------------------------- DatabaseSink


def test_database_if_exists_append_is_accepted() -> None:
    cfg = DatabaseSinkConfig.from_dict(_database_options(if_exists="append"), plugin_name="database")
    assert cfg.if_exists == "append"


def test_database_if_exists_replace_is_refused_at_config_validation() -> None:
    assert _config_refused(DatabaseSinkConfig, _database_options(if_exists="replace"), "database"), (
        "if_exists='replace' is typed as admissible but the sink cannot perform it; the config type must refuse it"
    )


def test_database_guidance_does_not_name_replace() -> None:
    assistance = DatabaseSink.get_agent_assistance()
    assert assistance is not None
    text = " ".join(assistance.composer_hints).lower()
    assert "replace" not in text, "composer hints still describe the removed 'replace' value"
    assert DatabaseSink.get_post_call_hints(tool_name="patch_output_options", config_snapshot={"if_exists": "replace"}) == ()
