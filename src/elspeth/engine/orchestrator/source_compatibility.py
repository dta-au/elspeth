"""Reject source-run drift before a replay or verify plugin effect."""

from __future__ import annotations

import json
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.canonical import stable_hash

if TYPE_CHECKING:
    from elspeth.core.landscape.factory import LandscapeReadRepositories, RecorderFactory
    from elspeth.engine.orchestrator.types import PipelineConfig


_INVOCATION_FIELDS = frozenset({"run_mode", "replay_from"})


def _first_different_setting(source: Any, current: Any, path: str = "settings") -> str:
    if type(source) in (dict, MappingProxyType) and type(current) in (dict, MappingProxyType):
        for key in sorted(source.keys() | current.keys()):
            if key not in source or key not in current:
                return f"{path}.{key}"
            different = _first_different_setting(source[key], current[key], f"{path}.{key}")
            if different:
                return different
        return ""
    return path if stable_hash(source) != stable_hash(current) else ""


def admit_source_configuration(
    factory: LandscapeReadRepositories | RecorderFactory,
    *,
    source_run_id: str,
    config: PipelineConfig,
    canonical_version: str,
) -> None:
    """Bind effective execution settings and canonicalizer to the source run.

    ``run_mode`` and ``replay_from`` change only the invocation. Every other
    setting is an execution input and must agree exactly. The source record's
    own config hash is checked before any field is trusted.
    """
    source = factory.run_lifecycle.get_run(source_run_id)
    if source is None:
        raise AuditIntegrityError(f"Replay source run {source_run_id!r} disappeared")
    try:
        source_settings = json.loads(source.settings_json)
    except json.JSONDecodeError as exc:
        raise AuditIntegrityError("Replay source settings are malformed") from exc
    if type(source_settings) is not dict or stable_hash(source_settings) != source.config_hash:
        raise AuditIntegrityError("Replay source settings hash is invalid")
    if source.canonical_version != canonical_version:
        raise AuditIntegrityError("Replay source canonical version differs")
    current_settings: dict[str, Any] = dict(config.config)
    for field in _INVOCATION_FIELDS:
        if field in source_settings:
            del source_settings[field]
        if field in current_settings:
            del current_settings[field]
    source_concurrency = source_settings["concurrency"] if "concurrency" in source_settings else None
    current_concurrency = current_settings["concurrency"] if "concurrency" in current_settings else None
    if type(source_concurrency) in (dict, MappingProxyType) and type(current_concurrency) in (dict, MappingProxyType):
        source_options = cast("dict[str, Any] | MappingProxyType[str, Any]", source_concurrency)
        current_options = cast("dict[str, Any] | MappingProxyType[str, Any]", current_concurrency)
        if "max_workers" in current_options and current_options["max_workers"] == 1:
            source_settings["concurrency"] = {key: value for key, value in source_options.items() if key != "max_workers"}
            current_settings["concurrency"] = {key: value for key, value in current_options.items() if key != "max_workers"}
    if stable_hash(source_settings) != stable_hash(current_settings):
        path = _first_different_setting(source_settings, current_settings)
        raise AuditIntegrityError(f"Replay execution settings differ from the source run at {path}")


def admit_registered_graph(factory: RecorderFactory, *, source_run_id: str, current_run_id: str) -> None:
    """Compare registered graph, plugin implementation, and route edges.

    Registration performs only new-run audit writes. This check runs before
    source, transform, or sink lifecycle hooks, so a drift cannot cause plugin
    effects before it is rejected.
    """
    source_nodes = factory.data_flow.get_nodes(source_run_id)
    current_nodes = factory.data_flow.get_nodes(current_run_id)

    def identity(node: Any) -> tuple[Any, ...]:
        return (
            node.node_id,
            node.node_type,
            node.plugin_name,
            node.plugin_version,
            node.source_file_hash,
            node.config_hash,
            node.determinism,
        )

    if {identity(node) for node in source_nodes} != {identity(node) for node in current_nodes}:
        raise AuditIntegrityError("Replay graph nodes, config, or plugin implementation differ from source run")
    source_edges = factory.data_flow.get_edges(source_run_id)
    current_edges = factory.data_flow.get_edges(current_run_id)

    def edge_identity(edge: Any) -> tuple[Any, ...]:
        return (edge.from_node_id, edge.to_node_id, edge.label, edge.default_mode)

    if sorted(edge_identity(edge) for edge in source_edges) != sorted(edge_identity(edge) for edge in current_edges):
        raise AuditIntegrityError("Replay graph route edges differ from source run")
