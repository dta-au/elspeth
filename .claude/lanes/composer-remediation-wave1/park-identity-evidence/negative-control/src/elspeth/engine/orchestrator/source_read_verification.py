"""Fixed canonical response policy for the reviewed Power Automate source."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from elspeth.contracts.call_data import HTTPDecodedBodyEvidence
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import check_json_depth, parse_json_strict
from elspeth.contracts.source_read_verification import MAX_JSON_DEPTH, CanonicalJSONSourceReadCapability, SourceReadVerificationPolicy

if TYPE_CHECKING:
    from elspeth.contracts import SourceProtocol


def source_read_verification_policy(source: object) -> SourceReadVerificationPolicy | None:
    """Select the nominal source policy issued by reviewed L3 composition."""
    if not isinstance(source, CanonicalJSONSourceReadCapability):
        return None
    if type(source.source_read_verification_policy) is not SourceReadVerificationPolicy:
        raise AuditIntegrityError("Canonical source verification requires the reviewed builtin source policy binding")
    return source.source_read_verification_policy


def source_load_input_data(source: SourceProtocol) -> dict[str, object]:
    """Retain the selected policy in ordinary operation input evidence."""
    data: dict[str, object] = {"source_plugin": source.name}
    policy = source_read_verification_policy(source)
    if policy is not None:
        data["source_read_verification_policy"] = policy.value
    if "snapshot_for_resume" in source.config and source.config["snapshot_for_resume"] is True:
        data["snapshot_for_resume"] = True
    return data


def canonical_source_read_response(response: Mapping[str, Any] | None, *, max_body_bytes: int) -> Mapping[str, object]:
    """Project status, JSON media type, redirects and the entire decoded JSON.

    Archive data is parsed independently of the client's parsed-body sidecar.
    Closed diagnostics contain no response bytes or parser exception text.
    """
    if response is None:
        raise AuditIntegrityError("Invalid canonical source response evidence")
    data = response["decoded_body"] if "decoded_body" in response else None
    if not isinstance(data, Mapping) or set(data) != {"body_b64", "decoded_size", "complete"}:
        raise AuditIntegrityError("Invalid canonical source response decoded evidence")
    try:
        evidence = HTTPDecodedBodyEvidence(body_b64=data["body_b64"], decoded_size=data["decoded_size"], complete=data["complete"])
    except (TypeError, ValueError):
        raise AuditIntegrityError("Invalid canonical source response decoded evidence") from None
    if not evidence.complete or evidence.decoded_size > max_body_bytes:
        raise AuditIntegrityError("Invalid canonical source response decoded evidence")
    status = response["status_code"] if "status_code" in response else None
    redirects = response["redirect_count"] if "redirect_count" in response else 0
    if type(status) is not int or not 100 <= status <= 999 or type(redirects) is not int or redirects != 0:
        raise AuditIntegrityError("Invalid canonical source response status or redirects")
    headers = response["headers"] if "headers" in response else None
    if not isinstance(headers, Mapping) or any(type(name) is not str or type(value) is not str for name, value in headers.items()):
        raise AuditIntegrityError("Invalid canonical source response headers")
    media = [value for name, value in headers.items() if name.lower() == "content-type"]
    if len(media) != 1 or media[0].split(";", 1)[0].strip().lower() != "application/json":
        raise AuditIntegrityError("Invalid canonical source response media type")
    try:
        text = base64.b64decode(evidence.body_b64, validate=True).decode("utf-8")
        check_json_depth(text, max_depth=MAX_JSON_DEPTH)
        parsed, error = parse_json_strict(text)
        if error is not None:
            raise ValueError("invalid JSON")
        canonical = canonical_json(parsed)
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise AuditIntegrityError("Invalid canonical source response JSON evidence") from None
    return {"status_code": status, "media_type": "application/json", "redirect_count": redirects, "canonical_body": canonical}
