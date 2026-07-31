from __future__ import annotations

import inspect

from elspeth.contracts.blobs import BlobServiceProtocol as CoreBlobServiceProtocol
from elspeth.web.blobs.protocol import BlobServiceProtocol
from elspeth.web.blobs.service import BlobServiceImpl


def test_web_blob_fork_copy_requires_exact_composite_session_authority() -> None:
    signature = inspect.signature(BlobServiceProtocol.copy_blobs_for_fork)
    assert signature.parameters["write_authority"].annotation == "SessionForkAuthority"
    assert "write_fence" not in signature.parameters


def test_core_blob_protocol_has_no_scalar_fork_authority_fallback() -> None:
    assert "copy_blobs_for_fork" not in CoreBlobServiceProtocol.__dict__
    assert "cleanup_blobs_for_fork" not in CoreBlobServiceProtocol.__dict__


def test_internal_blob_delete_has_no_boolean_fork_cleanup_bypass() -> None:
    ordinary = inspect.signature(BlobServiceImpl._delete_blob_with_ledger)
    fork_cleanup = inspect.signature(BlobServiceImpl._delete_fork_blob_with_ledger)
    assert "authorized_fork_cleanup" not in ordinary.parameters
    assert "authorized_fork_cleanup" not in fork_cleanup.parameters
    assert ordinary.parameters["context"].annotation == "SessionOperationContext"
    assert fork_cleanup.parameters["authority"].annotation == "SessionForkAuthority"
