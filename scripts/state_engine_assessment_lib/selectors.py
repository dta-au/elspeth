"""Validation for the catalog-wide v3 evidence selector manifest."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .catalog import validate_catalog
from .common import (
    _dict,
    _git_root,
    _list,
    _repository_path,
    _require,
    _string,
    _strings,
    _unique,
    load_unique_json,
)

CATALOG_ID = "elspeth-state-engine-v3"
POSTGRESQL_PROFILE = "postgresql-16-aws-single-leader-landscape"
ARTIFACT_KINDS = (
    "manifest",
    "junit_xml",
    "profile_report",
    "node_index",
    "scrub_report",
)

COMMON_LIVE_RESOURCES = (
    "AWS_REGION",
    "ELSPETH_STATE_ENGINE_AWS_POSTGRES_URL",
    "ELSPETH_STATE_ENGINE_AWS_DEPLOYMENT_PROVENANCE",
)
AWS_RESOURCES = (
    "ELSPETH_TEST_S3_BUCKET",
    "ELSPETH_TEST_TEXTRACT_REGION",
    "ELSPETH_TEST_TEXTRACT_BUCKET",
    "ELSPETH_TEST_TEXTRACT_KEY",
    "ELSPETH_TEST_TEXTRACT_EXPECTED_TEXT",
    "ELSPETH_BEDROCK_LIVE_TEST_MODEL",
    "ELSPETH_BEDROCK_LIVE_ADVISOR_MODEL",
    "ELSPETH_LIVE_BEDROCK_PROMPT_PROFILE_ALIAS",
    "ELSPETH_LIVE_BEDROCK_PROMPT_SAFE_TEXT",
    "ELSPETH_LIVE_BEDROCK_PROMPT_BLOCKED_TEXT",
    "ELSPETH_LIVE_BEDROCK_PROMPT_EXPECTED_VERSION",
    "ELSPETH_LIVE_BEDROCK_CONTENT_PROFILE_ALIAS",
    "ELSPETH_LIVE_BEDROCK_CONTENT_SAFE_TEXT",
    "ELSPETH_LIVE_BEDROCK_CONTENT_BLOCKED_TEXT",
    "ELSPETH_LIVE_BEDROCK_CONTENT_EXPECTED_VERSION",
)
AZURE_RESOURCES = (
    "ELSPETH_TEST_AZURE_STORAGE_ACCOUNT_URL",
    "ELSPETH_TEST_AZURE_STORAGE_CONTAINER",
    "ELSPETH_TEST_AZURE_STORAGE_BLOB_PREFIX",
    "ELSPETH_TEST_AZURE_CONTENT_SAFETY_ENDPOINT",
    "ELSPETH_TEST_AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT",
    "ELSPETH_TEST_AZURE_DOCUMENT_INTELLIGENCE_MODEL",
    "ELSPETH_TEST_AZURE_DOCUMENT_URL",
    # Credential variable NAMES only (never values): the safety/document
    # transforms read these through their production-declared
    # discovery_secret_requirements, and the service-principal blob variant
    # reads azure-identity's EnvironmentCredential trio.
    "AZURE_CONTENT_SAFETY_KEY",
    "AZURE_DOCUMENT_INTELLIGENCE_KEY",
    "AZURE_TENANT_ID",
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
)
DATAVERSE_RESOURCES = (
    "ELSPETH_TEST_DATAVERSE_URL",
    "ELSPETH_TEST_DATAVERSE_ENTITY",
    "ELSPETH_TEST_DATAVERSE_ROW_PREFIX",
    # azure-identity EnvironmentCredential trio for the service_principal
    # variant (names owned by the SDK contract, values never recorded).
    "AZURE_TENANT_ID",
    "AZURE_CLIENT_ID",
    "AZURE_CLIENT_SECRET",
)
CHROMA_RESOURCES = (
    "ELSPETH_TEST_CHROMA_HOST",
    "ELSPETH_TEST_CHROMA_PORT",
    "ELSPETH_TEST_CHROMA_SSL",
    "ELSPETH_TEST_CHROMA_TENANT",
    "ELSPETH_TEST_CHROMA_DATABASE",
    "ELSPETH_TEST_CHROMA_COLLECTION_PREFIX",
)
PROVIDER_RESOURCES = (
    "ELSPETH_TEST_AZURE_LLM_ENDPOINT",
    "ELSPETH_TEST_AZURE_LLM_MODEL",
    "ELSPETH_TEST_OPENROUTER_MODEL",
    "ELSPETH_TEST_BEDROCK_MODEL",
    "ELSPETH_TEST_LLM_GATEWAY_URL",
    "ELSPETH_TEST_LLM_GATEWAY_MODEL",
    "ELSPETH_TEST_AZURE_SEARCH_ENDPOINT",
    "ELSPETH_TEST_AZURE_SEARCH_INDEX",
    # Credential variable NAMES only (never values): the repository's
    # established production credential names for the four credentialed
    # provider variants (azure / openrouter / gateway LLM, azure-search).
    "AZURE_API_KEY",
    "OPENROUTER_API_KEY",
    "GATEWAY_BEARER",
    "AZURE_SEARCH_API_KEY",
    *CHROMA_RESOURCES,
)


@dataclass(frozen=True)
class LaneSpec:
    """Closed execution identity for one selector lane."""

    workflow_job: str | None
    profile_case: str
    marker_expression: str
    resource_variables: tuple[str, ...]


#: The closed fixed lanes: three local SQLite profiles plus the AWS
#: composition lane. Every other lane is DERIVED per non-local PB-09
#: (plugin_key, variant_id) pair by :func:`lane_specs`.
FIXED_LANE_SPECS: dict[str, LaneSpec] = {
    "local-sqlite-wal-single-process-leader": LaneSpec(
        None,
        "sqlite-wal-single-process-leader",
        "not live_provider",
        (),
    ),
    "local-sqlite-wal-same-host-leader-plus-claim-only-followers": LaneSpec(
        None,
        "sqlite-wal-same-host-leader-plus-claim-only-followers",
        "not live_provider",
        (),
    ),
    "local-sqlite-wal-web-hosted-leader-plus-same-host-cli-followers": LaneSpec(
        None,
        "sqlite-wal-web-hosted-leader-plus-same-host-cli-followers",
        "not live_provider",
        (),
    ),
    "postgresql-16-aws": LaneSpec(
        "live_aws",
        POSTGRESQL_PROFILE,
        "live_aws and live_provider",
        COMMON_LIVE_RESOURCES,
    ),
}

#: Execution identity shared by every derived per-case lane of one release
#: lane: the release lane's workflow job, marker expression, and resource
#: vocabulary, always at the PostgreSQL AWS profile.
_RELEASE_LANE_JOB_SPECS: dict[str, LaneSpec] = {
    "live-aws": LaneSpec(
        "live_aws",
        POSTGRESQL_PROFILE,
        "live_aws and live_provider",
        (*COMMON_LIVE_RESOURCES, *AWS_RESOURCES),
    ),
    "live-azure": LaneSpec(
        "live_azure",
        POSTGRESQL_PROFILE,
        "live_azure and live_provider",
        (*COMMON_LIVE_RESOURCES, *AZURE_RESOURCES),
    ),
    "live-dataverse": LaneSpec(
        "live_dataverse",
        POSTGRESQL_PROFILE,
        "live_dataverse and live_provider",
        (*COMMON_LIVE_RESOURCES, *DATAVERSE_RESOURCES),
    ),
    "live-chroma": LaneSpec(
        "live_chroma",
        POSTGRESQL_PROFILE,
        "live_chroma and live_provider",
        (*COMMON_LIVE_RESOURCES, *CHROMA_RESOURCES),
    ),
    "live-provider": LaneSpec(
        "live_provider",
        POSTGRESQL_PROFILE,
        "live_provider",
        (*COMMON_LIVE_RESOURCES, *PROVIDER_RESOURCES),
    ),
}


def derived_lane_id(plugin_key: str, variant_id: str) -> str:
    """Deterministic per-case protected lane identity for a PB-09 pair.

    This MUST match ``lane_id_for_case`` in
    ``scripts/state_engine_live_provider_artifact.py``: the workflow case ID
    ``{plugin_key}@{variant_id}`` maps to exactly this lane.
    """
    return "protected-live-" + plugin_key.replace(":", "-") + "-" + variant_id


def lane_specs(
    pb09_variants: set[tuple[str, str]],
    plugin_release_lanes: Mapping[str, str],
) -> dict[str, LaneSpec]:
    """Full closed lane inventory: fixed lanes, then derived lanes by lane_id.

    One derived protected lane exists per non-local PB-09
    ``(plugin_key, variant_id)`` pair; local-release PB-09 cases stay on the
    fixed ``postgresql-16-aws`` composition lane.
    """
    derived: dict[str, LaneSpec] = {}
    for plugin_key, variant_id in pb09_variants:
        release_lane = plugin_release_lanes[plugin_key]
        if release_lane == "local":
            continue
        lane_id = derived_lane_id(plugin_key, variant_id)
        _require(lane_id not in FIXED_LANE_SPECS and lane_id not in derived, f"derived selector lane identity collision: {lane_id}")
        derived[lane_id] = _RELEASE_LANE_JOB_SPECS[release_lane]
    return {**FIXED_LANE_SPECS, **{lane_id: derived[lane_id] for lane_id in sorted(derived)}}


_TOP_LEVEL_FIELDS = {"schema_version", "catalog_id", "lanes"}
_LANE_FIELDS = {
    "lane_id",
    "workflow_job",
    "profile_case",
    "marker_expression",
    "node_ids",
    "cells",
    "resource_variables",
    "artifact_kinds",
}
_CELL_FIELDS = {"leg_id", "dimension_id", "case_id", "profile_case"}
_PROTECTED_CELL_FIELDS = _CELL_FIELDS | {"node_ids"}

Cell = tuple[str, str, str, str]
Collector = Callable[[Path, str, Sequence[str]], list[str]]


def _collect_pytest_node_ids(repository_root: Path, marker_expression: str, node_ids: Sequence[str]) -> list[str]:
    """Collect exact pytest nodes without executing them."""
    environment = os.environ.copy()
    source_path = str(repository_root / "src")
    prior_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = source_path if prior_pythonpath is None else f"{source_path}{os.pathsep}{prior_pythonpath}"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:dotenv",
            "--collect-only",
            "-q",
            "-n",
            "0",
            "-m",
            marker_expression,
            *node_ids,
        ],
        cwd=repository_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    _require(
        result.returncode == 0,
        f"pytest selector collection failed for marker {marker_expression!r}: {result.stderr.strip()}",
    )
    return [line.strip() for line in result.stdout.splitlines() if line.startswith("tests/") and "::" in line]


def _catalog_cells(
    catalog: Mapping[str, Any],
) -> tuple[set[Cell], set[tuple[str, str]], dict[tuple[str, str], str | None], dict[tuple[str, str], str]]:
    required_cells: set[Cell] = set()
    pb09_variants: set[tuple[str, str]] = set()
    plugin_by_subject: dict[tuple[str, str], str | None] = {}
    variant_by_subject: dict[tuple[str, str], str] = {}
    legs = _list(catalog.get("legs"), "catalog legs")
    for raw_leg in legs:
        leg = _dict(raw_leg, "catalog leg")
        leg_id = _string(leg.get("id"), "catalog leg id")
        for raw_case in _list(leg.get("required_cases"), f"catalog leg {leg_id} required_cases"):
            case = _dict(raw_case, f"catalog leg {leg_id} case")
            case_id = _string(case.get("case_id"), f"catalog leg {leg_id} case_id")
            plugin_key = case.get("plugin_key")
            if plugin_key is not None:
                plugin_key = _string(plugin_key, f"catalog leg {leg_id} plugin_key")
            plugin_by_subject[(leg_id, case_id)] = plugin_key
            if leg_id == "PB-09":
                _require(plugin_key is not None, f"catalog PB-09 case {case_id} has no plugin key")
                variant_id = _string(case.get("variant_id"), f"catalog PB-09 case {case_id} variant_id")
                pb09_variants.add((cast(str, plugin_key), variant_id))
                variant_by_subject[(leg_id, case_id)] = variant_id
            applicability = _dict(case.get("cell_applicability"), f"catalog leg {leg_id} case {case_id} applicability")
            for profile_case, raw_dimensions in applicability.items():
                dimensions = _dict(raw_dimensions, f"catalog leg {leg_id} case {case_id} profile {profile_case}")
                for dimension_id, raw_policy in dimensions.items():
                    policy = _dict(raw_policy, f"catalog cell {leg_id}/{case_id}/{profile_case}/{dimension_id}")
                    if policy.get("status") == "required":
                        required_cells.add((leg_id, dimension_id, case_id, profile_case))
    return required_cells, pb09_variants, plugin_by_subject, variant_by_subject


def _target_lane(
    cell: Cell,
    plugin_by_subject: Mapping[tuple[str, str], str | None],
    variant_by_subject: Mapping[tuple[str, str], str],
    release_lanes: Mapping[str, str],
) -> str:
    leg_id, _dimension_id, case_id, profile_case = cell
    if profile_case.startswith("sqlite-wal-"):
        return f"local-{profile_case}"
    _require(profile_case == POSTGRESQL_PROFILE, f"required cell references unsupported profile {profile_case!r}")
    plugin_key = plugin_by_subject[(leg_id, case_id)]
    if leg_id != "PB-09" or plugin_key is None or release_lanes[plugin_key] == "local":
        return "postgresql-16-aws"
    return derived_lane_id(plugin_key, variant_by_subject[(leg_id, case_id)])


def _validate_full_node_id(repository_root: Path, lane_id: str, node_id: str) -> None:
    _require(node_id.startswith("tests/") and ".py::" in node_id, f"selector lane {lane_id} node must be a full pytest node ID")
    selector_path = node_id.split("::", 1)[0]
    path = _repository_path(repository_root, selector_path, f"selector lane {lane_id} node")
    _require(path.is_relative_to(repository_root / "tests"), f"selector lane {lane_id} node must be under tests/")
    _require(path.is_file(), f"selector lane {lane_id} node path does not exist: {selector_path}")
    # A parametrization ID may legally contain "::" (e.g. the plugin lifecycle
    # matrix's "profile::plugin" case IDs), so derive the function leaf from
    # the node ID with any "[...]" suffix removed.
    leaf = node_id.split("[", 1)[0].rsplit("::", 1)[-1]
    _require(leaf.startswith("test_"), f"selector lane {lane_id} node must be a full pytest node ID")


def validate_selector_manifest_data(
    manifest: Mapping[str, Any],
    catalog: Mapping[str, Any],
    *,
    pb09_variants: set[tuple[str, str]],
    plugin_release_lanes: Mapping[str, str],
    collector: Collector,
    repository_root: Path,
) -> None:
    """Validate an admitted selector manifest against catalog and live inventories.

    Schema 2 cell semantics: a PROTECTED lane's cell (``workflow_job`` is not
    null) may carry an optional ``node_ids`` array naming the exact lane nodes
    that prove that cell; a protected cell WITHOUT ``node_ids`` is
    assigned-but-not-yet-provable and stays honestly unknown at assessment.
    LOCAL lanes (``workflow_job`` null) must never carry per-cell ``node_ids``
    — their coverage is authored at assessment time from retained reporter
    runs. The no-orphan-node rule (every lane node referenced by at least one
    cell mapping) applies only to protected lanes that carry ANY mapping: a
    protected lane with zero mapped cells — the ``postgresql-16-aws``
    composition lane initially — keeps its node list purely as its dispatch
    selector, with coverage authored at ingest/assessment time once its cells
    are mapped.
    """
    _require(set(manifest) == _TOP_LEVEL_FIELDS, "selector manifest has unexpected top-level fields")
    _require(
        type(manifest.get("schema_version")) is int and manifest.get("schema_version") == 2, "selector manifest schema_version must be 2"
    )
    _require(manifest.get("catalog_id") == CATALOG_ID, f"selector manifest catalog_id must be {CATALOG_ID}")
    _require(
        catalog.get("catalog_id") == CATALOG_ID and type(catalog.get("schema_version")) is int and catalog.get("schema_version") == 2,
        "selector manifest requires catalog v3 schema 2",
    )

    required_cells, catalog_variants, plugin_by_subject, variant_by_subject = _catalog_cells(catalog)
    _require(catalog_variants == pb09_variants, "PB-09 variant inventory drift between catalog and production discriminators")
    catalog_plugin_keys = {plugin_key for plugin_key, _variant in catalog_variants}
    _require(set(plugin_release_lanes) == catalog_plugin_keys, "plugin release-lane inventory drift")
    _require(
        set(plugin_release_lanes.values()) <= {"local", *_RELEASE_LANE_JOB_SPECS},
        "plugin release-lane inventory contains an unknown lane",
    )
    specs = lane_specs(pb09_variants, plugin_release_lanes)

    raw_lanes = _list(manifest.get("lanes"), "selector manifest lanes")
    lane_ids = [_string(_dict(raw_lane, "selector lane").get("lane_id"), "selector lane_id") for raw_lane in raw_lanes]
    _unique(lane_ids, "selector lane IDs")
    _require(lane_ids == list(specs), "selector manifest lanes do not match the closed lane inventory")

    actual_cells: set[Cell] = set()
    node_owner: dict[str, str] = {}
    for raw_lane in raw_lanes:
        lane = _dict(raw_lane, "selector lane")
        lane_id = _string(lane.get("lane_id"), "selector lane_id")
        spec = specs[lane_id]
        _require(set(lane) == _LANE_FIELDS, f"selector lane {lane_id} has unexpected lane fields")
        _require(lane.get("workflow_job") == spec.workflow_job, f"selector lane {lane_id} has the wrong workflow job")
        _require(lane.get("profile_case") == spec.profile_case, f"selector lane {lane_id} has the wrong profile")
        _require(lane.get("marker_expression") == spec.marker_expression, f"selector lane {lane_id} has the wrong marker expression")

        resources = _strings(lane.get("resource_variables"), f"selector lane {lane_id} resource variables")
        _unique(resources, f"selector lane {lane_id} resource variables")
        _require(resources == list(spec.resource_variables), f"selector lane {lane_id} has invalid resource variable vocabulary")
        artifacts = _strings(lane.get("artifact_kinds"), f"selector lane {lane_id} artifact kinds")
        _require(artifacts == list(ARTIFACT_KINDS), f"selector lane {lane_id} has invalid artifact kinds")

        node_ids = _strings(lane.get("node_ids"), f"selector lane {lane_id} node_ids")
        _require(bool(node_ids), f"selector lane {lane_id} requires at least one node")
        _unique(node_ids, f"selector lane {lane_id} node_ids")
        for node_id in node_ids:
            _validate_full_node_id(repository_root, lane_id, node_id)
            if spec.profile_case == POSTGRESQL_PROFILE:
                _require(
                    not node_id.startswith("tests/testcontainer/"),
                    f"selector lane {lane_id} cannot substitute a testcontainer node for the PostgreSQL AWS profile",
                )
            prior_lane = node_owner.setdefault(node_id, lane_id)
            _require(prior_lane == lane_id, f"selector node {node_id} must establish only one proof subject")

        collected = collector(repository_root, spec.marker_expression, node_ids)
        _require(collected == node_ids, f"selector lane {lane_id} pytest collection drift: expected {node_ids}, collected {collected}")

        raw_cells = _list(lane.get("cells"), f"selector lane {lane_id} cells")
        _require(bool(raw_cells), f"selector lane {lane_id} requires at least one cell")
        mapped_nodes: set[str] = set()
        lane_carries_mapping = False
        for raw_cell in raw_cells:
            cell = _dict(raw_cell, f"selector lane {lane_id} cell")
            if spec.workflow_job is None:
                _require(set(cell) == _CELL_FIELDS, f"selector lane {lane_id} cell has unexpected cell fields")
            else:
                _require(
                    set(cell) in (_CELL_FIELDS, _PROTECTED_CELL_FIELDS),
                    f"selector lane {lane_id} cell has unexpected cell fields",
                )
            parsed: Cell = (
                _string(cell.get("leg_id"), f"selector lane {lane_id} cell leg_id"),
                _string(cell.get("dimension_id"), f"selector lane {lane_id} cell dimension_id"),
                _string(cell.get("case_id"), f"selector lane {lane_id} cell case_id"),
                _string(cell.get("profile_case"), f"selector lane {lane_id} cell profile_case"),
            )
            _require(parsed not in actual_cells, f"duplicate selector cell: {parsed}")
            _require(parsed in required_cells, f"selector manifest contains invented or not_applicable cells: {parsed}")
            expected_lane = _target_lane(parsed, plugin_by_subject, variant_by_subject, plugin_release_lanes)
            _require(expected_lane == lane_id, f"selector cell {parsed} is in the wrong selector lane; expected {expected_lane}")
            _require(parsed[3] == spec.profile_case, f"selector lane {lane_id} cell has the wrong profile")
            actual_cells.add(parsed)
            if "node_ids" in cell:
                cell_nodes = _strings(cell.get("node_ids"), f"selector lane {lane_id} cell node_ids")
                _require(bool(cell_nodes), f"selector lane {lane_id} cell node_ids must be non-empty when present")
                _unique(cell_nodes, f"selector lane {lane_id} cell node_ids")
                _require(
                    set(cell_nodes) <= set(node_ids),
                    f"selector lane {lane_id} cell node_ids must be a subset of the lane node_ids: {parsed}",
                )
                lane_carries_mapping = True
                mapped_nodes.update(cell_nodes)
        if spec.workflow_job is not None and lane_carries_mapping:
            orphan_nodes = sorted(set(node_ids) - mapped_nodes)
            _require(
                not orphan_nodes,
                f"selector lane {lane_id} has orphan nodes referenced by no cell mapping: {orphan_nodes[:5]}",
            )

    missing = sorted(required_cells - actual_cells)
    unexpected = sorted(actual_cells - required_cells)
    _require(not missing, f"selector manifest is missing required cells: {missing[:10]}")
    _require(not unexpected, f"selector manifest contains invented or not_applicable cells: {unexpected[:10]}")


def validate_selector_manifest(
    manifest_path: Path,
    catalog_path: Path,
    *,
    repository_root: Path | None = None,
) -> None:
    """Validate a checked-in selector manifest using production-owned facts."""
    root = _git_root(manifest_path) if repository_root is None else repository_root.resolve()
    _require(manifest_path.is_file(), f"selector manifest does not exist: {manifest_path}")
    manifest = load_unique_json(manifest_path)
    catalog = load_unique_json(catalog_path)
    validate_catalog(catalog, catalog_path)

    matrix_path = root / "tests/golden/state_engine/plugin_lifecycle_matrix.json"
    matrix = load_unique_json(matrix_path)
    entries = _list(matrix.get("plugins"), "plugin lifecycle matrix plugins")
    golden_pairs: set[tuple[str, str]] = set()
    release_lanes: dict[str, str] = {}
    for raw_entry in entries:
        entry = _dict(raw_entry, "plugin lifecycle matrix entry")
        plugin_key = _string(entry.get("plugin_key"), "plugin lifecycle matrix plugin_key")
        _require(plugin_key not in release_lanes, f"plugin lifecycle matrix has duplicate plugin key {plugin_key}")
        release_lanes[plugin_key] = _string(entry.get("release_lane"), f"plugin lifecycle matrix {plugin_key} release_lane")
        for variant_id in _strings(entry.get("variants"), f"plugin lifecycle matrix {plugin_key} variants"):
            golden_pairs.add((plugin_key, variant_id))

    from scripts import state_engine_plugin_matrix

    production_pairs = state_engine_plugin_matrix._validate_variant_configs(state_engine_plugin_matrix._variant_map())
    _require(golden_pairs == production_pairs, "PB-09 variant inventory drift between golden and production discriminators")
    validate_selector_manifest_data(
        manifest,
        catalog,
        pb09_variants=production_pairs,
        plugin_release_lanes=release_lanes,
        collector=_collect_pytest_node_ids,
        repository_root=root,
    )
