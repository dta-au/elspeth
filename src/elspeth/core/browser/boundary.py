"""Fail-closed browser request admission and archived-output replay contracts.

This module opens no sockets and starts no browser. A deployed gateway must
provide an independently enforced network boundary before using admission
tokens to forward traffic. No browser plugin is registered by this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from elspeth.core.security.web import HTTPOrigin, SSRFBlockedError, parse_http_origin, validate_allowed_http_origin

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class BrowserSecurityError(ValueError):
    """Browser job, gateway request, or archived evidence was refused."""


@dataclass(frozen=True, slots=True)
class BrowserBudgets:
    max_requests: int = 32
    max_response_bytes: int = 4 * 1024 * 1024
    max_total_response_bytes: int = 16 * 1024 * 1024
    max_wall_seconds: int = 30
    max_pages: int = 2

    def __post_init__(self) -> None:
        for value in (
            self.max_requests,
            self.max_response_bytes,
            self.max_total_response_bytes,
            self.max_wall_seconds,
            self.max_pages,
        ):
            if type(value) is not int or value <= 0:
                raise BrowserSecurityError("browser budgets must be positive integers")


@dataclass(frozen=True, slots=True)
class BrowserJob:
    job_id: str
    start_url: str
    allowed_origins: tuple[str, ...]
    budgets: BrowserBudgets = field(default_factory=BrowserBudgets)
    version: int = 1

    def __post_init__(self) -> None:
        if (
            type(self.version) is not int
            or self.version != 1
            or type(self.job_id) is not str
            or not (1 <= len(self.job_id) <= 128)
            or not isinstance(self.budgets, BrowserBudgets)
        ):
            raise BrowserSecurityError("unsupported browser job contract")
        if any(ord(char) < 33 or ord(char) > 126 for char in self.job_id):
            raise BrowserSecurityError("browser job ID is invalid")
        if (
            type(self.start_url) is not str
            or type(self.allowed_origins) is not tuple
            or not self.allowed_origins
            or any(type(origin) is not str for origin in self.allowed_origins)
        ):
            raise BrowserSecurityError("browser job requires a URL and explicit origins")
        try:
            self.start_url.encode("utf-8")
            for origin in self.allowed_origins:
                origin.encode("utf-8")
            origins = self.parsed_origins
            validate_allowed_http_origin(self.start_url, origins)
        except (SSRFBlockedError, ValueError, TypeError) as exc:
            raise BrowserSecurityError("browser start URL or allowed origin is invalid") from exc

    @property
    def parsed_origins(self) -> tuple[HTTPOrigin, ...]:
        return tuple(parse_http_origin(origin) for origin in self.allowed_origins)


@dataclass(frozen=True, slots=True)
class BrowserRequest:
    request_id: str
    index: int
    parent_request_id: str | None
    url: str
    method: str


@dataclass(frozen=True, slots=True)
class BrowserRequestRecord:
    """Persistence-safe request identity; URL path/query bytes are hashed."""

    request_id: str
    index: int
    parent_request_id: str | None
    origin: HTTPOrigin | None
    url_sha256: str
    method: str


class BrowserAdmissionJournal(Protocol):
    """Gateway-owned durable journal. Record must commit before admit returns."""

    def record(self, kind: str, request: BrowserRequestRecord) -> None: ...

    def record_response(self, evidence: BrowserRequestEvidence) -> None: ...


class BoundedPayloadReader(Protocol):
    def retrieve_bounded(self, content_hash: str, *, max_bytes: int) -> bytes | None: ...


class PayloadWriter(Protocol):
    def store(self, content: bytes) -> str: ...


def _validate_request(job: BrowserJob, request: BrowserRequest, *, expected_index: int) -> None:
    if (
        type(request.request_id) is not str
        or not (1 <= len(request.request_id) <= 128)
        or type(request.index) is not int
        or request.index != expected_index
        or (request.parent_request_id is not None and type(request.parent_request_id) is not str)
    ):
        raise BrowserSecurityError("browser request order or identity is invalid")
    if request.method not in ("GET", "HEAD"):
        raise BrowserSecurityError("browser request method is not admitted")
    if type(request.url) is not str:
        raise BrowserSecurityError("browser request URL is invalid")
    try:
        request.url.encode("utf-8")
        validate_allowed_http_origin(request.url, job.parsed_origins)
    except (SSRFBlockedError, ValueError, TypeError) as exc:
        raise BrowserSecurityError("browser request origin is not admitted") from exc


def _request_record(request: BrowserRequest, *, admitted: bool) -> BrowserRequestRecord:
    origin: HTTPOrigin | None = None
    if admitted:
        parsed = urlsplit(request.url)
        scheme = parsed.scheme.lower()
        origin = (scheme, parsed.hostname or "", parsed.port or (443 if scheme == "https" else 80))
    # Denials must remain journalable even when the URL has invalid Unicode.
    digest = hashlib.sha256(request.url.encode("utf-8", errors="surrogatepass")).hexdigest() if type(request.url) is str else "0" * 64
    return BrowserRequestRecord(request.request_id, request.index, request.parent_request_id, origin, digest, request.method)


class BrowserGatewayAdmission:
    """Pre-DNS gateway admission. It grants no socket capability by itself."""

    def __init__(self, job: BrowserJob, journal: BrowserAdmissionJournal) -> None:
        self._job = job
        self._journal = journal
        self._next_index = 0
        self._seen_ids: set[str] = set()
        self._accepted: dict[str, BrowserRequestRecord] = {}
        self._terminal: set[str] = set()
        self._total_response_bytes = 0

    def admit(self, request: BrowserRequest) -> None:
        try:
            if self._next_index >= self._job.budgets.max_requests:
                raise BrowserSecurityError("browser request budget exhausted")
            _validate_request(self._job, request, expected_index=self._next_index)
            if request.request_id in self._seen_ids or (
                request.parent_request_id is not None and request.parent_request_id not in self._seen_ids
            ):
                raise BrowserSecurityError("browser request graph is invalid")
        except BrowserSecurityError:
            self._journal.record("denied", _request_record(request, admitted=False))
            raise
        record = _request_record(request, admitted=True)
        self._journal.record("admitted", record)
        self._seen_ids.add(request.request_id)
        self._accepted[request.request_id] = record
        self._next_index += 1

    def finish_response(
        self,
        request_id: str,
        body: bytes,
        *,
        status: int,
        payload_store: PayloadWriter,
    ) -> BrowserRequestEvidence:
        """Persist bounded response bytes and evidence before releasing them."""
        if request_id not in self._accepted:
            raise BrowserSecurityError("browser response has no admitted request")
        record = self._accepted[request_id]
        if request_id in self._terminal:
            raise BrowserSecurityError("browser response is already terminal")
        # Once completion begins, a failed cap, storage write, or audit write
        # cannot be turned into success with a second body.
        self._terminal.add(request_id)
        if (
            type(body) is not bytes
            or len(body) > self._job.budgets.max_response_bytes
            or (self._total_response_bytes + len(body) > self._job.budgets.max_total_response_bytes)
        ):
            self._journal.record("response_denied", record)
            raise BrowserSecurityError("browser response budget exceeded")
        if type(status) is not int or not (100 <= status <= 599):
            self._journal.record("response_denied", record)
            raise BrowserSecurityError("browser response status is invalid")
        digest = payload_store.store(body)
        if digest != hashlib.sha256(body).hexdigest():
            self._journal.record("response_denied", record)
            raise BrowserSecurityError("browser payload store returned an invalid reference")
        evidence = BrowserRequestEvidence(record, digest, len(body), status)
        self._journal.record_response(evidence)
        self._total_response_bytes += len(body)
        return evidence


@dataclass(frozen=True, slots=True)
class BrowserRequestEvidence:
    request: BrowserRequestRecord
    response_sha256: str
    response_size: int
    response_status: int

    @classmethod
    def from_response(
        cls, request: BrowserRequest, *, response_sha256: str, response_size: int, response_status: int
    ) -> BrowserRequestEvidence:
        """Convert a validated live request into a safe archive record."""
        return cls(_request_record(request, admitted=True), response_sha256, response_size, response_status)


@dataclass(frozen=True, slots=True)
class BrowserArchive:
    version: int
    job_sha256: str
    requests: tuple[BrowserRequestEvidence, ...]
    output_sha256: str
    output_size: int


def browser_job_sha256(job: BrowserJob) -> str:
    body = {
        "version": job.version,
        "job_id": job.job_id,
        "start_url": job.start_url,
        "allowed_origins": list(job.allowed_origins),
        "budgets": {
            "max_requests": job.budgets.max_requests,
            "max_response_bytes": job.budgets.max_response_bytes,
            "max_total_response_bytes": job.budgets.max_total_response_bytes,
            "max_wall_seconds": job.budgets.max_wall_seconds,
            "max_pages": job.budgets.max_pages,
        },
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def browser_archive_sha256(archive: BrowserArchive) -> str:
    body = {
        "version": archive.version,
        "job_sha256": archive.job_sha256,
        "requests": [
            {
                "request_id": evidence.request.request_id,
                "index": evidence.request.index,
                "parent_request_id": evidence.request.parent_request_id,
                "origin": evidence.request.origin,
                "url_sha256": evidence.request.url_sha256,
                "method": evidence.request.method,
                "response_sha256": evidence.response_sha256,
                "response_size": evidence.response_size,
                "response_status": evidence.response_status,
            }
            for evidence in archive.requests
        ],
        "output_sha256": archive.output_sha256,
        "output_size": archive.output_size,
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _validate_payload_metadata(digest: str, size: int, cap: int) -> None:
    if type(digest) is not str or _SHA256.fullmatch(digest) is None or type(size) is not int or size < 0 or size > cap:
        raise BrowserSecurityError("browser payload metadata is invalid")


def _read_evidence(reader: BoundedPayloadReader, digest: str, size: int, cap: int) -> bytes:
    _validate_payload_metadata(digest, size, cap)
    content = reader.retrieve_bounded(digest, max_bytes=cap)
    if content is None or type(content) is not bytes or len(content) != size or hashlib.sha256(content).hexdigest() != digest:
        raise BrowserSecurityError("browser payload is missing or corrupt")
    return content


def replay_browser_archive(
    job: BrowserJob,
    archive: BrowserArchive,
    *,
    expected_archive_sha256: str,
    payload_store: BoundedPayloadReader,
) -> bytes:
    """Return checked archived DOM output; this path never opens a socket."""
    if (
        not isinstance(archive, BrowserArchive)
        or type(archive.version) is not int
        or archive.version != 1
        or type(archive.requests) is not tuple
        or type(expected_archive_sha256) is not str
        or _SHA256.fullmatch(expected_archive_sha256) is None
    ):
        raise BrowserSecurityError("browser source manifest is missing or changed")
    if (
        type(archive.job_sha256) is not str
        or archive.job_sha256 != browser_job_sha256(job)
        or len(archive.requests) > job.budgets.max_requests
    ):
        raise BrowserSecurityError("browser source job or request budget differs")
    _validate_payload_metadata(archive.output_sha256, archive.output_size, job.budgets.max_response_bytes)
    origins = job.parsed_origins
    total_size = 0
    seen_ids: set[str] = set()
    for index, evidence in enumerate(archive.requests):
        if not isinstance(evidence, BrowserRequestEvidence) or not isinstance(evidence.request, BrowserRequestRecord):
            raise BrowserSecurityError("browser source request is invalid")
        request = evidence.request
        if (
            type(request.index) is not int
            or request.index != index
            or type(request.request_id) is not str
            or not (1 <= len(request.request_id) <= 128)
            or (
                request.parent_request_id is not None
                and (type(request.parent_request_id) is not str or not (1 <= len(request.parent_request_id) <= 128))
            )
            or type(request.method) is not str
            or request.method not in ("GET", "HEAD")
            or type(request.origin) is not tuple
            or len(request.origin) != 3
            or type(request.origin[0]) is not str
            or type(request.origin[1]) is not str
            or type(request.origin[2]) is not int
            or request.origin not in origins
            or type(request.url_sha256) is not str
            or _SHA256.fullmatch(request.url_sha256) is None
        ):
            raise BrowserSecurityError("browser source request is invalid")
        if request.request_id in seen_ids or (request.parent_request_id is not None and request.parent_request_id not in seen_ids):
            raise BrowserSecurityError("browser source request graph differs")
        if type(evidence.response_status) is not int or not (100 <= evidence.response_status <= 599):
            raise BrowserSecurityError("browser source response status is invalid")
        _validate_payload_metadata(evidence.response_sha256, evidence.response_size, job.budgets.max_response_bytes)
        total_size += evidence.response_size
        if total_size > job.budgets.max_total_response_bytes:
            raise BrowserSecurityError("browser source response budget exceeded")
        seen_ids.add(request.request_id)
    # Only a complete, bounded manifest may be serialized or touch payloads.
    if browser_archive_sha256(archive) != expected_archive_sha256:
        raise BrowserSecurityError("browser source manifest is missing or changed")
    for evidence in archive.requests:
        _read_evidence(payload_store, evidence.response_sha256, evidence.response_size, job.budgets.max_response_bytes)
    return _read_evidence(payload_store, archive.output_sha256, archive.output_size, job.budgets.max_response_bytes)
