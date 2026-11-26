"""Upload size ceilings through the mounted app: the body guard, the upload route and the document ceiling.

The body guard runs before routing and refused every body over 10 MiB, so a
data file between 10 MiB and ``max_upload_bytes`` never reached the upload
route. Multipart uploads now take ``max_upload_bytes`` (plus the multipart
envelope). Binary documents and images keep their own 5 MiB Textract-inline
ceiling however they are declared. That ceiling deliberately does not move
with the data-file limit.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from elspeth.contracts.binary_documents import BINARY_DOCUMENT_MAX_BYTES, BINARY_DOCUMENT_MIME_BY_FORMAT
from elspeth.web.auth.middleware import get_current_user, require_pipeline_user
from tests.integration.web.conftest import _build_audit_readiness_app, _lifespan_test_client, _seed_session_without_state

_MIB = 1024 * 1024
_SIGNATURES = {"pdf": b"%PDF-1.7\n", "png": b"\x89PNG\r\n\x1a\n", "jpeg": b"\xff\xd8\xff\xe0"}


@pytest.fixture
def upload_client(tmp_path: Path):
    app = _build_audit_readiness_app(tmp_path, authed_user_id="uploader")
    app.dependency_overrides[require_pipeline_user] = app.dependency_overrides[get_current_user]
    with _lifespan_test_client(app) as client:
        session_id = _seed_session_without_state(client, user_id="uploader")
        yield client, f"/api/sessions/{session_id}/blobs", app.state.settings.max_upload_bytes


def _csv(size: int) -> bytes:
    head = b"id,text\n"
    row = b"1," + b"x" * 90 + b"\n"
    body = head + row * ((size - len(head)) // len(row))
    return body + b"z" * (size - len(body))


def test_a_data_file_over_the_old_10_mib_wall_is_stored_intact(upload_client) -> None:
    client, path, _ = upload_client
    data = _csv(11 * _MIB)

    created = client.post(path, files={"file": ("rows.csv", data, "text/csv")})

    assert created.status_code == 201, created.text
    stored = client.get(f"{path}/{created.json()['id']}/content").content
    assert hashlib.sha256(stored).digest() == hashlib.sha256(data).digest()


def test_a_data_file_over_max_upload_bytes_is_refused_by_the_route(upload_client) -> None:
    client, path, limit = upload_client

    refused = client.post(path, files={"file": ("rows.csv", _csv(limit + 1), "text/csv")})

    assert refused.status_code == 413
    assert refused.json() == {"detail": f"File exceeds maximum size of {limit} bytes"}


def test_requests_other_than_the_multipart_upload_keep_the_10_mib_ceiling(upload_client) -> None:
    client, path, _ = upload_client
    oversized = b'{"x":"' + b"a" * (10 * _MIB) + b'"}'

    for target in ("/api/sessions", f"{path}/inline"):
        refused = client.post(target, content=oversized, headers={"content-type": "application/json"})
        assert refused.status_code == 413, target
        assert refused.json() == {"error": "Request body too large (max 10 MB)"}


@pytest.mark.parametrize("fmt", sorted(_SIGNATURES))
@pytest.mark.parametrize("declared_as", ["own", "text/csv", "application/octet-stream"])
def test_documents_and_images_keep_the_5_mib_ceiling_however_declared(upload_client, fmt: str, declared_as: str) -> None:
    client, path, limit = upload_client
    assert BINARY_DOCUMENT_MAX_BYTES == 5 * _MIB
    size = 20 * _MIB
    assert BINARY_DOCUMENT_MAX_BYTES < size < limit, "the document must clear the data-file limit's guard to test its own ceiling"
    data = _SIGNATURES[fmt] + b"A" * (size - len(_SIGNATURES[fmt]))
    own = BINARY_DOCUMENT_MIME_BY_FORMAT[fmt]

    refused = client.post(path, files={"file": (f"doc.{fmt}", data, own if declared_as == "own" else declared_as)})

    if declared_as == "own":
        assert refused.status_code == 413
        assert refused.json() == {"detail": f"Binary document exceeds the {BINARY_DOCUMENT_MAX_BYTES}-byte Textract inline ceiling"}
    else:
        assert refused.status_code == 415
        assert f"matches binary format '{fmt}'" in refused.json()["detail"]
