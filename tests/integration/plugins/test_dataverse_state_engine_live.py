"""Protected live-Dataverse PB-09 lifecycle proofs for source:dataverse and sink:dataverse.

One node per production auth-discriminator variant (``service_principal``,
``managed_identity``), for each of the two subjects in
``tests/golden/state_engine/plugin_lifecycle_matrix.json``.  Every node crosses
the FULL production boundary (settings YAML -> ``instantiate_plugins_from_config``
-> ``ExecutionGraph`` -> runtime preflight -> public ``Orchestrator.run`` with a
real Landscape database) and then externally observes the live Dataverse
environment.  Direct ``DataverseClient`` calls appear only as support code
(entity-set resolution, row seeding, read-back, cleanup).

Operator provisioning contract for the test entity
(``ELSPETH_TEST_DATAVERSE_ENTITY`` carries its logical name):

- writable string columns ``emailaddress1`` and ``firstname`` (a standard
  ``contact``-shaped entity satisfies this);
- an alternate key defined over ``emailaddress1``, so alternate-key PATCH
  upserts and key-addressed DELETE cleanup resolve;
- metadata read access (``EntityDefinitions``) for entity-set resolution.

Created rows carry ``ELSPETH_TEST_DATAVERSE_ROW_PREFIX`` plus a UUID in their
``emailaddress1`` key and are deleted in ``finally``.
"""

from __future__ import annotations

import re
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from elspeth.contracts import RunStatus
from elspeth.plugins.infrastructure.clients.dataverse import (
    DataverseAuthConfig,
    DataverseClient,
)
from tests.integration.plugins._azure_live import (
    ensure_fingerprint_key,
    jsonl_source_options,
    local_jsonl_sink_options,
    read_jsonl,
    run_orchestrated_pipeline,
    write_jsonl,
)
from tests.integration.plugins._dataverse_chroma_live import (
    DATAVERSE_AUTH_VARIANTS,
    DATAVERSE_ENTITY_ENV,
    DATAVERSE_ROW_PREFIX_ENV,
    DATAVERSE_URL_ENV,
    dataverse_auth_options,
    require_resource,
)

pytestmark = [pytest.mark.integration, pytest.mark.live_dataverse, pytest.mark.live_provider]

_API_VERSION = "v9.2"
_SEED_FIRSTNAME = "state-engine-live"


@dataclass(frozen=True, slots=True)
class DataverseTarget:
    """Operator-approved Dataverse environment and disposable-row identity."""

    environment_url: str
    entity: str
    row_prefix: str

    def unique_email(self) -> str:
        """Uniquely-prefixed alternate-key value for one disposable row."""
        return f"{self.row_prefix}-{uuid.uuid4().hex}@state-engine-live.invalid"


def require_dataverse_target() -> DataverseTarget:
    """Resolve the live Dataverse target from the closed vocabulary (or skip)."""
    row_prefix = require_resource(DATAVERSE_ROW_PREFIX_ENV)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", row_prefix):
        raise AssertionError(f"{DATAVERSE_ROW_PREFIX_ENV} must be a URL- and email-local-part-safe token, got {row_prefix!r}")
    return DataverseTarget(
        environment_url=require_resource(DATAVERSE_URL_ENV).rstrip("/"),
        entity=require_resource(DATAVERSE_ENTITY_ENV),
        row_prefix=row_prefix,
    )


@pytest.fixture(params=DATAVERSE_AUTH_VARIANTS)
def dataverse_auth(request: pytest.FixtureRequest) -> tuple[str, DataverseTarget, dict[str, object]]:
    """Resolve one auth variant's live credentials, skipping when unconfigured."""
    variant = request.param
    target = require_dataverse_target()
    return variant, target, dataverse_auth_options(variant)


def support_client(target: DataverseTarget, auth: dict[str, object]) -> DataverseClient:
    """Production Dataverse client from the variant's own credentials (support only)."""
    credential = DataverseAuthConfig.model_validate(auth).create_credential()
    return DataverseClient(environment_url=target.environment_url, credential=credential)


def resolve_entity_set(client: DataverseClient, target: DataverseTarget) -> str:
    """Resolve the EntitySetName for the lane's entity logical name."""
    metadata_url = (
        f"{target.environment_url}/api/data/{_API_VERSION}/"
        f"EntityDefinitions(LogicalName='{target.entity}')?$select=LogicalName,EntitySetName"
    )
    (row,) = client.get_page(metadata_url).rows
    entity_set = row["EntitySetName"]
    assert isinstance(entity_set, str) and entity_set
    return entity_set


def row_key_url(target: DataverseTarget, entity_set: str, email: str) -> str:
    return f"{target.environment_url}/api/data/{_API_VERSION}/{entity_set}(emailaddress1='{email}')"


def delete_row_if_present(client: DataverseClient, target: DataverseTarget, entity_set: str, email: str) -> None:
    """Key-addressed DELETE cleanup; an absent row (404) is already clean."""
    response = httpx.delete(
        row_key_url(target, entity_set, email),
        headers=client.get_auth_headers(),
        timeout=30.0,
    )
    assert response.status_code in (204, 404), f"Dataverse cleanup DELETE returned HTTP {response.status_code}"


def fetch_rows_by_email(client: DataverseClient, target: DataverseTarget, entity_set: str, email: str) -> list[dict[str, object]]:
    """Independent read-back of rows keyed by the disposable email value."""
    filter_expression = urllib.parse.quote(f"emailaddress1 eq '{email}'", safe="'()")
    query_url = f"{target.environment_url}/api/data/{_API_VERSION}/{entity_set}?$select=emailaddress1,firstname&$filter={filter_expression}"
    return [dict(row) for row in client.get_page(query_url).rows]


def test_dataverse_source_live_lifecycle_loads_seeded_row(
    dataverse_auth: tuple[str, DataverseTarget, dict[str, object]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    variant, target, auth = dataverse_auth
    ensure_fingerprint_key(monkeypatch)
    email = target.unique_email()
    output_path = tmp_path / "output.jsonl"

    pipeline: dict[str, object] = {
        "sources": {
            "subject_source": {
                "plugin": "dataverse",
                "on_success": "output",
                "options": {
                    "environment_url": target.environment_url,
                    "auth": dict(auth),
                    "entity": target.entity,
                    "select": ["emailaddress1", "firstname"],
                    "filter": f"emailaddress1 eq '{email}'",
                    "schema": {"mode": "observed"},
                    "on_validation_failure": "discard",
                },
            }
        },
        "sinks": {
            "output": {
                "plugin": "json",
                "on_write_failure": "discard",
                "options": local_jsonl_sink_options(output_path),
            }
        },
    }

    client = support_client(target, auth)
    entity_set: str | None = None
    try:
        entity_set = resolve_entity_set(client, target)
        client.upsert(row_key_url(target, entity_set, email), {"firstname": _SEED_FIRSTNAME})

        evidence = run_orchestrated_pipeline(pipeline, tmp_path)
        assert evidence.status is RunStatus.COMPLETED, f"variant {variant}: {evidence}"
        evidence.assert_completed_lifecycle()

        loaded = read_jsonl(output_path)
        assert [row["emailaddress1"] for row in loaded] == [email]
        assert loaded[0]["firstname"] == _SEED_FIRSTNAME
    finally:
        if entity_set is not None:
            delete_row_if_present(client, target, entity_set, email)
        client.close()


def test_dataverse_sink_live_lifecycle_upserts_row(
    dataverse_auth: tuple[str, DataverseTarget, dict[str, object]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    variant, target, auth = dataverse_auth
    ensure_fingerprint_key(monkeypatch)
    email = target.unique_email()
    input_path = tmp_path / "rows.jsonl"
    write_jsonl(input_path, [{"email": email, "first_name": _SEED_FIRSTNAME}])

    client = support_client(target, auth)
    entity_set: str | None = None
    try:
        entity_set = resolve_entity_set(client, target)

        pipeline: dict[str, object] = {
            "sources": {
                "input": {
                    "plugin": "json",
                    "on_success": "subject_sink",
                    "options": jsonl_source_options(input_path),
                }
            },
            "sinks": {
                "subject_sink": {
                    "plugin": "dataverse",
                    "on_write_failure": "discard",
                    "options": {
                        "environment_url": target.environment_url,
                        "auth": dict(auth),
                        "entity": entity_set,
                        "mode": "upsert",
                        "field_mapping": {"email": "emailaddress1", "first_name": "firstname"},
                        "alternate_key": "emailaddress1",
                        "schema": {"mode": "observed"},
                    },
                }
            },
        }

        evidence = run_orchestrated_pipeline(pipeline, tmp_path)
        assert evidence.status is RunStatus.COMPLETED, f"variant {variant}: {evidence}"
        evidence.assert_completed_lifecycle()

        # External observation: the finalized upsert must be visible remotely.
        (remote_row,) = fetch_rows_by_email(client, target, entity_set, email)
        assert remote_row["emailaddress1"] == email
        assert remote_row["firstname"] == _SEED_FIRSTNAME
    finally:
        if entity_set is not None:
            delete_row_if_present(client, target, entity_set, email)
        client.close()
