"""Closed service handoffs carrying registered publication projection evidence."""

from __future__ import annotations

from asyncio import CancelledError
from dataclasses import dataclass
from typing import Literal

from elspeth.web.required_work import (
    PublicationProjectionDisposition,
    PublicationProjectionUnusedMetadata,
    RequiredWorkCoordinator,
    RequiredWorkTicket,
)
from elspeth.web.sessions.pipeline_revocation import (
    ComposerRevocationExpected as ComposerRevocationExpected,
)
from elspeth.web.sessions.pipeline_revocation import (
    ComposerRevocationSQLResult as ComposerRevocationSQLResult,
)
from elspeth.web.sessions.pipeline_revocation import (
    PipelinePublicationSQLResult as PipelinePublicationSQLResult,
)
from elspeth.web.sessions.pipeline_revocation import (
    _ComposerRevocationRequired as _ComposerRevocationRequired,
)
from elspeth.web.sessions.pipeline_revocation import (
    decode_composer_revocation_result as decode_composer_revocation_result,
)
from elspeth.web.sessions.protocol import PipelineProposalSettlementResult, ProposalEventRecord


@dataclass(frozen=True, slots=True)
class ComposerPipelineBusinessReturned:
    result: PipelineProposalSettlementResult
    deferred_cancellations: tuple[CancelledError, ...]


@dataclass(frozen=True, slots=True)
class ComposerPipelineRevocationCompleted:
    event: ProposalEventRecord
    deferred_cancellations: tuple[CancelledError, ...]
    publication_projection_disposition: Literal[PublicationProjectionDisposition.ELIGIBILITY_SELECTED]
    publication_projection_unused: PublicationProjectionUnusedMetadata


@dataclass(frozen=True, slots=True)
class ComposerPipelineRaised:
    error: BaseException
    deferred_cancellations: tuple[CancelledError, ...]
    publication_projection_disposition: PublicationProjectionDisposition
    publication_projection_unused: PublicationProjectionUnusedMetadata


type ComposerPipelineFinishOnce = ComposerPipelineBusinessReturned | ComposerPipelineRevocationCompleted | ComposerPipelineRaised


@dataclass(frozen=True, slots=True)
class PipelineFinishOnceWork:
    coordinator: RequiredWorkCoordinator
    publication: RequiredWorkTicket
    publication_projection: RequiredWorkTicket
    revocation: RequiredWorkTicket
    revocation_projection: RequiredWorkTicket
