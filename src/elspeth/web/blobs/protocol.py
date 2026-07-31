"""Blob service protocol re-exports.

The load-bearing blob contracts live at L0 in ``elspeth.contracts.blobs``
so core resolver code can depend on them without importing the web layer.
This module preserves the historical web-layer import path.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable
from uuid import UUID

from elspeth.contracts.blobs import ALLOWED_MIME_TYPES as ALLOWED_MIME_TYPES
from elspeth.contracts.blobs import BLOB_CREATORS as BLOB_CREATORS
from elspeth.contracts.blobs import BLOB_RUN_LINK_DIRECTIONS as BLOB_RUN_LINK_DIRECTIONS
from elspeth.contracts.blobs import BLOB_STATUSES as BLOB_STATUSES
from elspeth.contracts.blobs import FINALIZE_BLOB_STATUSES as FINALIZE_BLOB_STATUSES
from elspeth.contracts.blobs import AllowedMimeType as AllowedMimeType
from elspeth.contracts.blobs import BlobActiveRunError as BlobActiveRunError
from elspeth.contracts.blobs import BlobContentMissingError as BlobContentMissingError
from elspeth.contracts.blobs import BlobCreator as BlobCreator
from elspeth.contracts.blobs import BlobError as BlobError
from elspeth.contracts.blobs import BlobFinalizationError as BlobFinalizationError
from elspeth.contracts.blobs import BlobFinalizationResult as BlobFinalizationResult
from elspeth.contracts.blobs import BlobForkCleanupError as BlobForkCleanupError
from elspeth.contracts.blobs import BlobForkCleanupResult as BlobForkCleanupResult
from elspeth.contracts.blobs import BlobForkFenceLostError as BlobForkFenceLostError
from elspeth.contracts.blobs import BlobForkPlanEntry as BlobForkPlanEntry
from elspeth.contracts.blobs import BlobGuidedOperationFenceLostError as BlobGuidedOperationFenceLostError
from elspeth.contracts.blobs import BlobGuidedOperationWriteFence as BlobGuidedOperationWriteFence
from elspeth.contracts.blobs import BlobInProgressForkError as BlobInProgressForkError
from elspeth.contracts.blobs import BlobIntegrityError as BlobIntegrityError
from elspeth.contracts.blobs import BlobNotFoundError as BlobNotFoundError
from elspeth.contracts.blobs import BlobPendingProposalError as BlobPendingProposalError
from elspeth.contracts.blobs import BlobQuotaExceededError as BlobQuotaExceededError
from elspeth.contracts.blobs import BlobRecord as BlobRecord
from elspeth.contracts.blobs import BlobRunLinkDirection as BlobRunLinkDirection
from elspeth.contracts.blobs import BlobRunLinkRecord as BlobRunLinkRecord
from elspeth.contracts.blobs import BlobServiceProtocol as _BlobServiceProtocol
from elspeth.contracts.blobs import BlobStateError as BlobStateError
from elspeth.contracts.blobs import BlobStatus as BlobStatus
from elspeth.contracts.blobs import FinalizeBlobStatus as FinalizeBlobStatus
from elspeth.contracts.blobs import InlineCustodyRequest as InlineCustodyRequest
from elspeth.contracts.blobs import fork_blob_id as fork_blob_id
from elspeth.web.sessions.protocol import SessionForkAuthority


@runtime_checkable
class BlobServiceProtocol(_BlobServiceProtocol, Protocol):
    """Web blob surface with exact dual-session fork authority."""

    async def copy_blobs_for_fork(
        self,
        source_session_id: UUID,
        target_session_id: UUID,
        plan: tuple[BlobForkPlanEntry, ...],
        write_authority: SessionForkAuthority,
        *,
        checkpoint: Callable[[], Awaitable[None]],
    ) -> dict[UUID, BlobRecord]:
        """Copy one frozen plan under parent, child, and guided authority."""
        ...

    async def cleanup_blobs_for_fork(
        self,
        authority: SessionForkAuthority,
    ) -> BlobForkCleanupResult:
        """Clean failed fork blobs under the exact retained composite."""
        ...
