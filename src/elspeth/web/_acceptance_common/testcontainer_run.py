"""Testcontainer-run receipt — the acceptance driver records the PostgreSQL proofs it ran.

The ``testcontainer``-marked suites are the only proofs of PostgreSQL contention
behaviour in the tree (``pytest tests/`` deselects them), and CI's required
``Testcontainer (PostgreSQL contention proofs)`` job is their only standing run.
An acceptance run against a provisioned PostgreSQL re-runs the SAME selection
there and stores the outcome as a receipt of kind ``testcontainer-run``: the
selection (pinned token-for-token to the CI job, so a driver cannot quietly
narrow it), the pytest exit code, and the id counts read from the junit report
the run wrote. The receipt is a record of what ran, not a verdict: a failing
run is recorded with its exit code, and whether that blocks the gate is the
evidence ledger's decision (its ``tests`` stage), never this module's.

The receipt also says WHICH PostgreSQL the selection ran against
(``database``): the suites obtain their server through one seam,
``tests/helpers/postgres_target.py``, which honours
:data:`PROVISIONED_POSTGRES_URL_ENV`; the driver's receipt derives the field
from the same variable (:func:`resolve_testcontainer_run_target`), so it
cannot claim a provisioned run the suites did not make. The identity hash
covers host, port and database name only — never credentials.

One validator and ONE gate predicate (:func:`testcontainer_run_gate`) serve both
providers; each binds its own schema id
(:data:`TESTCONTAINER_RUN_SCHEMAS`). The kind is NEW in 0.8.0 — no existing
receipt gains a field, so every receipt produced before its introduction
validates byte-for-byte as before, and every field of a ``testcontainer-run``
receipt is required (closed set, adversarial rejects) from the first one.

Layer: L2 (acceptance policy, provider-neutral). Tier-3 boundaries:
:func:`parse_junit_report` (pytest's junit XML) and
:func:`validate_testcontainer_run_receipt` (a receipt read back from the store).
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal, TypedDict
from xml.etree import ElementTree

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from elspeth.contracts.trust_boundary import trust_boundary

from .errors import AcceptanceCheckError, AcceptanceInputError
from .receipt_validation import _GIT_SHA_PATTERN, _SCENARIO_ID_PATTERN, _parse_utc_z_timestamp, _sha256, _utc_timestamp

TESTCONTAINER_RUN_RECEIPT_KIND: Final = "testcontainer-run"
"""The receipt kind both provider bindings store the run under."""

TESTCONTAINER_SELECTION: Final[tuple[str, ...]] = ("tests/", "-m", "testcontainer", "-n", "0", "--junitxml=testcontainer-junit.xml")
"""The pytest arguments of the run a receipt may record — exactly CI's testcontainer job.

``tests/`` (never ``tests/testcontainer/``: marked ids live outside that directory
too), ``-m testcontainer``, ``-n 0`` (``tests/testcontainer/web/conftest.py``
shares one container and rejects xdist workers) and the junit path the counts
are read from. ``tests/unit/web/acceptance_common/test_testcontainer_run.py``
pins every token against ``.github/workflows/ci.yaml``.
"""

Provider = Literal["aws", "azure"]

TESTCONTAINER_RUN_SCHEMAS: Final[Mapping[Provider, str]] = MappingProxyType(
    {
        "aws": "elspeth.aws-ecs-testcontainer-run.v1",
        "azure": "elspeth.azure-container-apps-testcontainer-run.v1",
    }
)
"""Provider-scoped schema ids: one validator, two bindings, neither accepts the other's receipt."""

PROVISIONED_POSTGRES_URL_ENV: Final = "ELSPETH_TEST_POSTGRES_URL"
"""The variable the suites' seam and this receipt both read: set, the selection ran against that server."""

TestcontainerRunDatabase = Literal["testcontainers-docker", "provisioned"]
TESTCONTAINER_RUN_DATABASES: Final[frozenset[str]] = frozenset({"testcontainers-docker", "provisioned"})
"""``testcontainers-docker``: each suite provisioned its own container on the driver host; ``provisioned``: the named server."""

TESTCONTAINERS_DOCKER_IDENTITY_SHA256: Final = _sha256(b"testcontainers-docker")
"""The one identity a ``testcontainers-docker`` receipt may carry: there is no server to name, so the literal is hashed."""

MAX_JUNIT_BYTES: Final = 16 * 1024 * 1024
"""Upper bound on a junit report the driver will read (CI's whole-tree run is well under 1 MiB)."""

_MAX_COUNT: Final = 100_000
_OUTCOME_TAGS: Final = frozenset({"failure", "error", "skipped"})


class TestcontainerRunReceipt(TypedDict):
    """The owned ``testcontainer-run`` receipt document: every field required, none optional.

    A ``TypedDict`` (not a dataclass) because the receipt IS the stored JSON
    document — ``json.dumps(receipt, sort_keys=True)`` is the wire form both
    providers hash and store, and the field set here is the closed set
    :func:`validate_testcontainer_run_receipt` admits.
    """

    schema: str
    kind: str
    candidate_sha: str
    scenario_id: str
    selection: list[str]
    database: str
    database_identity_sha256: str
    exit_code: int
    collected: int
    passed: int
    failed: int
    errors: int
    skipped: int
    junit_sha256: str
    recorded_at: str


_RECEIPT_FIELDS: Final[frozenset[str]] = frozenset(TestcontainerRunReceipt.__required_keys__)
"""The closed field set, derived from the owned type so the validator cannot drift from it."""


class ReceiptIndexRow(TypedDict):
    """One row of a provider's already-validated receipt index (the ECS control manifest's ``evidence.receipts``)."""

    scenario_id: str
    kind: str
    subject_sha256: str
    receipt_sha256: str
    stored_at: str


@dataclass(frozen=True)
class TestcontainerRunRecord:
    """What the junit report says ran: one id per ``<testcase>``, classified by its outcome child."""

    collected: int
    passed: int
    failed: int
    errors: int
    skipped: int
    junit_sha256: str

    def __post_init__(self) -> None:
        counts = (self.collected, self.passed, self.failed, self.errors, self.skipped)
        if any(type(count) is not int or not 0 <= count <= _MAX_COUNT for count in counts):
            raise ValueError("testcontainer run counts must be bounded non-negative integers")
        if self.passed + self.failed + self.errors + self.skipped != self.collected:
            raise ValueError("testcontainer run outcomes must partition the collected ids")
        if type(self.junit_sha256) is not str or len(self.junit_sha256) != 64 or set(self.junit_sha256) - set("0123456789abcdef"):
            raise ValueError("junit_sha256 must be a lowercase hex sha256")


@dataclass(frozen=True)
class TestcontainerRunTarget:
    """Which PostgreSQL the selection ran against, as the seam variable said."""

    database: TestcontainerRunDatabase
    database_identity_sha256: str

    def __post_init__(self) -> None:
        if self.database not in TESTCONTAINER_RUN_DATABASES:
            raise ValueError("database must be testcontainers-docker or provisioned")
        if not _is_hex_sha256(self.database_identity_sha256):
            raise ValueError("database_identity_sha256 must be a lowercase hex sha256")
        if (self.database == "testcontainers-docker") != (self.database_identity_sha256 == TESTCONTAINERS_DOCKER_IDENTITY_SHA256):
            raise ValueError("a testcontainers-docker target carries the fixed identity and a provisioned one never does")


def _is_int(value: object) -> bool:
    return type(value) is int


def _is_hex_sha256(value: object) -> bool:
    return type(value) is str and len(value) == 64 and not (set(value) - set("0123456789abcdef"))


@trust_boundary(
    tier=3,
    source="the acceptance driver's process environment, the same ELSPETH_TEST_POSTGRES_URL the suites' seam reads",
    source_param="environ",
    suppresses=("R1", "R5"),
    invariant=(
        "returns the owned testcontainers-docker target when the variable is unset or blank; otherwise raises "
        "AcceptanceInputError before use unless the value is a PostgreSQL URL naming host, role, password and database, "
        "and then returns the owned provisioned target whose identity hashes host, port and database only"
    ),
    test_ref="tests/unit/web/acceptance_common/test_testcontainer_run.py::test_resolve_target_reads_only_the_seam_variable_and_never_hashes_credentials",
    test_fingerprint="d1dc52429f853811c82bbd6d8966ea9da3f15578c14d8deadae36e31a4430fc8",
)
def resolve_testcontainer_run_target(environ: Mapping[str, str]) -> TestcontainerRunTarget:
    """Derive the receipt's ``database`` fields from the seam variable, never from a flag."""

    raw = environ.get(PROVISIONED_POSTGRES_URL_ENV)
    if raw is None or raw.strip() == "":
        return TestcontainerRunTarget(database="testcontainers-docker", database_identity_sha256=TESTCONTAINERS_DOCKER_IDENTITY_SHA256)
    try:
        url = make_url(raw)
    except ArgumentError:
        raise AcceptanceInputError(f"{PROVISIONED_POSTGRES_URL_ENV} is not a SQLAlchemy URL") from None
    if url.get_backend_name() != "postgresql" or not url.host or not url.username or url.password is None or not url.database:
        raise AcceptanceInputError(f"{PROVISIONED_POSTGRES_URL_ENV} must be a postgresql URL naming host, role, password and database")
    port = 5432 if url.port is None else url.port
    identity = f"{url.host}:{port}/{url.database}".encode()
    return TestcontainerRunTarget(database="provisioned", database_identity_sha256=_sha256(identity))


@trust_boundary(
    tier=3,
    source="the junit XML report pytest wrote for the testcontainer run, read from the acceptance harness filesystem",
    source_param="content",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError('testcontainer_junit') before use unless the bytes are a bounded, DTD-free "
        "junit document with at least one <testcase>, each classified by at most one failure/error/skipped child; "
        "returns only the owned TestcontainerRunRecord whose outcomes partition the collected count"
    ),
    test_ref="tests/unit/web/acceptance_common/test_testcontainer_run.py::test_parse_junit_report_rejects_malformed_reports",
    test_fingerprint="b7e38e832933fd68d4de535884e201ad480a4163db0e4ecd2726f788742f2d72",
)
def parse_junit_report(content: bytes) -> TestcontainerRunRecord:
    """Count the ids and outcomes in a pytest junit report without trusting its shape."""

    if type(content) is not bytes or not content or len(content) > MAX_JUNIT_BYTES:
        raise AcceptanceCheckError("testcontainer_junit")
    # No DTD, no entities: the report is produced by pytest and never carries
    # either, and the standard-library parser would otherwise expand them.
    if b"<!DOCTYPE" in content or b"<!ENTITY" in content:
        raise AcceptanceCheckError("testcontainer_junit")
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        raise AcceptanceCheckError("testcontainer_junit") from None
    if root.tag not in {"testsuites", "testsuite"}:
        raise AcceptanceCheckError("testcontainer_junit")
    collected = passed = failed = errors = skipped = 0
    for case in root.iter("testcase"):
        outcomes = [child.tag for child in case if child.tag in _OUTCOME_TAGS]
        if len(outcomes) > 1:
            raise AcceptanceCheckError("testcontainer_junit")
        collected += 1
        if not outcomes:
            passed += 1
        elif outcomes[0] == "failure":
            failed += 1
        elif outcomes[0] == "error":
            errors += 1
        else:
            skipped += 1
    if collected == 0 or collected > _MAX_COUNT:
        raise AcceptanceCheckError("testcontainer_junit")
    return TestcontainerRunRecord(
        collected=collected,
        passed=passed,
        failed=failed,
        errors=errors,
        skipped=skipped,
        junit_sha256=_sha256(content),
    )


def read_junit_report(path: Path) -> TestcontainerRunRecord:
    """Read one regular junit file within the byte bound and parse it."""

    try:
        report_stat = path.lstat()
    except OSError:
        raise AcceptanceCheckError("testcontainer_junit") from None
    if not stat.S_ISREG(report_stat.st_mode) or report_stat.st_size > MAX_JUNIT_BYTES:
        raise AcceptanceCheckError("testcontainer_junit")
    try:
        content = path.read_bytes()
    except OSError:
        raise AcceptanceCheckError("testcontainer_junit") from None
    return parse_junit_report(content)


def build_testcontainer_run_receipt(
    *,
    provider: Provider,
    candidate_sha: str,
    scenario_id: str,
    exit_code: int,
    record: TestcontainerRunRecord,
    target: TestcontainerRunTarget,
    recorded_at: datetime,
) -> TestcontainerRunReceipt:
    """The receipt the driver stores; refuses inputs the validator would refuse."""

    if provider not in TESTCONTAINER_RUN_SCHEMAS:
        raise AcceptanceInputError("provider must be aws or azure")
    if type(candidate_sha) is not str or _GIT_SHA_PATTERN.fullmatch(candidate_sha) is None:
        raise AcceptanceInputError("candidate_sha must be a git sha")
    if type(scenario_id) is not str or _SCENARIO_ID_PATTERN.fullmatch(scenario_id) is None:
        raise AcceptanceInputError("scenario_id must be a bounded identifier")
    if not _is_int(exit_code) or not 0 <= exit_code <= 255:
        raise AcceptanceInputError("exit_code must be a process exit status")
    # ``record`` and ``target`` are the owned types (nominally typed; mypy
    # holds the caller to them); only the clock needs a runtime check.
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise AcceptanceInputError("recorded_at must be an aware datetime")
    receipt: TestcontainerRunReceipt = {
        "schema": TESTCONTAINER_RUN_SCHEMAS[provider],
        "kind": TESTCONTAINER_RUN_RECEIPT_KIND,
        "candidate_sha": candidate_sha,
        "scenario_id": scenario_id,
        "selection": list(TESTCONTAINER_SELECTION),
        "database": target.database,
        "database_identity_sha256": target.database_identity_sha256,
        "exit_code": exit_code,
        "collected": record.collected,
        "passed": record.passed,
        "failed": record.failed,
        "errors": record.errors,
        "skipped": record.skipped,
        "junit_sha256": record.junit_sha256,
        "recorded_at": _utc_timestamp(recorded_at),
    }
    return validate_testcontainer_run_receipt(
        receipt,
        provider=provider,
        candidate_sha=candidate_sha,
        scenario_id=scenario_id,
        subject_sha256=_sha256(record.junit_sha256.encode("utf-8")),
    )


@trust_boundary(
    tier=3,
    source="a testcontainer-run receipt read back from either provider's acceptance receipt store",
    source_param="payload",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError('receipt_store_schema' or 'receipt_store_binding') before use unless the payload "
        "is a dict with exactly the testcontainer-run fields, the provider's own schema id, the pinned selection, a "
        "database of testcontainers-docker (with its fixed identity) or provisioned (with any other sha256 identity), a "
        "process exit code and bounded counts that partition the collected ids and agree with the exit code, bound "
        "to the caller's candidate sha, scenario and junit subject hash"
    ),
    test_ref="tests/unit/web/acceptance_common/test_testcontainer_run.py::test_validate_testcontainer_run_receipt_rejects_open_or_inconsistent_receipts",
    test_fingerprint="aacce4e4ab930363d63e7afef5a013abd26bab59891c676b517f537fec218d6e",
)
def validate_testcontainer_run_receipt(
    payload: object,
    *,
    provider: Provider,
    candidate_sha: str,
    scenario_id: str,
    subject_sha256: str,
) -> TestcontainerRunReceipt:
    """Admit one stored ``testcontainer-run`` receipt for ``provider`` or raise.

    Returns a freshly constructed :class:`TestcontainerRunReceipt` built from the
    owned constants and the validated scalars — never the caller's payload
    object — so what leaves the boundary is the owned type by construction.
    """

    if provider not in TESTCONTAINER_RUN_SCHEMAS:
        raise AcceptanceInputError("provider must be aws or azure")
    if not isinstance(payload, dict) or set(payload) != _RECEIPT_FIELDS:
        raise AcceptanceCheckError("receipt_store_schema")
    schema = payload["schema"]
    kind = payload["kind"]
    selection = payload["selection"]
    database = payload["database"]
    database_identity_sha256 = payload["database_identity_sha256"]
    exit_code = payload["exit_code"]
    counts = {name: payload[name] for name in ("collected", "passed", "failed", "errors", "skipped")}
    junit_sha256 = payload["junit_sha256"]
    recorded_at = payload["recorded_at"]
    if (
        schema != TESTCONTAINER_RUN_SCHEMAS[provider]
        or kind != TESTCONTAINER_RUN_RECEIPT_KIND
        or not isinstance(selection, list)
        or tuple(selection) != TESTCONTAINER_SELECTION
        or database not in TESTCONTAINER_RUN_DATABASES
        or not _is_hex_sha256(database_identity_sha256)
        or (database == "testcontainers-docker") != (database_identity_sha256 == TESTCONTAINERS_DOCKER_IDENTITY_SHA256)
        or not _is_int(exit_code)
        or not 0 <= exit_code <= 255
        or any(not _is_int(count) or not 0 <= count <= _MAX_COUNT for count in counts.values())
        or not _is_hex_sha256(junit_sha256)
        or type(recorded_at) is not str
    ):
        raise AcceptanceCheckError("receipt_store_schema")
    if (
        counts["collected"] == 0
        or counts["passed"] + counts["failed"] + counts["errors"] + counts["skipped"] != counts["collected"]
        or (exit_code == 0) != (counts["failed"] == 0 and counts["errors"] == 0)
    ):
        raise AcceptanceCheckError("receipt_store_schema")
    try:
        _parse_utc_z_timestamp(recorded_at)
    except ValueError:
        raise AcceptanceCheckError("receipt_store_schema") from None
    if (
        payload["candidate_sha"] != candidate_sha
        or payload["scenario_id"] != scenario_id
        or _sha256(junit_sha256.encode("utf-8")) != subject_sha256
    ):
        raise AcceptanceCheckError("receipt_store_binding")
    return TestcontainerRunReceipt(
        schema=TESTCONTAINER_RUN_SCHEMAS[provider],
        kind=TESTCONTAINER_RUN_RECEIPT_KIND,
        candidate_sha=candidate_sha,
        scenario_id=scenario_id,
        selection=list(TESTCONTAINER_SELECTION),
        database=database,
        database_identity_sha256=database_identity_sha256,
        exit_code=exit_code,
        collected=counts["collected"],
        passed=counts["passed"],
        failed=counts["failed"],
        errors=counts["errors"],
        skipped=counts["skipped"],
        junit_sha256=junit_sha256,
        recorded_at=recorded_at,
    )


GateReason = Literal[
    "testcontainer_run_missing",
    "testcontainer_run_ambiguous",
    "testcontainer_run_invalid",
    "testcontainer_run_failed",
]
TESTCONTAINER_RUN_GATE_REASONS: Final[frozenset[str]] = frozenset(
    {"testcontainer_run_missing", "testcontainer_run_ambiguous", "testcontainer_run_invalid", "testcontainer_run_failed"}
)
"""The closed set of reasons the gate refuses with; each names exactly what the store lacked."""


@dataclass(frozen=True)
class TestcontainerRunGateVerdict:
    """Whether the candidate has exactly one passing testcontainer run on record, and which receipt proves it."""

    passed: bool
    reason: GateReason | None
    receipt_sha256: str | None

    def __post_init__(self) -> None:
        if self.passed != (self.reason is None) or self.passed != (self.receipt_sha256 is not None):
            raise ValueError("a passing verdict names its receipt and no reason; a refusal names its reason and no receipt")
        if self.reason is not None and self.reason not in TESTCONTAINER_RUN_GATE_REASONS:
            raise ValueError("gate reason must be one of the closed set")


def _canonical_sha256(document: TestcontainerRunReceipt) -> str:
    return _sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def testcontainer_run_gate(
    receipt_index: Sequence[ReceiptIndexRow],
    *,
    provider: Provider,
    candidate_sha: str,
    read_receipt: Callable[[str], object],
) -> TestcontainerRunGateVerdict:
    """The ONE gate predicate: refuse unless exactly one passing testcontainer run is on record.

    ``receipt_index`` is the provider's already-validated receipt index (the
    ECS control manifest's ``evidence.receipts`` rows: ``scenario_id``,
    ``kind``, ``subject_sha256``, ``receipt_sha256``, ``stored_at``);
    ``read_receipt`` loads the stored document for one ``receipt_sha256``.
    Every ``testcontainer-run`` row is validated for ``provider`` and
    ``candidate_sha`` through :func:`validate_testcontainer_run_receipt`, and
    the document must hash to the row that indexes it. A candidate passes iff
    exactly one such receipt validates AND records exit code 0: no receipt is
    ``testcontainer_run_missing``; a receipt that no longer validates (or hashes
    elsewhere) is ``testcontainer_run_invalid``; only failing runs on record is
    ``testcontainer_run_failed`` (a failed run stays in the store as evidence
    and is superseded, not erased, by a later passing one); more than one
    passing run is ``testcontainer_run_ambiguous``. Absence is a refusal, never
    a pass: a candidate whose driver skipped the run cannot export evidence.
    """

    if provider not in TESTCONTAINER_RUN_SCHEMAS:
        raise AcceptanceInputError("provider must be aws or azure")
    rows = [row for row in receipt_index if row["kind"] == TESTCONTAINER_RUN_RECEIPT_KIND]
    if not rows:
        return TestcontainerRunGateVerdict(passed=False, reason="testcontainer_run_missing", receipt_sha256=None)
    passing: list[str] = []
    for row in rows:
        receipt_sha256 = row["receipt_sha256"]
        if type(receipt_sha256) is not str:
            return TestcontainerRunGateVerdict(passed=False, reason="testcontainer_run_invalid", receipt_sha256=None)
        try:
            document = validate_testcontainer_run_receipt(
                read_receipt(receipt_sha256),
                provider=provider,
                candidate_sha=candidate_sha,
                scenario_id=row["scenario_id"],
                subject_sha256=row["subject_sha256"],
            )
        except AcceptanceCheckError:
            return TestcontainerRunGateVerdict(passed=False, reason="testcontainer_run_invalid", receipt_sha256=None)
        if _canonical_sha256(document) != receipt_sha256:
            return TestcontainerRunGateVerdict(passed=False, reason="testcontainer_run_invalid", receipt_sha256=None)
        if document["exit_code"] == 0:
            passing.append(receipt_sha256)
    if not passing:
        return TestcontainerRunGateVerdict(passed=False, reason="testcontainer_run_failed", receipt_sha256=None)
    if len(passing) > 1:
        return TestcontainerRunGateVerdict(passed=False, reason="testcontainer_run_ambiguous", receipt_sha256=None)
    return TestcontainerRunGateVerdict(passed=True, reason=None, receipt_sha256=passing[0])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="testcontainer-run-receipt", add_help=True)
    parser.add_argument("--provider", required=True, choices=sorted(TESTCONTAINER_RUN_SCHEMAS))
    parser.add_argument("--junit", required=True, help="the junit report the pinned selection wrote")
    parser.add_argument("--exit-code", required=True, type=int, help="pytest's exit status for that run")
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--scenario-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """``testcontainer-run-receipt``: print the receipt for one run on stdout.

    Exit 0 when a receipt was produced (whatever the run's own exit code — the
    receipt records it), 2 when the inputs cannot yield one. The receipt's
    ``junit_sha256`` is the ``--subject-id`` the provider's ``receipt-store``
    command binds it under.
    """

    args = build_parser().parse_args(argv)
    try:
        record = read_junit_report(Path(args.junit))
        receipt = build_testcontainer_run_receipt(
            provider=args.provider,
            candidate_sha=args.candidate_sha,
            scenario_id=args.scenario_id,
            exit_code=args.exit_code,
            record=record,
            target=resolve_testcontainer_run_target(os.environ),
            recorded_at=datetime.now(UTC),
        )
    except AcceptanceCheckError as exc:
        json.dump({"receipt": TESTCONTAINER_RUN_RECEIPT_KIND, "error": exc.check}, sys.stdout, sort_keys=True)
        sys.stdout.write("\n")
        return 2
    except AcceptanceInputError as exc:
        json.dump({"receipt": TESTCONTAINER_RUN_RECEIPT_KIND, "error": "input_invalid", "detail": str(exc)}, sys.stdout, sort_keys=True)
        sys.stdout.write("\n")
        return 2
    json.dump(receipt, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
