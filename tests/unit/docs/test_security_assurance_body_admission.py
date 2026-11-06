"""U-47 R2: the security-assurance docs describe the body and upload limits the code enforces.

``_BodySizeLimitMiddleware`` bounds the bytes it actually receives, not only
a declared ``Content-Length``, within one read deadline. Upload routes add a
route-local ``max_upload_bytes`` ceiling inside that whole-request envelope,
and binary documents keep a separate hard ceiling. Every number below is
derived from the enforcing constant, so a changed limit or a stale document
fails here.
"""

from __future__ import annotations

from pathlib import Path

from elspeth.contracts.binary_documents import BINARY_DOCUMENT_MAX_BYTES
from elspeth.web.app import _BodySizeLimitMiddleware
from elspeth.web.config import WebSettings

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS = REPO_ROOT / "docs" / "security-assurance"
MIB = 1024 * 1024


def _mib(value: int) -> int:
    assert value % MIB == 0, f"{value} bytes is not a whole number of MiB"
    return value // MIB


BODY_MIB = _mib(_BodySizeLimitMiddleware._MAX_BODY_BYTES)
BODY_READ_SECONDS = int(_BodySizeLimitMiddleware._MAX_BODY_READ_SECONDS)
UPLOAD_DEFAULT_MIB = _mib(WebSettings.model_fields["max_upload_bytes"].default)
BINARY_MIB = _mib(BINARY_DOCUMENT_MAX_BYTES)


def _row(path: Path, row_id: str) -> str:
    rows = [line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith(f"| {row_id} |")]
    assert len(rows) == 1, f"expected exactly one {row_id} row in {path.name}"
    return rows[0]


def test_derived_limits_are_the_current_values() -> None:
    """Positive control on the derivation itself."""
    assert (BODY_MIB, BODY_READ_SECONDS, UPLOAD_DEFAULT_MIB, BINARY_MIB) == (10, 30, 100, 5)


def test_data_flow_f3_describes_received_byte_and_deadline_enforcement() -> None:
    row = _row(DOCS / "02-data-flows-and-trust-boundaries.md", "F3")
    assert "does not read or prove the size of a body with no header" not in row
    assert f"{BODY_MIB} MiB" in row
    assert "received" in row
    assert "absent or understated" in row
    assert f"{BODY_READ_SECONDS}-second" in row
    assert "does not bound the downstream response" in row


def test_data_flow_f5_distinguishes_route_local_upload_limit_from_the_request_envelope() -> None:
    row = _row(DOCS / "02-data-flows-and-trust-boundaries.md", "F5")
    assert "`max_upload_bytes`" in row
    assert "route-local" in row
    assert f"{UPLOAD_DEFAULT_MIB} MiB" in row
    assert f"{BODY_MIB} MiB complete-body" in row
    assert f"{BINARY_MIB} MiB binary-document" in row


def test_threat_model_resource_control_names_received_bytes_and_deadline() -> None:
    text = (DOCS / "04-threat-model.md").read_text(encoding="utf-8")
    start = text.index("### 4.10 Resource exhaustion")
    section = text[start : text.index("\n### ", start + 1)]
    assert f"declared {BODY_MIB} MiB API request-body ceiling" not in section
    assert "received" in section
    assert f"{BODY_READ_SECONDS}-second" in section


def test_evidence_index_labels_the_september_result_as_historical() -> None:
    index = DOCS / "16-evidence-index.md"
    boundary = _row(index, "EV-017")
    assert "without reading a body whose length is undeclared" not in boundary
    assert "received" in boundary
    assert "Historical" in boundary and "`49c184508`" in boundary
    assert "tests/unit/web/test_body_size_limits.py" in boundary
    assert "tests/unit/web/test_mounted_body_rejection_order.py" in boundary
    resource = _row(index, "EV-408")
    assert "received" in resource
    assert "Historical" in resource and "`49c184508`" in resource


def test_cited_current_controls_exist() -> None:
    for relative in ("tests/unit/web/test_body_size_limits.py", "tests/unit/web/test_mounted_body_rejection_order.py"):
        assert (REPO_ROOT / relative).is_file(), relative
