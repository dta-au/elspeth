"""ChromaDB provider for RAG retrieval.

Supports three modes:
- ephemeral: In-memory, no persistence. Ideal for testing and development.
- persistent: Local disk storage. Survives process restarts.
- client: Remote Chroma server via HTTP/gRPC.

Score normalization:
- Chroma returns distances, not similarities. The normalization depends on
  the collection's distance function:
  - cosine: distance in [0, 2], similarity = 1 - (distance / 2)
  - l2: distance in [0, inf), similarity = 1 / (1 + distance)
  - ip (inner product): distance = 1 - similarity for normalized vectors,
    similarity = 1 - distance (clamped to [0, 1])
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Mapping
from itertools import pairwise
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Self

import chromadb
import chromadb.api
import httpx
from pydantic import BaseModel, field_validator, model_validator

from elspeth.contracts.call_data import RawCallPayload
from elspeth.contracts.call_mode import CallModeSession
from elspeth.contracts.coordination import CoordinationToken, WorkerMembershipToken
from elspeth.contracts.enums import CallStatus, CallType, RunMode
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.probes import CollectionReadinessResult
from elspeth.contracts.scheduler import TokenWorkItem
from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.plugins.infrastructure.clients.retrieval.base import RetrievalError
from elspeth.plugins.infrastructure.clients.retrieval.connection import (
    ChromaConnectionConfig,
    ChromaSearchMode,
    _validated_chroma_http_client_args,
)
from elspeth.plugins.infrastructure.clients.retrieval.types import RetrievalChunk

if TYPE_CHECKING:
    from elspeth.core.landscape.execution_repository import ExecutionRepository


class ChromaSearchProviderConfig(BaseModel):
    """Configuration for ChromaDB provider."""

    model_config = {"extra": "forbid", "frozen": True}

    collection: str
    mode: ChromaSearchMode = "ephemeral"

    persist_directory: str | None = None

    host: str | None = None
    port: int = 8000
    ssl: bool = True

    distance_function: Literal["cosine", "l2", "ip"] = "cosine"

    @field_validator("collection")
    @classmethod
    def validate_collection_name(cls, v: str) -> str:
        if len(v) < 3:
            raise ValueError(f"collection name must be at least 3 characters, got {len(v)}")
        if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*[a-zA-Z0-9]$", v):
            raise ValueError(
                f"collection must contain only alphanumeric characters, hyphens, and underscores "
                f"(and start/end with alphanumeric), got {v!r}."
            )
        return v

    @model_validator(mode="after")
    def validate_mode_requirements(self) -> Self:
        if self.mode in ("persistent", "client"):
            # Delegate cross-field validation to ChromaConnectionConfig.
            # Construction triggers its model_validator; we discard the
            # instance — the provider config keeps its own flat fields.
            ChromaConnectionConfig(
                collection=self.collection,
                mode=self.mode,
                persist_directory=self.persist_directory,
                host=self.host,
                port=self.port,
                ssl=self.ssl,
                distance_function=self.distance_function,
            )
        return self

    def to_connection_config(self) -> ChromaConnectionConfig:
        """Build a ChromaConnectionConfig from this provider config.

        Only valid when mode is 'persistent' or 'client'. Raises
        ValueError for ephemeral mode (no shared connection to configure).
        """
        if self.mode == "ephemeral":
            raise ValueError(
                "Cannot create ChromaConnectionConfig from ephemeral mode — ephemeral clients have no shared connection parameters"
            )
        return ChromaConnectionConfig(
            collection=self.collection,
            mode=self.mode,
            persist_directory=self.persist_directory,
            host=self.host,
            port=self.port,
            ssl=self.ssl,
            distance_function=self.distance_function,
        )


@trust_boundary(
    tier=3,
    source="chromadb Collection.metadata as returned by the Chroma SDK / server for an existing collection",
    source_param="collection_metadata",
    suppresses=("R5",),
    invariant=(
        "raises RetrievalError(retryable=False) when the metadata is not a Mapping, carries no 'hnsw:space', "
        "or its 'hnsw:space' is not a str; never substitutes a default distance function"
    ),
    test_ref="tests/unit/plugins/infrastructure/clients/retrieval/test_chroma.py::test_collection_distance_space_rejects_non_mapping_metadata",
    test_fingerprint="aea5f064c296f13717d3bcd56351bf114f2e951151ea7492e77c5250122f6374",
)
def _collection_distance_space(collection_metadata: Any, collection_name: str) -> str:
    """Parse the distance function an existing Chroma collection was built with.

    Score normalization is only meaningful against the collection's real
    ``hnsw:space``; a missing or malformed value must fail startup rather than
    fall back to a guess. ``collection_name`` is only used for the message.
    """
    if not isinstance(collection_metadata, Mapping):
        raise RetrievalError(
            f"Chroma collection {collection_name!r} returned malformed metadata. "
            "Score normalization requires an exact metadata mapping containing 'hnsw:space'.",
            retryable=False,
        )
    if "hnsw:space" not in collection_metadata:
        raise RetrievalError(
            f"Chroma collection {collection_name!r} has no 'hnsw:space' "
            f"in metadata. Score normalization cannot proceed without a "
            f"known distance function.",
            retryable=False,
        )
    actual_space = collection_metadata["hnsw:space"]
    if not isinstance(actual_space, str):
        raise RetrievalError(
            f"Chroma collection {collection_name!r} has a non-string 'hnsw:space' "
            f"({type(actual_space).__name__}) in metadata. Score normalization requires "
            f"a named distance function.",
            retryable=False,
        )
    return actual_space


class ChromaSearchProvider:
    """ChromaDB implementation of RetrievalProvider.

    Uses the chromadb Python SDK directly. No AuditedHTTPClient.
    Score normalization converts Chroma distances to [0.0, 1.0] similarity scores.
    """

    def __init__(
        self,
        config: ChromaSearchProviderConfig,
        *,
        execution: ExecutionRepository,
        run_id: str,
        call_mode_session: CallModeSession | None = None,
    ) -> None:
        self._config = config
        self._distance_function = config.distance_function
        self._execution = execution
        self._run_id = run_id
        self._call_mode_session = call_mode_session
        self._operation_id: str | None = None
        self._coordination_token: CoordinationToken | None = None
        self.last_skipped_count: int = 0
        self.last_skipped_reasons: list[dict[str, Any]] = []

        self._client: chromadb.api.ClientAPI | None = None
        self._collection: Any = None
        if call_mode_session is not None and call_mode_session.mode in (RunMode.REPLAY, RunMode.VERIFY):
            # Chroma's constructors and get_collection may contact a server or
            # touch a persistent index. Admit the source call before either.
            return

        self._initialize_client()

    def _initialize_client(self) -> None:
        config = self._config

        client: chromadb.api.ClientAPI
        if config.mode == "ephemeral":
            client = chromadb.Client()
        elif config.mode == "persistent":
            # persist_directory is guaranteed non-None by validate_mode_requirements
            assert config.persist_directory is not None
            client = chromadb.PersistentClient(path=config.persist_directory)
        else:
            # host is guaranteed non-None by validate_mode_requirements
            assert config.host is not None
            client = chromadb.HttpClient(
                **_validated_chroma_http_client_args(
                    config.host,
                    config.port,
                    ssl=config.ssl,
                )
            )
        self._client = client

        # Retrieval providers must NOT create collections — that's a sink/indexing
        # concern. Using get_collection() ensures a typo in the collection name
        # fails fast at startup instead of silently creating an empty collection.
        try:
            self._collection = client.get_collection(
                name=config.collection,
            )
        except (chromadb.errors.ChromaError, ConnectionError, OSError) as exc:
            raise RetrievalError(
                f"Chroma collection {config.collection!r} does not exist or is "
                f"unreachable. Retrieval requires a pre-populated collection — "
                f"check the collection name and ensure the corpus has been indexed. "
                f"Error: {exc}",
                retryable=False,
            ) from exc

        # Validate distance function matches what the collection was created with.
        actual_space = _collection_distance_space(self._collection.metadata, config.collection)
        if actual_space != config.distance_function:
            raise RetrievalError(
                f"Chroma collection {config.collection!r} exists with "
                f"distance_function={actual_space!r}, but config specifies "
                f"{config.distance_function!r}. Score normalization would use "
                f"the wrong formula. Either change the config to match the "
                f"existing collection, or use a different collection name.",
                retryable=False,
            )

    def search(
        self,
        query: str,
        top_k: int,
        min_score: float,
        *,
        state_id: str,
        token_id: str | None,
        member_token: WorkerMembershipToken,
        work_item: TokenWorkItem,
    ) -> list[RetrievalChunk]:
        request_payload = RawCallPayload({"query": query, "top_k": top_k, "collection": self._config.collection})
        request_data = request_payload.to_dict()
        if self._call_mode_session is not None and self._call_mode_session.mode is RunMode.REPLAY:
            return self._replay_search(
                request_payload,
                state_id=state_id,
                member_token=member_token,
                work_item=work_item,
            )
        call_index = self._execution.allocate_call_index(state_id, member_token=member_token, work_item=work_item)
        if self._call_mode_session is not None and self._call_mode_session.mode is RunMode.VERIFY:
            self._call_mode_session.admit_verify_call(
                call_type=CallType.VECTOR,
                request_data=request_data,
                current_state_id=state_id,
                current_operation_id=None,
                current_call_index=call_index,
            )
        if self._collection is None:
            self._initialize_client()
        count_start = time.monotonic()
        count_request = request_payload
        try:
            collection_count = self._collection.count()
        except (
            chromadb.errors.InvalidDimensionException,
            chromadb.errors.InvalidArgumentError,
            chromadb.errors.NotFoundError,
            chromadb.errors.AuthorizationError,
        ) as exc:
            self._record_error(
                state_id,
                count_start,
                count_request,
                exc,
                call_index=call_index,
                retryable=False,
                member_token=member_token,
                work_item=work_item,
            )
            raise RetrievalError(
                f"Chroma count failed (permanent): {exc}",
                retryable=False,
            ) from exc
        except chromadb.errors.ChromaError as exc:
            self._record_error(
                state_id,
                count_start,
                count_request,
                exc,
                call_index=call_index,
                retryable=True,
                member_token=member_token,
                work_item=work_item,
            )
            raise RetrievalError(f"Chroma count failed: {exc}", retryable=True) from exc
        except (ConnectionError, TimeoutError, OSError) as exc:
            # OS-level failures that bypass the ChromaDB SDK's own error wrapping
            self._record_error(
                state_id,
                count_start,
                count_request,
                exc,
                call_index=call_index,
                retryable=True,
                member_token=member_token,
                work_item=work_item,
            )
            raise RetrievalError(f"Chroma connection failed during count: {exc}", retryable=True) from exc
        if type(collection_count) is not int or collection_count < 0:
            error = RetrievalError(
                f"Chroma collection count returned malformed evidence: expected a non-negative exact int, "
                f"got {collection_count!r} ({type(collection_count).__name__}).",
                retryable=False,
            )
            self._record_error(
                state_id,
                count_start,
                count_request,
                error,
                call_index=call_index,
                retryable=False,
                member_token=member_token,
                work_item=work_item,
            )
            raise error
        if collection_count == 0:
            # The corpus is empty: no query() is issued and zero chunks are
            # returned. This is still an auditable retrieval decision — the
            # external count() call ran and its outcome shaped the pipeline
            # result. Recording it (rather than exiting silently) lets an
            # auditor distinguish "retrieval ran against an empty corpus" from
            # "retrieval never ran"; collection_count=0 further distinguishes
            # it from a populated query that simply matched nothing.
            empty_elapsed_ms = (time.monotonic() - count_start) * 1000
            response_payload = RawCallPayload(
                {
                    "result_count": 0,
                    "skipped_count": 0,
                    "top_score": None,
                    "collection_count": 0,
                    "chunks": [],
                    "skipped_items": [],
                }
            )
            recorded = self._execution.record_call(
                member_token=member_token,
                work_item=work_item,
                state_id=state_id,
                call_index=call_index,
                call_type=CallType.VECTOR,
                status=CallStatus.SUCCESS,
                request_data=count_request,
                response_data=response_payload,
                latency_ms=round(empty_elapsed_ms),
            )
            if self._call_mode_session is not None and self._call_mode_session.mode is RunMode.VERIFY:
                self._call_mode_session.verify_call(
                    call_type=CallType.VECTOR,
                    request_data=request_data,
                    current_state_id=state_id,
                    current_operation_id=None,
                    current_call_index=call_index,
                    current_call_id=recorded.call_id,
                    live_status=CallStatus.SUCCESS,
                    live_response_data=response_payload.to_dict(),
                    live_error_data=None,
                )
            self.last_skipped_count = 0
            self.last_skipped_reasons = []
            return []
        effective_top_k = min(top_k, collection_count)

        start_time = time.monotonic()
        request_payload = count_request
        try:
            results = self._collection.query(
                query_texts=[query],
                n_results=effective_top_k,
                include=["documents", "distances", "metadatas"],
            )
        except (
            chromadb.errors.InvalidDimensionException,
            chromadb.errors.InvalidArgumentError,
            chromadb.errors.NotFoundError,
            chromadb.errors.AuthorizationError,
        ) as exc:
            self._record_error(
                state_id,
                start_time,
                request_payload,
                exc,
                call_index=call_index,
                retryable=False,
                member_token=member_token,
                work_item=work_item,
            )
            raise RetrievalError(
                f"Chroma query failed (permanent): {exc}",
                retryable=False,
            ) from exc
        except chromadb.errors.ChromaError as exc:
            self._record_error(
                state_id,
                start_time,
                request_payload,
                exc,
                call_index=call_index,
                retryable=True,
                member_token=member_token,
                work_item=work_item,
            )
            raise RetrievalError(f"Chroma query failed: {exc}", retryable=True) from exc
        except (ConnectionError, TimeoutError, OSError) as exc:
            # OS-level failures that bypass the ChromaDB SDK's own error wrapping
            self._record_error(
                state_id,
                start_time,
                request_payload,
                exc,
                call_index=call_index,
                retryable=True,
                member_token=member_token,
                work_item=work_item,
            )
            raise RetrievalError(f"Chroma connection failed: {exc}", retryable=True) from exc
        elapsed_ms = (time.monotonic() - start_time) * 1000

        # Post-query processing: parse response, validate distances, build chunks.
        # The external call already happened — any failure here must still produce
        # an audit record. Without this try/except, response parsing errors,
        # distance validation crashes, and normalization failures would leave
        # no trace in the Landscape (fix: elspeth-9454d584d2).
        try:
            chunks, skipped_items = self._parse_and_build_chunks(results, min_score)
        except RetrievalError as exc:
            self._record_error(
                state_id,
                start_time,
                request_payload,
                exc,
                call_index=call_index,
                retryable=exc.retryable,
                member_token=member_token,
                work_item=work_item,
            )
            raise

        chunks.sort(key=lambda c: c.score, reverse=True)
        self.last_skipped_count = len(skipped_items)
        self.last_skipped_reasons = skipped_items

        response_payload = RawCallPayload(
            {
                "result_count": len(chunks),
                "skipped_count": len(skipped_items),
                "top_score": chunks[0].score if chunks else None,
                "collection_count": collection_count,
                "chunks": [
                    {"content": chunk.content, "score": chunk.score, "source_id": chunk.source_id, "metadata": deep_thaw(chunk.metadata)}
                    for chunk in chunks
                ],
                "skipped_items": skipped_items,
            }
        )
        recorded = self._execution.record_call(
            member_token=member_token,
            work_item=work_item,
            state_id=state_id,
            call_index=call_index,
            call_type=CallType.VECTOR,
            status=CallStatus.SUCCESS,
            request_data=request_payload,
            response_data=response_payload,
            latency_ms=round(elapsed_ms),
        )
        if self._call_mode_session is not None and self._call_mode_session.mode is RunMode.VERIFY:
            self._call_mode_session.verify_call(
                call_type=CallType.VECTOR,
                request_data=request_data,
                current_state_id=state_id,
                current_operation_id=None,
                current_call_index=call_index,
                current_call_id=recorded.call_id,
                live_status=CallStatus.SUCCESS,
                live_response_data=response_payload.to_dict(),
                live_error_data=None,
            )

        return chunks

    def _replay_search(
        self,
        request_payload: RawCallPayload,
        *,
        state_id: str,
        member_token: WorkerMembershipToken,
        work_item: TokenWorkItem,
    ) -> list[RetrievalChunk]:
        session = self._call_mode_session
        if session is None or session.mode is not RunMode.REPLAY:
            raise RuntimeError("Chroma replay requires a replay call session")
        call_index = self._execution.allocate_call_index(state_id, member_token=member_token, work_item=work_item)
        evidence = session.replay_call(
            call_type=CallType.VECTOR,
            request_data=request_payload.to_dict(),
            current_state_id=state_id,
            current_operation_id=None,
            current_call_index=call_index,
        )
        response = evidence.response_data
        if evidence.status is not CallStatus.SUCCESS or response is None:
            raise RetrievalError("Chroma replay source call did not succeed or has no retained response", retryable=False)
        try:
            raw_chunks = response["chunks"]
            raw_skips = response["skipped_items"]
            count = response["collection_count"]
        except KeyError as exc:
            raise RetrievalError("Chroma replay source call lacks complete chunk, skip, or count evidence", retryable=False) from exc
        # ReplayCallEvidence deep-freezes owned audit JSON: arrays become tuples
        # and objects become mapping proxies. Older incomplete shapes fail closed.
        if type(raw_chunks) is not tuple or type(raw_skips) is not tuple or type(count) is not int or count < 0:
            raise RetrievalError("Chroma replay source call lacks complete chunk, skip, or count evidence", retryable=False)
        chunks: list[RetrievalChunk] = []
        for raw_chunk in raw_chunks:
            if type(raw_chunk) is not MappingProxyType:
                raise RetrievalError("Chroma replay source call has malformed chunk evidence", retryable=False)
            try:
                content = raw_chunk["content"]
                score = raw_chunk["score"]
                source_id = raw_chunk["source_id"]
                metadata = raw_chunk["metadata"]
            except KeyError as exc:
                raise RetrievalError("Chroma replay source call has incomplete chunk fields", retryable=False) from exc
            if (
                type(content) is not str
                or type(score) not in (int, float)
                or type(source_id) is not str
                or type(metadata) is not MappingProxyType
            ):
                raise RetrievalError("Chroma replay source call has incomplete chunk fields", retryable=False)
            chunks.append(RetrievalChunk(content=content, score=float(score), source_id=source_id, metadata=deep_thaw(metadata)))
        skips = deep_thaw(raw_skips)
        if type(skips) is not list or any(type(item) is not dict for item in skips):
            raise RetrievalError("Chroma replay source call has malformed skip evidence", retryable=False)
        try:
            result_count = response["result_count"]
            skipped_count = response["skipped_count"]
            top_score = response["top_score"]
        except KeyError as exc:
            raise RetrievalError("Chroma replay source call lacks complete summary evidence", retryable=False) from exc
        if (
            type(result_count) is not int
            or result_count != len(chunks)
            or type(skipped_count) is not int
            or skipped_count != len(skips)
            or count < len(chunks)
            or (top_score is not None and type(top_score) not in (int, float))
            or (float(top_score) if top_score is not None else None) != (chunks[0].score if chunks else None)
            or any(left.score < right.score for left, right in pairwise(chunks))
        ):
            raise RetrievalError("Chroma replay source call summary disagrees with retained chunks", retryable=False)
        self.last_skipped_count = len(skips)
        self.last_skipped_reasons = skips
        self._execution.record_call(
            member_token=member_token,
            work_item=work_item,
            state_id=state_id,
            call_index=call_index,
            call_type=CallType.VECTOR,
            status=CallStatus.SUCCESS,
            request_data=request_payload,
            response_data=RawCallPayload(deep_thaw(response)),
            latency_ms=evidence.latency_ms,
            source_call_id=evidence.source_call_id,
        )
        return chunks

    def _parse_and_build_chunks(
        self,
        results: Any,
        min_score: float,
    ) -> tuple[list[RetrievalChunk], list[dict[str, Any]]]:
        """Parse ChromaDB query results and build RetrievalChunk list.

        Tier 3 boundary: the SDK response structure is external data.
        All access is guarded against malformed/unexpected responses.

        Returns:
            Tuple of (chunks, skipped_items)

        Raises:
            RetrievalError: On malformed response structure, corrupt distances,
                or non-finite distance values.
        """
        if not isinstance(results, Mapping):
            raise RetrievalError(
                f"Chroma query returned unexpected result structure: expected a mapping, got {type(results).__name__}",
                retryable=False,
            )
        required_fields = ("documents", "distances", "metadatas", "ids")
        if any(field not in results for field in required_fields):
            raise RetrievalError(
                "Chroma query returned unexpected result structure: one or more required arrays are absent",
                retryable=False,
            )
        batches: dict[str, list[Any]] = {}
        for field in required_fields:
            raw_batches = results[field]
            if type(raw_batches) is not list or len(raw_batches) != 1 or type(raw_batches[0]) is not list:
                raise RetrievalError(
                    f"Chroma query returned unexpected result structure for {field}: expected exactly one list batch",
                    retryable=False,
                )
            batches[field] = raw_batches[0]
        documents = batches["documents"]
        distances = batches["distances"]
        metadatas = batches["metadatas"]
        ids = batches["ids"]
        if not (len(documents) == len(distances) == len(metadatas) == len(ids)):
            raise RetrievalError(
                f"Chroma query for collection {self._config.collection!r} returned "
                f"mismatched result array lengths (documents={len(documents)}, "
                f"distances={len(distances)}, metadatas={len(metadatas)}, ids={len(ids)}). "
                f"This indicates a malformed SDK response or index corruption.",
                retryable=False,
            )
        if any(type(doc_id) is not str or not doc_id for doc_id in ids):
            raise RetrievalError(
                "Chroma query returned an invalid document ID; every ID must be a non-empty exact string",
                retryable=False,
            )
        if any(metadata is not None and type(metadata) is not dict for metadata in metadatas):
            raise RetrievalError(
                "Chroma query returned invalid metadata; every metadata item must be an exact mapping or None",
                retryable=False,
            )

        chunks: list[RetrievalChunk] = []
        skipped_items: list[dict[str, Any]] = []
        for doc, distance, metadata, doc_id in zip(documents, distances, metadatas, ids, strict=True):
            if not isinstance(doc, str):  # Tier 3: SDK may return non-str from corrupt index
                skipped_items.append({"reason": "invalid_content_type", "id": doc_id, "type": type(doc).__name__})
                continue

            # ChromaDB is our infrastructure, not an external API — corrupt
            # distances indicate index corruption or SDK bug.  Crash rather
            # than silently skipping: a run that completes with missing
            # retrieval chunks is worse than a crash (silent wrong result).
            if isinstance(distance, bool) or not isinstance(distance, (int, float)):
                raise RetrievalError(
                    f"ChromaDB collection {self._config.collection!r} returned "
                    f"non-numeric distance {distance!r} (type={type(distance).__name__}) "
                    f"for document {doc_id!r}. This indicates index corruption or a "
                    f"ChromaDB SDK bug — the collection may need to be rebuilt.",
                    retryable=False,
                )

            score = self._normalize_distance(distance)
            if score < min_score:
                continue

            chunk_metadata: dict[str, Any] = {}
            if metadata is not None:
                chunk_metadata = dict(metadata)
            try:
                chunks.append(
                    RetrievalChunk(
                        content=doc,
                        score=score,
                        source_id=doc_id,
                        metadata=chunk_metadata,
                    )
                )
            except ValueError as exc:
                raise RetrievalError(
                    f"Chroma provider produced invalid chunk data for document {doc_id!r}: {exc}",
                    retryable=False,
                ) from exc

        return chunks, skipped_items

    def _normalize_distance(self, distance: float) -> float:
        if not math.isfinite(distance):
            raise RetrievalError(
                f"Chroma returned non-finite distance {distance!r} — possible index corruption",
                retryable=False,
            )
        if self._distance_function == "cosine":
            return max(0.0, min(1.0, 1.0 - (distance / 2.0)))
        if self._distance_function == "l2":
            if distance < 0:
                raise RetrievalError(
                    f"Chroma returned negative L2 distance {distance!r} — possible index corruption",
                    retryable=False,
                )
            return 1.0 / (1.0 + distance)
        return max(0.0, min(1.0, 1.0 - distance))

    def _record_error(
        self,
        state_id: str,
        start_time: float,
        request_data: RawCallPayload,
        exc: Exception,
        *,
        call_index: int,
        retryable: bool,
        member_token: WorkerMembershipToken,
        work_item: TokenWorkItem,
    ) -> None:
        """Record a failed search call in the audit trail.

        Called from except blocks before re-raising. Uses best-effort
        recording — if the audit recording itself fails, the original search
        error takes priority (we don't mask it with an AuditIntegrityError
        here because the caller is about to raise a RetrievalError).
        """
        elapsed_ms = (time.monotonic() - start_time) * 1000
        error_data = {
            "error_type": type(exc).__name__,
            "message": str(exc),
            "retryable": retryable,
        }
        recorded = self._execution.record_call(
            member_token=member_token,
            work_item=work_item,
            state_id=state_id,
            call_index=call_index,
            call_type=CallType.VECTOR,
            status=CallStatus.ERROR,
            request_data=request_data,
            error=RawCallPayload(error_data),
            latency_ms=round(elapsed_ms),
        )
        if self._call_mode_session is not None and self._call_mode_session.mode is RunMode.VERIFY:
            self._call_mode_session.verify_call(
                call_type=CallType.VECTOR,
                request_data=request_data.to_dict(),
                current_state_id=state_id,
                current_operation_id=None,
                current_call_index=call_index,
                current_call_id=recorded.call_id,
                live_status=CallStatus.ERROR,
                live_response_data=None,
                live_error_data=error_data,
            )

    def check_readiness(self) -> CollectionReadinessResult:
        """Check that the ChromaDB collection is reachable and has documents.

        Called during runtime preflight AFTER provider construction. self._collection
        is set by __init__ (which calls get_collection — fails fast
        if collection doesn't exist). If __init__ fails, the provider doesn't
        exist and this method is never called.
        """
        collection_name = self._config.collection
        request_data = {"operation": "readiness_count", "collection": collection_name}
        session = self._call_mode_session
        operation_id = self._operation_id
        coordination_token = self._coordination_token
        if session is not None and (operation_id is None or coordination_token is None):
            raise RetrievalError("Chroma replay/verify readiness requires an audited operation parent", retryable=False)
        call_index: int | None = None
        if operation_id is not None and coordination_token is not None:
            call_index = self._execution.allocate_operation_call_index(operation_id, coordination_token=coordination_token)
        if session is not None and session.mode is RunMode.REPLAY:
            assert operation_id is not None and coordination_token is not None and call_index is not None
            evidence = session.replay_call(
                call_type=CallType.VECTOR,
                request_data=request_data,
                current_state_id=None,
                current_operation_id=operation_id,
                current_call_index=call_index,
            )
            response = evidence.response_data
            if evidence.status is not CallStatus.SUCCESS or response is None:
                raise RetrievalError("Chroma replay readiness has no successful retained response", retryable=False)
            try:
                count = response["collection_count"]
            except KeyError as exc:
                raise RetrievalError("Chroma replay readiness has no valid retained collection count", retryable=False) from exc
            if type(count) is not int or count < 0:
                raise RetrievalError("Chroma replay readiness has no valid retained collection count", retryable=False)
            self._execution.record_operation_call(
                operation_id=operation_id,
                coordination_token=coordination_token,
                call_index=call_index,
                call_type=CallType.VECTOR,
                status=CallStatus.SUCCESS,
                request_data=RawCallPayload(request_data),
                response_data=RawCallPayload({"collection_count": count}),
                latency_ms=evidence.latency_ms,
                source_call_id=evidence.source_call_id,
            )
            return CollectionReadinessResult(
                collection=collection_name,
                reachable=True,
                count=count,
                message=f"Collection '{collection_name}' has {count} documents"
                if count > 0
                else f"Collection '{collection_name}' is empty",
            )
        if session is not None and session.mode is RunMode.VERIFY:
            assert operation_id is not None and call_index is not None
            session.admit_verify_call(
                call_type=CallType.VECTOR,
                request_data=request_data,
                current_state_id=None,
                current_operation_id=operation_id,
                current_call_index=call_index,
            )
        if self._collection is None:
            self._initialize_client()

        try:
            count = self._collection.count()
            if type(count) is not int or count < 0:
                raise ValueError(f"malformed collection count: expected a non-negative exact int, got {count!r} ({type(count).__name__})")
            if count > 0:
                message = f"Collection '{collection_name}' has {count} documents"
            else:
                message = f"Collection '{collection_name}' is empty"
            if operation_id is not None and coordination_token is not None:
                assert call_index is not None
                response_data = {"collection_count": count}
                recorded = self._execution.record_operation_call(
                    operation_id=operation_id,
                    coordination_token=coordination_token,
                    call_index=call_index,
                    call_type=CallType.VECTOR,
                    status=CallStatus.SUCCESS,
                    request_data=RawCallPayload(request_data),
                    response_data=RawCallPayload(response_data),
                )
                if session is not None and session.mode is RunMode.VERIFY:
                    session.verify_call(
                        call_type=CallType.VECTOR,
                        request_data=request_data,
                        current_state_id=None,
                        current_operation_id=operation_id,
                        current_call_index=call_index,
                        current_call_id=recorded.call_id,
                        live_status=CallStatus.SUCCESS,
                        live_response_data=response_data,
                        live_error_data=None,
                    )
            return CollectionReadinessResult(
                collection=collection_name,
                reachable=True,
                count=count,
                message=message,
            )
        except (chromadb.errors.ChromaError, ConnectionError, OSError, ValueError, httpx.HTTPError) as exc:
            # ValueError: chromadb 1.5.5 raises plain ValueError on unreachable HTTP server.
            # httpx.HTTPError: httpx transport errors inherit only from Exception,
            # not from ChromaError/ConnectionError/OSError.
            return CollectionReadinessResult(
                collection=collection_name,
                reachable=False,
                count=None,
                message=f"Collection '{collection_name}' unreachable: {type(exc).__name__}: {exc}",
            )

    def runtime_preflight(self, *, operation_id: str, coordination_token: CoordinationToken) -> CollectionReadinessResult:
        """Run the collection check beneath the engine's preflight operation."""
        self._operation_id = operation_id
        self._coordination_token = coordination_token
        return self.check_readiness()

    def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            client.close()  # type: ignore[attr-defined]
