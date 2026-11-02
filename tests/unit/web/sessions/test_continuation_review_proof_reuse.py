"""A verified Review source may be reused only in its original SQL transaction."""

from __future__ import annotations

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_semantic_actions import _require_same_review_transaction
from elspeth.web.sessions.engine import create_session_engine


def test_review_proof_reuse_requires_same_connection_transaction_snapshot_and_effect() -> None:
    engine = create_session_engine("sqlite:///:memory:")
    source = StoredContinuationSnapshot("{}", 3, "original-checkpoint")
    changed = StoredContinuationSnapshot("{}", 3, "substituted-checkpoint")
    with engine.connect() as conn:
        with conn.begin():
            transaction = conn.get_transaction()
            assert transaction is not None
            _require_same_review_transaction(conn, conn, transaction, source, source, "provider:1:0", "provider:1:0")
            with pytest.raises(AuditIntegrityError, match="original current transaction and source"):
                _require_same_review_transaction(conn, conn, transaction, source, changed, "provider:1:0", "provider:1:0")
            with pytest.raises(AuditIntegrityError, match="original current transaction and source"):
                _require_same_review_transaction(conn, conn, transaction, source, source, "provider:1:0", "provider:1:1")
        with conn.begin(), pytest.raises(AuditIntegrityError, match="original current transaction and source"):
            _require_same_review_transaction(conn, conn, transaction, source, source, "provider:1:0", "provider:1:0")
        with engine.connect() as other, other.begin(), pytest.raises(AuditIntegrityError, match="original current transaction and source"):
            _require_same_review_transaction(other, conn, transaction, source, source, "provider:1:0", "provider:1:0")
    engine.dispose()
