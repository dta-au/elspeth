"""P5 sweep: DatabaseSink and ChromaSink admit exactly the write modes that work.

DatabaseSink typed ``if_exists`` as ``Literal["append", "replace"]`` but refused
``replace`` at effect validation; ChromaSink typed ``on_duplicate`` as
``Literal["overwrite", "skip", "error"]`` but refused everything except
``overwrite`` at prepare time while its composer hint advertised all three.
The config type is the contract: a value it admits must work, so the dead
values are refused at config validation and no guidance names them.

ChromaSink also accepted a whitespace-only document id. Chroma treats it as a
real id, so a row whose id is blank in every meaningful sense was published
under an unaddressable key; it must divert per member exactly like an empty id.
"""

from __future__ import annotations

import hashlib
from typing import Any

from elspeth.contracts.sink_effects import SinkEffectMember, SinkEffectPipelineMembersInput
from elspeth.core.canonical import canonical_json
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.sinks.chroma_sink import ChromaSink, ChromaSinkConfig
from elspeth.plugins.sinks.database_sink import DatabaseSink, DatabaseSinkConfig
from tests.fixtures.base_classes import inject_write_failure

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


def _chroma_options(**overrides: object) -> dict[str, Any]:
    options: dict[str, Any] = {
        "collection": "p5-sweep",
        "mode": "persistent",
        "persist_directory": "./unused",
        "field_mapping": {"document_field": "text", "id_field": "doc_id", "metadata_fields": []},
        "schema": {"mode": "observed"},
    }
    options.update(overrides)
    return options


def _config_refused(config_model: type[DatabaseSinkConfig] | type[ChromaSinkConfig], options: dict[str, Any], plugin: str) -> bool:
    try:
        config_model.from_dict(options, plugin_name=plugin)
    except PluginConfigError:
        return True
    return False


def _member(ordinal: int, row: dict[str, object]) -> SinkEffectMember:
    lineage_json = canonical_json([f"row-{ordinal}"])
    return SinkEffectMember(
        ordinal=ordinal,
        token_id=f"token-{ordinal}",
        row_id=f"row-{ordinal}",
        ingest_sequence=ordinal,
        lineage_json=lineage_json,
        lineage_hash=hashlib.sha256(lineage_json.encode("utf-8")).hexdigest(),
        payload_hash=hashlib.sha256(canonical_json(row).encode("utf-8")).hexdigest(),
        row=row,
    )


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


# ------------------------------------------------------------------ ChromaSink


def test_chroma_on_duplicate_overwrite_is_accepted() -> None:
    cfg = ChromaSinkConfig.from_dict(_chroma_options(on_duplicate="overwrite"), plugin_name="chroma_sink")
    assert cfg.on_duplicate == "overwrite"


def test_chroma_on_duplicate_skip_is_refused_at_config_validation() -> None:
    assert _config_refused(ChromaSinkConfig, _chroma_options(on_duplicate="skip"), "chroma_sink"), (
        "on_duplicate='skip' is typed as admissible but prepare_effect refuses it; the config type must refuse it"
    )


def test_chroma_on_duplicate_error_is_refused_at_config_validation() -> None:
    assert _config_refused(ChromaSinkConfig, _chroma_options(on_duplicate="error"), "chroma_sink"), (
        "on_duplicate='error' is typed as admissible but prepare_effect refuses it; the config type must refuse it"
    )


def test_chroma_guidance_does_not_advertise_skip_or_error() -> None:
    assistance = ChromaSink.get_agent_assistance()
    assert assistance is not None
    text = " ".join(assistance.composer_hints)
    assert "skip" not in text, "composer hints still advertise on_duplicate='skip'"
    assert "error" not in text.replace("on_write_failure", ""), "composer hints still advertise on_duplicate='error'"


def test_chroma_whitespace_only_id_diverts_while_valid_sibling_is_published() -> None:
    sink = inject_write_failure(ChromaSink(_chroma_options()))
    members = [
        _member(0, {"doc_id": "   ", "text": "blank id"}),
        _member(1, {"doc_id": "\t\n", "text": "blank id"}),
        _member(2, {"doc_id": "doc-2", "text": "valid"}),
    ]
    effect_input = SinkEffectPipelineMembersInput(members, members, len(members))
    _descriptor, _payload_hash, _plan_hash, extracted_by_ordinal, diversions = sink._member_effect_material("0" * 64, effect_input)
    diverted = dict(diversions)
    assert sorted(diverted) == [0, 1], f"whitespace-only ids must divert; diversions were {diversions!r}"
    assert all("id" in reason for reason in diverted.values())
    assert sorted(extracted_by_ordinal) == [2]
    assert extracted_by_ordinal[2][0] == "doc-2"


def test_dead_on_duplicate_error_exception_is_removed() -> None:
    # DuplicateDocumentError existed only for on_duplicate='error', which no
    # longer exists; nothing raises it, so it must not stay in the contracts.
    import elspeth.contracts as contracts
    import elspeth.contracts.errors as contract_errors

    assert "DuplicateDocumentError" not in vars(contract_errors)
    assert "DuplicateDocumentError" not in vars(contracts)
    assert "DuplicateDocumentError" not in contracts.__all__
