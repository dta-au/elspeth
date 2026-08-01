from __future__ import annotations

import hashlib
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from elspeth.web.aws_rds_trust import (
    AWS_RDS_CA_CERTIFICATE_IDENTIFIER,
    AWS_RDS_GLOBAL_BUNDLE_CERTIFICATE_COUNT,
    AWS_RDS_GLOBAL_BUNDLE_PATH,
    AWS_RDS_GLOBAL_BUNDLE_SHA256,
    AwsRdsTrustBundleError,
    _verify_bundle_file,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_BUNDLE = REPO_ROOT / "deploy/aws-ecs/trust/global-bundle.pem"
SOURCE_CHECKSUM = REPO_ROOT / "deploy/aws-ecs/trust/global-bundle.pem.sha256"


def test_reviewed_bundle_matches_the_pinned_release_contract() -> None:
    data = SOURCE_BUNDLE.read_bytes()
    certificates = x509.load_pem_x509_certificates(data)

    assert hashlib.sha256(data).hexdigest() == AWS_RDS_GLOBAL_BUNDLE_SHA256
    assert SOURCE_CHECKSUM.read_text(encoding="ascii") == (f"{AWS_RDS_GLOBAL_BUNDLE_SHA256}  global-bundle.pem\n")
    assert len(certificates) == AWS_RDS_GLOBAL_BUNDLE_CERTIFICATE_COUNT == 108
    assert all(certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca for certificate in certificates)
    assert Path("/etc/elspeth/rds/global-bundle.pem") == AWS_RDS_GLOBAL_BUNDLE_PATH
    assert AWS_RDS_CA_CERTIFICATE_IDENTIFIER == "rds-ca-rsa2048-g1"


def _self_signed_certificate(*, ca: bool) -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "unit-test")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM)


def _verify_test_file(path: Path, *, count: int = 1):
    file_stat = path.stat()
    return _verify_bundle_file(
        path,
        expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        expected_certificate_count=count,
        expected_owner_uid=file_stat.st_uid,
        expected_mode=stat.S_IMODE(file_stat.st_mode),
    )


def test_verifier_returns_only_redacted_metadata_for_valid_bundle(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.pem"
    bundle.write_bytes(_self_signed_certificate(ca=True))
    report = _verify_test_file(bundle)
    assert report.actual_sha256 == hashlib.sha256(bundle.read_bytes()).hexdigest()
    assert report.certificate_count == 1
    assert report.path == str(bundle)
    assert "BEGIN CERTIFICATE" not in repr(report)


@pytest.mark.parametrize(
    ("content", "code"),
    [
        # Explicit ids keep collection deterministic: the generated certificates
        # differ per process, and xdist rejects workers whose auto-derived ids
        # (built from the parameter bytes) disagree.
        pytest.param(b"", "empty", id="empty"),
        pytest.param(b"-----BEGIN CERTIFICATE-----\nnot-base64\n-----END CERTIFICATE-----\n", "malformed_pem", id="malformed_pem"),
        pytest.param(_self_signed_certificate(ca=True) + b"not-whitespace", "trailing_data", id="trailing_data"),
        pytest.param(_self_signed_certificate(ca=False), "non_ca_certificate", id="non_ca_certificate"),
    ],
)
def test_verifier_rejects_invalid_certificate_content(
    tmp_path: Path,
    content: bytes,
    code: str,
) -> None:
    bundle = tmp_path / "bundle.pem"
    bundle.write_bytes(content)
    with pytest.raises(AwsRdsTrustBundleError) as caught:
        _verify_test_file(bundle)
    assert caught.value.code == code
    assert "BEGIN CERTIFICATE" not in repr(caught.value)
    assert "not-base64" not in repr(caught.value)


def test_verifier_refuses_symlink_without_reading_target(tmp_path: Path) -> None:
    target = tmp_path / "target.pem"
    target.write_bytes(_self_signed_certificate(ca=True))
    link = tmp_path / "bundle.pem"
    link.symlink_to(target)
    with pytest.raises(AwsRdsTrustBundleError) as caught:
        _verify_bundle_file(
            link,
            expected_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
            expected_certificate_count=1,
            expected_owner_uid=os.getuid(),
            expected_mode=0o600,
        )
    assert caught.value.code == "symlink"


def test_verifier_rejects_missing_file(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.pem"
    with pytest.raises(AwsRdsTrustBundleError) as caught:
        _verify_bundle_file(
            bundle,
            expected_sha256="0" * 64,
            expected_certificate_count=1,
            expected_owner_uid=os.getuid(),
            expected_mode=0o600,
        )
    assert caught.value.code == "missing"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_verifier_rejects_unreadable_file(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.pem"
    data = _self_signed_certificate(ca=True)
    bundle.write_bytes(data)
    file_stat = bundle.stat()
    expected_sha256 = hashlib.sha256(data).hexdigest()
    bundle.chmod(0o000)
    try:
        with pytest.raises(AwsRdsTrustBundleError) as caught:
            _verify_bundle_file(
                bundle,
                expected_sha256=expected_sha256,
                expected_certificate_count=1,
                expected_owner_uid=file_stat.st_uid,
                expected_mode=stat.S_IMODE(file_stat.st_mode),
            )
        assert caught.value.code == "unreadable"
    finally:
        bundle.chmod(0o600)


def test_verifier_rejects_non_regular_file(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.pem"
    bundle.mkdir()
    with pytest.raises(AwsRdsTrustBundleError) as caught:
        _verify_bundle_file(
            bundle,
            expected_sha256="0" * 64,
            expected_certificate_count=1,
            expected_owner_uid=os.getuid(),
            expected_mode=0o600,
        )
    assert caught.value.code == "non_regular"


def test_verifier_rejects_owner_mismatch(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.pem"
    bundle.write_bytes(_self_signed_certificate(ca=True))
    file_stat = bundle.stat()
    with pytest.raises(AwsRdsTrustBundleError) as caught:
        _verify_bundle_file(
            bundle,
            expected_sha256=hashlib.sha256(bundle.read_bytes()).hexdigest(),
            expected_certificate_count=1,
            expected_owner_uid=file_stat.st_uid + 1,
            expected_mode=stat.S_IMODE(file_stat.st_mode),
        )
    assert caught.value.code == "owner_mismatch"


def test_verifier_rejects_mode_mismatch(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.pem"
    bundle.write_bytes(_self_signed_certificate(ca=True))
    bundle.chmod(0o600)
    with pytest.raises(AwsRdsTrustBundleError) as caught:
        _verify_bundle_file(
            bundle,
            expected_sha256=hashlib.sha256(bundle.read_bytes()).hexdigest(),
            expected_certificate_count=1,
            expected_owner_uid=os.getuid(),
            expected_mode=0o400,
        )
    assert caught.value.code == "mode_mismatch"


def test_verifier_rejects_digest_mismatch(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.pem"
    bundle.write_bytes(_self_signed_certificate(ca=True))
    file_stat = bundle.stat()
    with pytest.raises(AwsRdsTrustBundleError) as caught:
        _verify_bundle_file(
            bundle,
            expected_sha256="0" * 64,
            expected_certificate_count=1,
            expected_owner_uid=file_stat.st_uid,
            expected_mode=stat.S_IMODE(file_stat.st_mode),
        )
    assert caught.value.code == "digest_mismatch"


def test_verifier_rejects_certificate_count_mismatch(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.pem"
    bundle.write_bytes(_self_signed_certificate(ca=True))
    with pytest.raises(AwsRdsTrustBundleError) as caught:
        _verify_test_file(bundle, count=2)
    assert caught.value.code == "certificate_count_mismatch"
