"""Browser contract and replay refusal tests; no browser is launched here."""

import hashlib
from dataclasses import asdict, replace
from unittest.mock import patch

import pytest

from elspeth.core.browser.boundary import (
    BrowserArchive,
    BrowserBudgets,
    BrowserGatewayAdmission,
    BrowserJob,
    BrowserRequest,
    BrowserRequestEvidence,
    BrowserRequestRecord,
    BrowserSecurityError,
    browser_archive_sha256,
    browser_job_sha256,
    replay_browser_archive,
)
from elspeth.plugins.infrastructure.manager import PluginManager


def _job() -> BrowserJob:
    return BrowserJob(
        job_id="row-1",
        start_url="https://search.example.gov.au/results?q=company",
        allowed_origins=("https://search.example.gov.au",),
        budgets=BrowserBudgets(max_requests=2, max_response_bytes=100, max_total_response_bytes=200),
    )


def test_unicode_request_origin_remains_replayable_after_admission() -> None:
    job = BrowserJob(job_id="unicode-row", start_url="https://bücher.example/page", allowed_origins=("https://bücher.example",))
    journal = _Journal()
    store = _Store(b"dom")
    gateway = BrowserGatewayAdmission(job, journal)
    gateway.admit(BrowserRequest(request_id="first", index=0, parent_request_id=None, url=job.start_url, method="GET"))
    evidence = gateway.finish_response("first", b"dom", status=200, payload_store=store)
    assert evidence.request.origin == ("https", "xn--bcher-kva.example", 443)
    archive = BrowserArchive(
        version=1,
        job_sha256=browser_job_sha256(job),
        requests=(evidence,),
        output_sha256=hashlib.sha256(b"dom").hexdigest(),
        output_size=3,
    )
    output = replay_browser_archive(job, archive, expected_archive_sha256=browser_archive_sha256(archive), payload_store=store)
    assert output == b"dom"
    assert journal.events == [("admitted", 0)]
    assert journal.responses == [evidence]


def _request(index: int = 0, url: str = "https://search.example.gov.au/results") -> BrowserRequest:
    return BrowserRequest(request_id=f"r-{index}", index=index, parent_request_id=None, url=url, method="GET")


class _Journal:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[tuple[str, int]] = []
        self.fail = fail
        self.responses: list[BrowserRequestEvidence] = []

    def record(self, kind: str, request: BrowserRequestRecord) -> None:
        if self.fail:
            raise OSError("audit unavailable")
        self.events.append((kind, request.index))

    def record_response(self, evidence: BrowserRequestEvidence) -> None:
        if self.fail:
            raise OSError("audit unavailable")
        self.responses.append(evidence)


class _Store:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.reads: list[tuple[str, int]] = []

    def retrieve_bounded(self, content_hash: str, *, max_bytes: int) -> bytes | None:
        self.reads.append((content_hash, max_bytes))
        return self.content if len(self.content) <= max_bytes else None

    def store(self, content: bytes) -> str:
        self.content = content
        return hashlib.sha256(content).hexdigest()


def test_browser_job_requires_explicit_exact_origin() -> None:
    with pytest.raises(BrowserSecurityError, match="origin"):
        BrowserJob(job_id="row-1", start_url="https://search.example.gov.au", allowed_origins=())
    with pytest.raises(BrowserSecurityError, match="origin"):
        BrowserJob(job_id="row-1", start_url="https://search.example.gov.au", allowed_origins=("https://example.gov.au/path",))


@pytest.mark.parametrize(
    "changes",
    [
        {"version": True},
        {"budgets": object()},
        {"start_url": "https://search.example.gov.au/" + "\ud800"},
        {"allowed_origins": ("https://search.example.gov.au", "https://" + "\ud800" + ".example.gov.au")},
    ],
)
def test_browser_job_refuses_malformed_contract_fields(changes: dict[str, object]) -> None:
    with pytest.raises(BrowserSecurityError):
        replace(_job(), **changes)


def test_browser_remains_unavailable_in_builtin_catalog() -> None:
    manager = PluginManager()
    manager.register_builtin_plugins()
    names = {transform.name for transform in manager.get_transforms()}
    assert "web_scrape" in names  # Positive control for registry discovery.
    assert "web_browser" not in names


@pytest.mark.parametrize(
    "url,method",
    [
        ("http://127.0.0.1/private", "GET"),
        ("https://other.example.gov.au/", "GET"),
        ("https://search.example.gov.au:8443/", "GET"),
        ("file:///etc/passwd", "GET"),
        ("https://search.example.gov.au/", "POST"),
    ],
)
def test_gateway_rejects_unapproved_requests_before_recording_admission(url: str, method: str) -> None:
    journal = _Journal()
    gateway = BrowserGatewayAdmission(_job(), journal)
    with pytest.raises(BrowserSecurityError):
        gateway.admit(BrowserRequest(request_id="r-0", index=0, parent_request_id=None, url=url, method=method))
    assert journal.events == [("denied", 0)]


def test_gateway_journals_denial_for_url_with_unpaired_surrogate() -> None:
    journal = _Journal()
    gateway = BrowserGatewayAdmission(_job(), journal)
    malformed = _request(url="https://search.example.gov.au/" + "\ud800")
    with pytest.raises(BrowserSecurityError):
        gateway.admit(malformed)
    assert journal.events == [("denied", 0)]


def test_gateway_requires_durable_admission_before_forwarding_and_caps_requests() -> None:
    journal = _Journal()
    gateway = BrowserGatewayAdmission(_job(), journal)
    gateway.admit(_request())
    gateway.admit(_request(1))
    with pytest.raises(BrowserSecurityError, match="request budget"):
        gateway.admit(_request(2))
    assert journal.events == [("admitted", 0), ("admitted", 1), ("denied", 2)]

    failed_journal = _Journal(fail=True)
    with pytest.raises(OSError, match="audit unavailable"):
        BrowserGatewayAdmission(_job(), failed_journal).admit(_request())


def test_gateway_holds_response_until_bounded_payload_and_evidence_are_committed() -> None:
    journal = _Journal()
    gateway = BrowserGatewayAdmission(_job(), journal)
    store = _Store(b"")
    gateway.admit(_request())
    assert journal.responses == []
    assert store.content == b""
    evidence = gateway.finish_response("r-0", b"page", status=200, payload_store=store)
    assert evidence.response_sha256 == hashlib.sha256(b"page").hexdigest()
    assert journal.responses == [evidence]
    with pytest.raises(BrowserSecurityError, match="already terminal"):
        gateway.finish_response("r-0", b"page", status=200, payload_store=store)

    failing = _Journal()
    failing_gateway = BrowserGatewayAdmission(_job(), failing)
    failing_gateway.admit(_request())
    failing.fail = True
    with pytest.raises(OSError, match="audit unavailable"):
        failing_gateway.finish_response("r-0", b"page", status=200, payload_store=_Store(b""))
    assert failing.responses == []
    failing.fail = False
    with pytest.raises(BrowserSecurityError, match="already terminal"):
        failing_gateway.finish_response("r-0", b"page", status=200, payload_store=_Store(b""))


def test_response_budget_trip_is_terminal_for_the_request() -> None:
    journal = _Journal()
    gateway = BrowserGatewayAdmission(_job(), journal)
    store = _Store(b"")
    gateway.admit(_request())
    with pytest.raises(BrowserSecurityError, match="response budget"):
        gateway.finish_response("r-0", b"x" * 101, status=200, payload_store=store)
    with pytest.raises(BrowserSecurityError, match="already terminal"):
        gateway.finish_response("r-0", b"small", status=200, payload_store=store)
    assert journal.events == [("admitted", 0), ("response_denied", 0)]
    assert journal.responses == []
    assert store.content == b""


def test_replay_checks_source_manifest_and_payload_without_network() -> None:
    content = b"<html>archived page</html>"
    ref = hashlib.sha256(content).hexdigest()
    archive = BrowserArchive(
        version=1,
        job_sha256=browser_job_sha256(_job()),
        requests=(BrowserRequestEvidence.from_response(_request(), response_sha256=ref, response_size=len(content), response_status=200),),
        output_sha256=ref,
        output_size=len(content),
    )
    store = _Store(content)
    assert replay_browser_archive(_job(), archive, expected_archive_sha256=browser_archive_sha256(archive), payload_store=store) == content
    assert store.reads == [(ref, 100), (ref, 100)]

    with pytest.raises(BrowserSecurityError, match="manifest"):
        replay_browser_archive(_job(), archive, expected_archive_sha256="0" * 64, payload_store=store)
    with pytest.raises(BrowserSecurityError, match="payload"):
        replay_browser_archive(_job(), archive, expected_archive_sha256=browser_archive_sha256(archive), payload_store=_Store(b"tampered"))


def test_archived_request_evidence_omits_sensitive_query_values() -> None:
    evidence = BrowserRequestEvidence.from_response(
        BrowserRequest(
            request_id="r-0",
            index=0,
            parent_request_id=None,
            url="https://search.example.gov.au/results?token=secret-value",
            method="GET",
        ),
        response_sha256="0" * 64,
        response_size=0,
        response_status=200,
    )
    assert "secret-value" not in repr(asdict(evidence))


def test_replay_refuses_reordered_or_unapproved_source_requests() -> None:
    content = b"page"
    ref = hashlib.sha256(content).hexdigest()
    for requests in (
        (BrowserRequestEvidence.from_response(_request(1), response_sha256=ref, response_size=len(content), response_status=200),),
        (
            BrowserRequestEvidence.from_response(
                _request(0, "https://other.example.gov.au/"), response_sha256=ref, response_size=len(content), response_status=200
            ),
        ),
    ):
        archive = BrowserArchive(1, browser_job_sha256(_job()), requests, ref, len(content))
        with pytest.raises(BrowserSecurityError):
            replay_browser_archive(_job(), archive, expected_archive_sha256=browser_archive_sha256(archive), payload_store=_Store(content))


def test_replay_refuses_malformed_response_size_as_evidence_error() -> None:
    content = b"page"
    ref = hashlib.sha256(content).hexdigest()
    malformed = BrowserRequestEvidence.from_response(_request(), response_sha256=ref, response_size="4", response_status=200)
    archive = BrowserArchive(1, browser_job_sha256(_job()), (malformed,), ref, len(content))
    with pytest.raises(BrowserSecurityError, match="metadata"):
        replay_browser_archive(_job(), archive, expected_archive_sha256=browser_archive_sha256(archive), payload_store=_Store(content))


@pytest.mark.parametrize("requests", [(object(), object(), object()), ({"unexpected": "mapping"},)])
def test_replay_rejects_oversized_or_malformed_manifest_before_hashing(requests: tuple[object, ...]) -> None:
    archive = BrowserArchive(1, browser_job_sha256(_job()), requests, "0" * 64, 0)
    store = _Store(b"")
    with pytest.raises(BrowserSecurityError):
        replay_browser_archive(_job(), archive, expected_archive_sha256="0" * 64, payload_store=store)
    assert store.reads == []


@pytest.mark.parametrize(
    "malformed_request",
    [
        object(),
        replace(BrowserRequestRecord("r-0", 0, None, ("https", "search.example.gov.au", 443), "0" * 64, "GET"), parent_request_id=[]),
        replace(BrowserRequestRecord("r-0", 0, None, ("https", "search.example.gov.au", 443), "0" * 64, "GET"), method={}),
    ],
)
def test_replay_rejects_malformed_request_fields_before_hashing(malformed_request: object) -> None:
    evidence = replace(
        BrowserRequestEvidence.from_response(_request(), response_sha256="0" * 64, response_size=0, response_status=200),
        request=malformed_request,
    )
    archive = BrowserArchive(1, browser_job_sha256(_job()), (evidence,), "0" * 64, 0)
    store = _Store(b"")
    with (
        patch("elspeth.core.browser.boundary.browser_archive_sha256", side_effect=AssertionError("unvalidated manifest was hashed")),
        pytest.raises(BrowserSecurityError),
    ):
        replay_browser_archive(_job(), archive, expected_archive_sha256="0" * 64, payload_store=store)
    assert store.reads == []


def test_replay_validates_entire_manifest_before_reading_first_payload() -> None:
    content = b"page"
    ref = hashlib.sha256(content).hexdigest()
    first = BrowserRequestEvidence.from_response(_request(), response_sha256=ref, response_size=len(content), response_status=200)
    second = replace(first, request=replace(first.request, request_id="r-1", index=1), response_size="4")
    archive = BrowserArchive(1, browser_job_sha256(_job()), (first, second), ref, len(content))
    store = _Store(content)
    with pytest.raises(BrowserSecurityError, match="metadata"):
        replay_browser_archive(_job(), archive, expected_archive_sha256=browser_archive_sha256(archive), payload_store=store)
    assert store.reads == []


def test_replay_rejects_boolean_archive_version() -> None:
    content = b"page"
    ref = hashlib.sha256(content).hexdigest()
    archive = BrowserArchive(True, browser_job_sha256(_job()), (), ref, len(content))
    store = _Store(content)
    with pytest.raises(BrowserSecurityError):
        replay_browser_archive(_job(), archive, expected_archive_sha256=browser_archive_sha256(archive), payload_store=store)
    assert store.reads == []
