"""PostgreSQL proof that a long mapping-form query name still starts its run.

``run_prompt_inputs.field_path`` was VARCHAR(256) while the query name it
embeds (``queries.<name>.template``) is uncapped; a name over 239 characters
made PostgreSQL refuse ``begin_run``. SQLite does not enforce VARCHAR widths,
so only this dialect can show it.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.fixtures.stores import MockPayloadStore
from tests.helpers.postgres_target import postgres_test_target

from elspeth.contracts.run_prompt_input import RunPromptInputEvidence
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import run_prompt_inputs_table

pytestmark = pytest.mark.testcontainer


@pytest.mark.timeout(120)
def test_postgres_begin_run_records_a_300_character_query_name() -> None:
    field_path = f"queries.{'q' * 300}.template"
    evidence = RunPromptInputEvidence("classify", field_path, str(uuid4()), "a" * 64, "b" * 64, None)
    with postgres_test_target(driver="psycopg") as postgres_url, LandscapeDB.from_url(postgres_url) as db:
        factory = RecorderFactory(db, payload_store=MockPayloadStore())
        run = factory.run_lifecycle.begin_run(
            config={},
            canonical_version="v1",
            openrouter_catalog_sha256="0" * 64,
            openrouter_catalog_source="bundled",
            run_prompt_inputs=(evidence,),
        )
        with db.read_only_connection() as conn:
            stored = conn.execute(
                select(run_prompt_inputs_table.c.field_path).where(run_prompt_inputs_table.c.run_id == run.run_id)
            ).scalar_one()
    assert stored == field_path
    assert len(stored) == 317  # well past the former VARCHAR(256)
