"""Fresh bounded credential inspection of each complete attachment original."""

import hashlib
import hmac
import json
from dataclasses import dataclass

from elspeth.contracts.credential_material import (
    CREDENTIAL_DETECTOR_VERSION,
    DEFAULT_CREDENTIAL_TRAVERSAL_POLICY,
)
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.json_parser import check_json_depth, parse_json_strict
from elspeth.web.credential_guard import require_no_credential_material


class AttachmentInspectionRefused(ValueError):
    """The original exceeds the operator's bounded inspection resource."""


@dataclass(frozen=True, slots=True)
class AttachmentInspectionPolicy:
    max_bytes: int
    env_ref_names: frozenset[str] = frozenset()
    additional_credential_fields: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if type(self.max_bytes) is not int or self.max_bytes <= 0:
            raise ValueError("Attachment inspection requires a positive exact byte limit")
        for names in (self.env_ref_names, self.additional_credential_fields):
            if type(names) is not frozenset or any(type(name) is not str or not name for name in names):
                raise ValueError("Attachment inspection reference policy requires exact nonempty strings")

    @property
    def fingerprint(self) -> str:
        traversal = DEFAULT_CREDENTIAL_TRAVERSAL_POLICY
        value = {
            "schema_version": 1,
            "content_interpretation": "complete-original-strict-utf-8-and-strict-json-fields",
            "max_bytes": self.max_bytes,
            "max_string_bytes": self.max_bytes,
            "max_depth": traversal.max_depth,
            "max_nodes": traversal.max_nodes,
            "max_width": traversal.max_width,
            "env_ref_names": sorted(self.env_ref_names),
            "additional_credential_fields": sorted(self.additional_credential_fields),
        }
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AttachmentReadAdmission:
    """Identity/history of this inspection only; never reusable clearance."""

    session_id: str
    blob_id: str
    content_hash: str
    size_bytes: int
    detector_version: str
    policy_fingerprint: str


def require_attachment_size_within_policy(size_bytes: int, policy: AttachmentInspectionPolicy) -> None:
    if size_bytes > policy.max_bytes:
        raise AttachmentInspectionRefused(
            f"Attachment exceeds the {policy.max_bytes}-byte complete-original inspection policy; no content was returned."
        )


def inspect_attachment_original(
    *,
    session_id: str,
    blob_id: str,
    verified_hash: str,
    raw: bytes,
    policy: AttachmentInspectionPolicy,
) -> AttachmentReadAdmission:
    if type(policy) is not AttachmentInspectionPolicy or type(raw) is not bytes:
        raise TypeError("Attachment inspection requires owned policy and exact immutable bytes")
    if type(session_id) is not str or not session_id or type(blob_id) is not str or not blob_id:
        raise AuditIntegrityError("Attachment inspection requires actual session/blob identities")
    actual_hash = hashlib.sha256(raw).hexdigest()
    if type(verified_hash) is not str or not hmac.compare_digest(actual_hash, verified_hash):
        raise AuditIntegrityError("Attachment inspection does not match verified bytes")
    require_attachment_size_within_policy(len(raw), policy)
    content = raw.decode("utf-8")
    require_no_credential_material(
        content,
        surface="composer_attachment_original",
        env_ref_names=policy.env_ref_names,
        additional_credential_fields=policy.additional_credential_fields,
        max_string_bytes=policy.max_bytes,
    )
    # Text is always inspected above. JSON supplies actual field semantics in
    # addition: declared secret slots and escaped keys cannot hide in a string.
    json_shaped = content.lstrip().startswith(("{", "[", '"'))
    if json_shaped:
        try:
            check_json_depth(content, max_depth=DEFAULT_CREDENTIAL_TRAVERSAL_POLICY.max_depth)
        except ValueError as error:
            raise AttachmentInspectionRefused("Attachment JSON exceeds the complete inspection depth") from error
    structured, parse_error = parse_json_strict(content)
    if parse_error is None:
        try:
            json.dumps(structured, allow_nan=False)
        except ValueError as error:
            raise AttachmentInspectionRefused("Attachment JSON contains a nonfinite value") from error
        require_no_credential_material(
            structured,
            surface="composer_attachment_original_json",
            env_ref_names=policy.env_ref_names,
            additional_credential_fields=policy.additional_credential_fields,
            max_string_bytes=policy.max_bytes,
        )
    elif json_shaped:
        raise AttachmentInspectionRefused("Attachment JSON is not a complete strict value; no content was returned")
    return AttachmentReadAdmission(
        session_id=session_id,
        blob_id=blob_id,
        content_hash=verified_hash,
        size_bytes=len(raw),
        detector_version=CREDENTIAL_DETECTOR_VERSION,
        policy_fingerprint=policy.fingerprint,
    )
