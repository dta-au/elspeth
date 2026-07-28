"""Immutable AWS RDS trust-root contract."""

from __future__ import annotations

from pathlib import Path

AWS_RDS_GLOBAL_BUNDLE_PATH = Path("/etc/elspeth/rds/global-bundle.pem")
AWS_RDS_GLOBAL_BUNDLE_SHA256 = "e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3"
AWS_RDS_GLOBAL_BUNDLE_CERTIFICATE_COUNT = 108
AWS_RDS_GLOBAL_BUNDLE_OWNER_UID = 0
AWS_RDS_GLOBAL_BUNDLE_MODE = 0o444
AWS_RDS_CA_CERTIFICATE_IDENTIFIER = "rds-ca-rsa2048-g1"
