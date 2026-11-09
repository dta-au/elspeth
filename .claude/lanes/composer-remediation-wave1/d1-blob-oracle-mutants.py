"""Isolated omission controls; changes only this process's imported objects."""
from __future__ import annotations

import sys

import pytest

from elspeth.contracts.blobs import BlobReplacementPlan
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer.tools import blobs
from elspeth.web.coordination import quota_authority, repository

mode = sys.argv[1]
root = "/workspace/elspeth/.claude/worktrees/composer-remediation-wave1"
direct = "tests/unit/web/blobs/test_mutation_admission.py"
quota = "tests/unit/web/blobs/test_mutation_admission_quota.py"

if mode == "retention":
    repository._RepositoryBlobMutations._require_blob_deletion_retention_clear = lambda *args, **kwargs: None
    selection = [direct + "::test_admission_retains_every_relational_edge_and_admits_settled_edges[True-run_link-update_blob]"]
elif mode in {"identity", "container"}:
    quota_authority.RepositoryQuotaAuthority.admit_storage_bytes = lambda *args, **kwargs: None
    selection = [quota + f"::test_independent_admission_bounds_keep_truthful_durable_quota_evidence[{mode}]"]
elif mode == "session_reservation":
    from sqlalchemy import func, select
    from elspeth.web.sessions.models import blobs_table
    def ignore_reservations(connection, *, session_id):
        return connection.execute(select(func.coalesce(func.sum(blobs_table.c.size_bytes), 0)).where(blobs_table.c.session_id == session_id)).scalar_one()
    repository.session_storage_bytes_on_connection = ignore_reservations
    selection = [quota + "::test_output_reservation_excess_is_charged_once_by_every_admission_bound[session-partial]"]
elif mode in {"identity_reservation", "container_reservation"}:
    quota_authority.storage_bytes_used_on_connection = lambda *args, **kwargs: 3
    scope = mode.removesuffix("_reservation")
    selection = [quota + f"::test_output_reservation_excess_is_charged_once_by_every_admission_bound[{scope}-partial]"]
elif mode == "operation_kind":
    repository._BLOB_REPLACEMENT_OPERATION_KINDS |= {SessionOperationKind.EXECUTE}
    selection = [direct + "::test_admission_requires_exact_live_mutation_authority_before_reading_files[execute-update_blob]"]
elif mode == "corrupt_metadata":
    BlobReplacementPlan.require_current_metadata = lambda *args, **kwargs: None
    selection = [direct + "::test_admission_keeps_corrupt_interrupted_evidence_an_integrity_fault[replacement-intent-live_mismatch]"]
elif mode == "recovery":
    original = blobs.admit_blob_mutation
    def recover_instead(tool_name, arguments, state, context):
        from pathlib import Path
        from elspeth.web.blobs.replacement import BlobReplacementCoordinator
        BlobReplacementCoordinator(engine=context.session_engine, data_dir=Path(context.data_dir), session_operation_authority=context.session_operation_authority).reconcile(context=context.session_operation_context)
        return original(tool_name, arguments, state, context)
    blobs.admit_blob_mutation = recover_instead
    selection = [direct + "::test_admission_refuses_actual_interrupted_debt_without_recovery[delete_blob-replacement-intent]"]
else:
    raise ValueError(mode)

print(f"mode={mode}; repository={repository.__file__}; no checkout edits", flush=True)
raise SystemExit(pytest.main([*selection, "-n0", "-o", f"pythonpath={root}/src {root}/elspeth-lints/src"]))
