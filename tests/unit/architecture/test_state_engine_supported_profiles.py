from __future__ import annotations

import json
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CURRENT_CATALOG_PATH = REPOSITORY_ROOT / "docs/architecture/state_engine/proof-catalog/v3/catalog.json"
FROZEN_V2_CATALOG_PATH = REPOSITORY_ROOT / "docs/architecture/state_engine/proof-catalog/v2/catalog.json"
ADR_041_PATH = REPOSITORY_ROOT / "docs/architecture/adr/041-state-engine-supported-profiles.md"
ADR_030_PATH = REPOSITORY_ROOT / "docs/architecture/adr/030-multi-worker-deployment-shape.md"
ADR_INDEX_PATH = REPOSITORY_ROOT / "docs/architecture/adr/README.md"
AWS_README_PATH = REPOSITORY_ROOT / "deploy/aws-ecs/terraform/README.md"


def _normalized_text(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_supported_state_engine_profiles_are_pinned_across_catalog_and_docs() -> None:
    """The maintained-current catalog is v3; a silent fallback to v2 fails here."""

    assert ADR_041_PATH.is_file(), "ADR-041 must define the supported state-engine profiles"
    assert CURRENT_CATALOG_PATH.is_file(), "the maintained-current v3 proof catalog must pin the supported profiles"

    catalog = json.loads(CURRENT_CATALOG_PATH.read_text(encoding="utf-8"))
    assert catalog["catalog_id"] == "elspeth-state-engine-v3"
    assert catalog["schema_version"] == 2

    execution_profiles = catalog["execution_profiles"]
    state_store_profiles = execution_profiles["state_store"]
    state_store_ids = [profile["id"] for profile in state_store_profiles]
    assert len(state_store_ids) == 2
    assert len(state_store_ids) == len(set(state_store_ids))
    assert set(state_store_ids) == {"sqlite-wal", "postgresql-16"}
    assert all(profile["required"] is True for profile in state_store_profiles)
    state_stores = {profile["id"]: profile for profile in state_store_profiles}

    assert state_stores["sqlite-wal"]["contract_ref"] == ("docs/architecture/adr/030-multi-worker-deployment-shape.md")
    assert state_stores["postgresql-16"]["contract_ref"] == ("docs/architecture/adr/041-state-engine-supported-profiles.md")

    sqlite_deployments = state_stores["sqlite-wal"]["deployments"]
    postgresql_deployments = state_stores["postgresql-16"]["deployments"]
    expected_sqlite_deployments = {
        "single-process-leader",
        "same-host-leader-plus-claim-only-followers",
        "web-hosted-leader-plus-same-host-cli-followers",
    }
    expected_postgresql_deployments = {"aws-single-leader-landscape"}
    assert len(sqlite_deployments) == 3
    assert len(sqlite_deployments) == len(set(sqlite_deployments))
    assert set(sqlite_deployments) == expected_sqlite_deployments
    assert len(postgresql_deployments) == 1
    assert len(postgresql_deployments) == len(set(postgresql_deployments))
    assert set(postgresql_deployments) == expected_postgresql_deployments
    assert set(sqlite_deployments).isdisjoint(postgresql_deployments)

    deployments = execution_profiles["deployments"]
    expected_deployments = expected_sqlite_deployments | expected_postgresql_deployments
    assert len(deployments) == 4
    assert len(deployments) == len(set(deployments))
    assert set(deployments) == expected_deployments
    assert all("multi-host" not in deployment for deployment in deployments)

    adr_041 = _normalized_text(ADR_041_PATH)
    assert "PostgreSQL 16 single-leader" in adr_041
    assert "required first-class state-engine backend" in adr_041
    assert "SQLite WAL one-host leader/follower" in adr_041
    assert "PostgreSQL multi-replica scheduling remains unsupported" in adr_041
    for contract_term in (
        "DB-server time",
        "row locking",
        "isolation",
        "schema migration",
        "connection-loss behavior",
    ):
        assert contract_term in adr_041
    assert "cannot inherit single-leader evidence" in adr_041

    adr_030 = _normalized_text(ADR_030_PATH)
    assert "Amended by ADR-041" in adr_030
    assert "PostgreSQL 16 single-leader" in adr_030

    aws_readme = _normalized_text(AWS_README_PATH)
    assert "PostgreSQL 16 single-leader" in aws_readme
    assert "required first-class state-engine backend" in aws_readme
    assert "PostgreSQL multi-replica scheduling remains unsupported" in aws_readme

    adr_index = ADR_INDEX_PATH.read_text(encoding="utf-8")
    assert "[041](041-state-engine-supported-profiles.md)" in adr_index


def test_frozen_v2_catalog_remains_valid_historical_input() -> None:
    """The frozen v2 package stays byte-present with the exact supported profiles.

    v2 is immutable history: it can never again become the maintained-current
    catalog (the maintained-current assertion above names v3), but the frozen
    package must keep resolving so historical v2 assessments stay valid.
    """

    assert FROZEN_V2_CATALOG_PATH.is_file(), "the frozen v2 proof catalog must remain in the tree"
    frozen = json.loads(FROZEN_V2_CATALOG_PATH.read_text(encoding="utf-8"))
    current = json.loads(CURRENT_CATALOG_PATH.read_text(encoding="utf-8"))

    assert frozen["catalog_id"] == "elspeth-state-engine-v2"
    assert current["catalog_id"] == "elspeth-state-engine-v3"
    assert frozen["execution_profiles"] == current["execution_profiles"]
    assert {profile["id"] for profile in frozen["execution_profiles"]["state_store"]} == {
        "sqlite-wal",
        "postgresql-16",
    }
