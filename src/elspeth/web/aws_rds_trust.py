"""Immutable AWS RDS trust-root contract."""

from __future__ import annotations

import errno
import hashlib
import hmac
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from cryptography import x509

AWS_RDS_GLOBAL_BUNDLE_PATH = Path("/etc/elspeth/rds/global-bundle.pem")
AWS_RDS_GLOBAL_BUNDLE_SHA256 = "e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3"
AWS_RDS_GLOBAL_BUNDLE_CERTIFICATE_COUNT = 108
AWS_RDS_GLOBAL_BUNDLE_OWNER_UID = 0
AWS_RDS_GLOBAL_BUNDLE_MODE = 0o444
AWS_RDS_CA_CERTIFICATE_IDENTIFIER = "rds-ca-rsa2048-g1"

_BEGIN_CERTIFICATE = b"-----BEGIN CERTIFICATE-----"
_END_CERTIFICATE = b"-----END CERTIFICATE-----"
_PEM_WHITESPACE = b" \t\r\n"

RdsTrustErrorCode = Literal[
    "missing",
    "symlink",
    "unreadable",
    "non_regular",
    "owner_mismatch",
    "mode_mismatch",
    "digest_mismatch",
    "empty",
    "malformed_pem",
    "trailing_data",
    "non_ca_certificate",
    "certificate_count_mismatch",
]


class AwsRdsTrustBundleError(RuntimeError):
    """Static, redacted trust-root failure."""

    def __init__(
        self,
        code: RdsTrustErrorCode,
        *,
        actual_sha256: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.actual_sha256 = actual_sha256


@dataclass(frozen=True, slots=True)
class AwsRdsTrustBundleReport:
    path: str
    expected_sha256: str
    actual_sha256: str
    certificate_count: int


def _parse_ca_certificates(data: bytes) -> tuple[x509.Certificate, ...]:
    # Lazy import: deployment_contract imports this module for its constants
    # on every deployment target, and cryptography ships only with the aws
    # extra. Constants must stay importable without cryptography installed.
    from cryptography import x509

    if not data.strip():
        raise AwsRdsTrustBundleError("empty")

    certificates: list[x509.Certificate] = []
    cursor = 0
    while cursor < len(data):
        while cursor < len(data) and data[cursor] in _PEM_WHITESPACE:
            cursor += 1
        if cursor == len(data):
            break
        if not data.startswith(_BEGIN_CERTIFICATE, cursor):
            raise AwsRdsTrustBundleError("trailing_data")
        end = data.find(_END_CERTIFICATE, cursor + len(_BEGIN_CERTIFICATE))
        if end < 0:
            raise AwsRdsTrustBundleError("malformed_pem")
        block_end = end + len(_END_CERTIFICATE)
        block = data[cursor:block_end]
        try:
            certificate = x509.load_pem_x509_certificate(block)
            basic_constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
        except (ValueError, x509.ExtensionNotFound):
            raise AwsRdsTrustBundleError("malformed_pem") from None
        if not basic_constraints.ca:
            raise AwsRdsTrustBundleError("non_ca_certificate")
        certificates.append(certificate)
        cursor = block_end

    if not certificates:
        raise AwsRdsTrustBundleError("empty")
    return tuple(certificates)


def _verify_bundle_file(
    path: Path,
    *,
    expected_sha256: str,
    expected_certificate_count: int,
    expected_owner_uid: int,
    expected_mode: int,
) -> AwsRdsTrustBundleReport:
    fd: int | None = None
    try:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise AwsRdsTrustBundleError("missing") from None
        except OSError as exc:
            code: RdsTrustErrorCode = "symlink" if exc.errno == errno.ELOOP else "unreadable"
            raise AwsRdsTrustBundleError(code) from None

        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise AwsRdsTrustBundleError("non_regular")
        if file_stat.st_uid != expected_owner_uid:
            raise AwsRdsTrustBundleError("owner_mismatch")
        if stat.S_IMODE(file_stat.st_mode) != expected_mode:
            raise AwsRdsTrustBundleError("mode_mismatch")

        with os.fdopen(fd, "rb", closefd=True) as bundle:
            fd = None
            data = bundle.read()
    except AwsRdsTrustBundleError:
        raise
    except OSError:
        raise AwsRdsTrustBundleError("unreadable") from None
    finally:
        if fd is not None:
            os.close(fd)

    actual_sha256 = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        raise AwsRdsTrustBundleError(
            "digest_mismatch",
            actual_sha256=actual_sha256,
        )
    certificates = _parse_ca_certificates(data)
    if len(certificates) != expected_certificate_count:
        raise AwsRdsTrustBundleError(
            "certificate_count_mismatch",
            actual_sha256=actual_sha256,
        )
    return AwsRdsTrustBundleReport(
        path=str(path),
        expected_sha256=expected_sha256,
        actual_sha256=actual_sha256,
        certificate_count=len(certificates),
    )


def verify_aws_rds_trust_bundle() -> AwsRdsTrustBundleReport:
    """Verify only the canonical immutable image trust root."""
    return _verify_bundle_file(
        AWS_RDS_GLOBAL_BUNDLE_PATH,
        expected_sha256=AWS_RDS_GLOBAL_BUNDLE_SHA256,
        expected_certificate_count=AWS_RDS_GLOBAL_BUNDLE_CERTIFICATE_COUNT,
        expected_owner_uid=AWS_RDS_GLOBAL_BUNDLE_OWNER_UID,
        expected_mode=AWS_RDS_GLOBAL_BUNDLE_MODE,
    )
