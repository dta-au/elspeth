from __future__ import annotations

import hashlib
from pathlib import Path

from cryptography import x509

from elspeth.web.aws_rds_trust import (
    AWS_RDS_CA_CERTIFICATE_IDENTIFIER,
    AWS_RDS_GLOBAL_BUNDLE_CERTIFICATE_COUNT,
    AWS_RDS_GLOBAL_BUNDLE_PATH,
    AWS_RDS_GLOBAL_BUNDLE_SHA256,
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
