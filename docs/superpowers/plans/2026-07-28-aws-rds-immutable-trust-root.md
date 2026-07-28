# AWS RDS Immutable Trust-Root Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace runtime RDS CA downloads with a reviewed trust root baked into the immutable ELSPETH image, enforce it before PostgreSQL admission, and qualify one new image digest for production RC use.

**Architecture:** A focused `elspeth.web.aws_rds_trust` module owns the canonical image path, pinned SHA-256, RDS CA identifier, secure file opening, X.509 parsing, and redacted verification result. Docker bakes the reviewed AWS commercial-region bundle into `/etc/elspeth/rds`; Terraform pins the Aurora instance CA, emits only `verify-full` URLs using that path, removes download code, and makes every ELSPETH container root filesystem read-only. Doctor and web startup share the verifier, while doctor also records `pg_stat_ssl` evidence for both logical databases before release admission.

**Tech Stack:** Python 3.13, `cryptography` 49, SQLAlchemy 2, psycopg/psycopg2, pytest, Docker/BuildKit, distroless Debian 13, Terraform 1.x with AWS provider 6.54, ECS Fargate, Aurora PostgreSQL 16.13, AWS CLI, Filigree.

---

## Execution Contract

- Work only in `/home/john/elspeth/.worktrees/aws-rds-trust-root-hardening`.
- Use branch `codex/aws-rds-trust-root-hardening`.
- Keep tracker bug `elspeth-ca436b5f1b` in `fixing`; it blocks parent
  `elspeth-671a17d5c0`.
- Preserve the separate dirty cold-install documentation worktree. Do not
  reset, clean, copy from, or edit
  `/home/john/elspeth/.worktrees/aws-cold-install-rc-280726`.
- Treat
  `sha256:c5e65357b7470cf1a702eeb084e865f0f5e0e43ab9741b76e872fa7568029700`
  as disqualified from final RC promotion.
- Do not move the final public RC tag until Task 11 passes in full.
- A failed live gate produces a new source commit, image digest, and complete
  rerun. Evidence from different attempts is never combined.

Run this preflight before Task 1:

```bash
cd /home/john/elspeth/.worktrees/aws-rds-trust-root-hardening
test "$(git branch --show-current)" = codex/aws-rds-trust-root-hardening
git merge-base --is-ancestor 173b22c8b HEAD
test -z "$(git status --porcelain)"
env -u VIRTUAL_ENV uv sync --frozen --all-extras
terraform -chdir=deploy/aws-ecs/terraform/scenario-a init \
  -backend=false -reconfigure -input=false
```

Expected: every command exits zero; the worktree is clean and the frozen local
environment is active only through `uv run`.

## File Map

### New files

- `deploy/aws-ecs/trust/rds-global-bundle.pem` — reviewed AWS public trust
  bundle bytes.
- `deploy/aws-ecs/trust/rds-global-bundle.pem.sha256` — exact offline checksum.
- `deploy/aws-ecs/trust/README.md` — source, retrieval, inventory, and rotation
  procedure.
- `src/elspeth/web/aws_rds_trust.py` — canonical constants and fail-closed
  verifier.
- `tests/unit/web/test_aws_rds_trust.py` — bundle supply-chain and verifier
  regressions.

### Modified files

- `.gitignore` — admit only the reviewed public bundle to Git.
- `.dockerignore` — admit only the reviewed public bundle to the Docker context.
- `pyproject.toml`, `uv.lock` — make `cryptography` a direct AWS dependency.
- `Dockerfile` — validate and install the trust root and publish OCI labels.
- `.github/workflows/build-push.yaml` — prove immutable trust material and
  read-only execution before publication and after pull-by-digest.
- `src/elspeth/web/deployment_contract.py` — accept only the canonical
  `verify-full` query.
- `src/elspeth/web/aws_ecs_startup.py` — verify trust material before any
  database work.
- `src/elspeth/web/doctor.py` — expose the trust check and TLS transport checks.
- `tests/unit/web/test_deployment_contract.py`,
  `tests/unit/web/test_aws_ecs_startup.py`, `tests/unit/web/test_doctor.py` —
  unit admission regressions.
- `deploy/aws-ecs/terraform/modules/scenario/locals.tf` — one canonical RDS
  path and CA identifier for generated infrastructure.
- `deploy/aws-ecs/terraform/modules/scenario/storage_identity.tf` — pin the
  Aurora CA and emit canonical URLs.
- `deploy/aws-ecs/terraform/modules/scenario/ecs.tf` — remove the startup
  download and make ELSPETH containers read-only.
- `deploy/aws-ecs/terraform/modules/scenario/database_bootstrap.tf` — use the
  image verifier and read-only root.
- `tests/unit/deployment/test_aws_ecs_terraform_package.py` — rendered package
  and no-fallback regressions.
- `tests/unit/test_build_push_release_checks.py` — Docker and publication
  invariants.
- `tests/testcontainer/web/conftest.py` — authenticated local TLS trust fixture.
- `tests/testcontainer/web/test_doctor_aws_ecs_postgres.py`,
  `tests/testcontainer/web/test_aws_ecs_validate_only_startup.py`,
  `tests/testcontainer/web/test_aws_ecs_readiness_postgres.py` — real
  PostgreSQL trust and TLS evidence.
- `deploy/aws-ecs/terraform/README.md`,
  `docs/runbooks/aws-ecs-deployment.md`,
  `tests/unit/web/test_aws_ecs_runbook_contract.py` — source-free production
  qualification instructions and guards.

### Deliberately unchanged

- Session and Landscape remain two logical PostgreSQL databases on one Aurora
  cluster.
- The CloudWatch Agent sidecar keeps its independent filesystem contract.
- No init container, EFS trust file, operating-system trust fallback, S3
  download, or Secrets Manager certificate payload is introduced.

## Task 1: Commit the Reviewed AWS Trust-Root Input

**Files:**

- Create: `deploy/aws-ecs/trust/rds-global-bundle.pem`
- Create: `deploy/aws-ecs/trust/rds-global-bundle.pem.sha256`
- Create: `deploy/aws-ecs/trust/README.md`
- Create: `src/elspeth/web/aws_rds_trust.py`
- Create: `tests/unit/web/test_aws_rds_trust.py`
- Modify: `.gitignore`
- Modify: `.dockerignore`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

- [ ] **Step 1: Write the failing supply-chain test**

Create `tests/unit/web/test_aws_rds_trust.py` with:

```python
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
SOURCE_BUNDLE = REPO_ROOT / "deploy/aws-ecs/trust/rds-global-bundle.pem"
SOURCE_CHECKSUM = REPO_ROOT / "deploy/aws-ecs/trust/rds-global-bundle.pem.sha256"


def test_reviewed_bundle_matches_the_pinned_release_contract() -> None:
    data = SOURCE_BUNDLE.read_bytes()
    certificates = x509.load_pem_x509_certificates(data)

    assert hashlib.sha256(data).hexdigest() == AWS_RDS_GLOBAL_BUNDLE_SHA256
    assert SOURCE_CHECKSUM.read_text(encoding="ascii") == (
        f"{AWS_RDS_GLOBAL_BUNDLE_SHA256}  rds-global-bundle.pem\n"
    )
    assert len(certificates) == AWS_RDS_GLOBAL_BUNDLE_CERTIFICATE_COUNT == 108
    assert all(
        certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        for certificate in certificates
    )
    assert AWS_RDS_GLOBAL_BUNDLE_PATH == Path("/etc/elspeth/rds/global-bundle.pem")
    assert AWS_RDS_CA_CERTIFICATE_IDENTIFIER == "rds-ca-rsa2048-g1"
```

- [ ] **Step 2: Run the test and verify that the source contract is absent**

Run:

```bash
env -u VIRTUAL_ENV uv run --frozen pytest -n0 -q \
  tests/unit/web/test_aws_rds_trust.py
```

Expected: collection fails because `elspeth.web.aws_rds_trust` and the reviewed
bundle do not exist.

- [ ] **Step 3: Retrieve the maintainer-reviewed bytes and pin their digest**

Run exactly:

```bash
mkdir -p deploy/aws-ecs/trust
curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location \
  --output deploy/aws-ecs/trust/rds-global-bundle.pem \
  https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
test "$(
  sha256sum deploy/aws-ecs/trust/rds-global-bundle.pem | cut -d' ' -f1
)" = e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3
test "$(
  rg -c '^-----BEGIN CERTIFICATE-----$' \
    deploy/aws-ecs/trust/rds-global-bundle.pem
)" = 108
```

Expected: both comparisons pass. If AWS has changed the bytes, stop and review
the new certificate inventory and design constants before changing this plan.

- [ ] **Step 4: Add checksum, provenance, constants, and direct dependency**

Create `deploy/aws-ecs/trust/rds-global-bundle.pem.sha256`:

```text
e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3  rds-global-bundle.pem
```

Create `deploy/aws-ecs/trust/README.md`:

```markdown
# AWS RDS commercial-region trust root

Source: `https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem`
Retrieved: 2026-07-28 UTC
SHA-256: `e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3`
Certificate count: 108

ELSPETH qualifies Aurora PostgreSQL in the commercial AWS partition with
`rds-ca-rsa2048-g1`. In `ap-southeast-1`, AWS RDS reported SHA-1 thumbprint
`aa6b73726c21176cc714815278d6ebc932f293b3`, valid from 2021-05-21 through
2061-05-21.

This file is updated only by a reviewed maintainer change. Build and runtime
must not download it. To rotate it, retrieve the official bytes into a review
worktree, inspect every X.509 certificate, update the checksum and application
constants in the same commit, build a new image digest, and repeat the complete
source-free AWS production qualification before moving a release tag.
```

Create the initial `src/elspeth/web/aws_rds_trust.py`:

```python
"""Immutable AWS RDS trust-root contract."""

from __future__ import annotations

from pathlib import Path

AWS_RDS_GLOBAL_BUNDLE_PATH = Path("/etc/elspeth/rds/global-bundle.pem")
AWS_RDS_GLOBAL_BUNDLE_SHA256 = (
    "e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3"
)
AWS_RDS_GLOBAL_BUNDLE_CERTIFICATE_COUNT = 108
AWS_RDS_GLOBAL_BUNDLE_OWNER_UID = 0
AWS_RDS_GLOBAL_BUNDLE_MODE = 0o444
AWS_RDS_CA_CERTIFICATE_IDENTIFIER = "rds-ca-rsa2048-g1"
```

Add `"cryptography>=49,<50"` to the `aws = [...]` optional-dependency list in
`pyproject.toml`. Regenerate only lock metadata:

```bash
env -u VIRTUAL_ENV uv lock --offline
env -u VIRTUAL_ENV uv sync --frozen --all-extras
```

After the last `*.pem` rule in `.gitignore`, add:

```gitignore
!deploy/aws-ecs/trust/rds-global-bundle.pem
```

After `*.pem` in `.dockerignore`, add:

```dockerignore
!deploy/aws-ecs/trust/rds-global-bundle.pem
```

- [ ] **Step 5: Run the supply-chain test**

Run:

```bash
env -u VIRTUAL_ENV uv run --frozen pytest -n0 -q \
  tests/unit/web/test_aws_rds_trust.py
```

Expected: `1 passed`.

- [ ] **Step 6: Commit the reviewed input**

```bash
git add .gitignore .dockerignore pyproject.toml uv.lock \
  deploy/aws-ecs/trust \
  src/elspeth/web/aws_rds_trust.py \
  tests/unit/web/test_aws_rds_trust.py
git commit -m "build: pin the AWS RDS trust root"
```

## Task 2: Implement the Fail-Closed Trust Verifier

**Files:**

- Modify: `src/elspeth/web/aws_rds_trust.py`
- Modify: `tests/unit/web/test_aws_rds_trust.py`

- [ ] **Step 1: Add failing verifier tests**

Append tests that cover success, missing files, symlinks, non-regular files,
ownership, mode, digest, malformed PEM, trailing bytes, empty input, non-CA
certificates, and certificate count. Use this helper for generated
certificates:

```python
import os
import stat
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from elspeth.web.aws_rds_trust import (
    AwsRdsTrustBundleError,
    _verify_bundle_file,
)


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
        (b"", "empty"),
        (b"-----BEGIN CERTIFICATE-----\nnot-base64\n-----END CERTIFICATE-----\n", "malformed_pem"),
        (_self_signed_certificate(ca=True) + b"not-whitespace", "trailing_data"),
        (_self_signed_certificate(ca=False), "non_ca_certificate"),
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
```

Add separate assertions for:

```python
assert error.code == "missing"
assert error.code == "non_regular"
assert error.code == "owner_mismatch"
assert error.code == "mode_mismatch"
assert error.code == "digest_mismatch"
assert error.code == "certificate_count_mismatch"
```

- [ ] **Step 2: Run the verifier tests and verify they fail**

Run:

```bash
env -u VIRTUAL_ENV uv run --frozen pytest -n0 -q \
  tests/unit/web/test_aws_rds_trust.py
```

Expected: failures because `AwsRdsTrustBundleError` and
`_verify_bundle_file` are not defined.

- [ ] **Step 3: Replace the constants-only module with the complete verifier**

Implement `src/elspeth/web/aws_rds_trust.py` with these public types and
functions:

```python
"""Immutable AWS RDS trust-root contract."""

from __future__ import annotations

import errno
import hashlib
import hmac
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cryptography import x509

AWS_RDS_GLOBAL_BUNDLE_PATH = Path("/etc/elspeth/rds/global-bundle.pem")
AWS_RDS_GLOBAL_BUNDLE_SHA256 = (
    "e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3"
)
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
            basic_constraints = certificate.extensions.get_extension_for_class(
                x509.BasicConstraints
            ).value
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
```

- [ ] **Step 4: Run verifier tests and static checks**

Run:

```bash
env -u VIRTUAL_ENV uv run --frozen pytest -n0 -q \
  tests/unit/web/test_aws_rds_trust.py
env -u VIRTUAL_ENV uv run --frozen ruff check \
  src/elspeth/web/aws_rds_trust.py tests/unit/web/test_aws_rds_trust.py
env -u VIRTUAL_ENV uv run --frozen mypy src/elspeth/web/aws_rds_trust.py
```

Expected: every command passes.

- [ ] **Step 5: Commit the verifier**

```bash
git add src/elspeth/web/aws_rds_trust.py \
  tests/unit/web/test_aws_rds_trust.py
git commit -m "feat(web): verify the immutable AWS RDS trust root"
```

## Task 3: Bake and Prove the Trust Root in the Release Image

**Files:**

- Modify: `Dockerfile`
- Modify: `.github/workflows/build-push.yaml`
- Modify: `tests/unit/test_build_push_release_checks.py`

- [ ] **Step 1: Add failing Docker and publication contract tests**

Append:

```python
RDS_BUNDLE_SHA256 = "e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3"


def test_release_image_bakes_the_reviewed_rds_trust_root() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    dockerignore = DOCKERIGNORE.read_text(encoding="utf-8")

    assert "!deploy/aws-ecs/trust/rds-global-bundle.pem" in dockerignore
    assert "COPY deploy/aws-ecs/trust/rds-global-bundle.pem " in dockerfile
    assert "/runtime-root/etc/elspeth/rds/global-bundle.pem" in dockerfile
    assert "chmod 0444" in dockerfile
    assert RDS_BUNDLE_SHA256 in dockerfile
    assert 'LABEL io.elspeth.rds-ca-bundle-sha256="$RDS_CA_BUNDLE_SHA256"' in dockerfile
    assert 'LABEL io.elspeth.rds-ca-certificate-identifier="rds-ca-rsa2048-g1"' in dockerfile


def test_release_workflow_verifies_trust_root_under_read_only_rootfs() -> None:
    job = _build_push_job()
    lean = _step_run(job, "Verify lean PostgreSQL image contract")
    generic = _step_run(_job("smoke-test"), "Verify generic image runtime contract")

    for script in (lean, generic):
        assert "io.elspeth.rds-ca-bundle-sha256" in script
        assert RDS_BUNDLE_SHA256 in script
        assert "verify_aws_rds_trust_bundle" in script
        assert "docker run --rm --read-only" in script
```

- [ ] **Step 2: Run the tests and verify they fail**

```bash
env -u VIRTUAL_ENV uv run --frozen pytest -n0 -q \
  tests/unit/test_build_push_release_checks.py
```

Expected: the new Docker and workflow assertions fail.

- [ ] **Step 3: Install and label the asset in `Dockerfile`**

Add a global build argument:

```dockerfile
ARG RDS_CA_BUNDLE_SHA256="e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3"
```

Redeclare it in the builder stage. Before the existing runtime-root preparation
`RUN`, add:

```dockerfile
ARG RDS_CA_BUNDLE_SHA256
COPY deploy/aws-ecs/trust/rds-global-bundle.pem /runtime-root/etc/elspeth/rds/global-bundle.pem
COPY deploy/aws-ecs/trust/rds-global-bundle.pem.sha256 /runtime-root/etc/elspeth/rds/global-bundle.pem.sha256
RUN test "$(sha256sum /runtime-root/etc/elspeth/rds/global-bundle.pem | cut -d' ' -f1)" = "$RDS_CA_BUNDLE_SHA256" && \
    chown -R 0:0 /runtime-root/etc/elspeth && \
    find /runtime-root/etc/elspeth -type d -exec chmod 0755 {} + && \
    find /runtime-root/etc/elspeth -type f -exec chmod 0444 {} +
```

Redeclare the argument in the runtime stage and add labels:

```dockerfile
ARG RDS_CA_BUNDLE_SHA256
LABEL io.elspeth.rds-ca-bundle-sha256="$RDS_CA_BUNDLE_SHA256"
LABEL io.elspeth.rds-ca-certificate-identifier="rds-ca-rsa2048-g1"
```

- [ ] **Step 4: Extend both workflow image proofs**

In the lean pre-publication step and the pulled-digest smoke step, assert the
two OCI labels and run:

```bash
test "$(
  docker inspect --format \
    '{{ index .Config.Labels "io.elspeth.rds-ca-bundle-sha256" }}' \
    "$image"
)" = e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3
test "$(
  docker inspect --format \
    '{{ index .Config.Labels "io.elspeth.rds-ca-certificate-identifier" }}' \
    "$image"
)" = rds-ca-rsa2048-g1
docker run --rm --read-only --entrypoint python "$image" -c \
  'from elspeth.web.aws_rds_trust import verify_aws_rds_trust_bundle; report = verify_aws_rds_trust_bundle(); assert report.certificate_count == 108'
docker run --rm --read-only --entrypoint /bin/sh "$image" -c \
  'test "$(stat -c "%u:%g:%a" /etc/elspeth/rds/global-bundle.pem)" = "0:0:444" &&
   test "$(stat -c "%u:%g:%a" /etc/elspeth/rds/global-bundle.pem.sha256)" = "0:0:444" &&
   cd /etc/elspeth/rds &&
   sha256sum -c global-bundle.pem.sha256'
```

For the lean step, set `image="$LEAN_IMAGE"` before the assertions. Keep these
checks before registry login and push.

- [ ] **Step 5: Run unit tests and build the exact lean image**

```bash
env -u VIRTUAL_ENV uv run --frozen pytest -n0 -q \
  tests/unit/test_build_push_release_checks.py
docker build --load \
  --build-arg INSTALL_EXTRAS="webui llm aws postgres" \
  --tag elspeth-rds-trust-local:plan .
docker run --rm --read-only --entrypoint python \
  elspeth-rds-trust-local:plan -c \
  'from elspeth.web.aws_rds_trust import verify_aws_rds_trust_bundle; print(verify_aws_rds_trust_bundle())'
docker run --rm --read-only --entrypoint /bin/sh \
  elspeth-rds-trust-local:plan -c \
  'test "$(stat -c "%u:%g:%a" /etc/elspeth/rds/global-bundle.pem)" = "0:0:444" &&
   test "$(stat -c "%u:%g:%a" /etc/elspeth/rds/global-bundle.pem.sha256)" = "0:0:444" &&
   cd /etc/elspeth/rds &&
   sha256sum -c global-bundle.pem.sha256'
```

Expected: tests pass, the image builds, verification reports 108 certificates,
and both container runs succeed with a read-only root filesystem.

- [ ] **Step 6: Commit image construction and publication gates**

```bash
git add Dockerfile .github/workflows/build-push.yaml \
  tests/unit/test_build_push_release_checks.py
git commit -m "build: bake and verify the RDS trust root"
```

## Task 4: Enforce the Canonical PostgreSQL URL Contract

**Files:**

- Modify: `src/elspeth/web/deployment_contract.py`
- Modify: `tests/unit/web/test_deployment_contract.py`
- Modify: `tests/unit/web/test_aws_ecs_startup.py`
- Modify: `tests/unit/web/test_doctor.py`

- [ ] **Step 1: Change AWS test URLs to the canonical path and add negatives**

Import `AWS_RDS_GLOBAL_BUNDLE_PATH` in the three test modules and set:

```python
_AWS_TLS_QUERY = (
    f"sslmode=verify-full&sslrootcert={AWS_RDS_GLOBAL_BUNDLE_PATH}"
)
```

Add this regression to `tests/unit/web/test_deployment_contract.py`:

```python
@pytest.mark.parametrize(
    "alternate",
    [
        "system",
        "/var/lib/elspeth/rds-global-bundle.pem",
        "/tmp/rds-global-bundle.pem",
        "/etc/ssl/certs/ca-certificates.crt",
    ],
)
def test_aws_ecs_rejects_every_noncanonical_trust_root(alternate: str) -> None:
    settings = _external_settings(
        Path("/tmp"),
        target="aws-ecs",
        session_db_url=(
            "postgresql+psycopg://user:password@db/session"
            f"?sslmode=verify-full&sslrootcert={alternate}"
        ),
    )
    check = {
        item.name: item for item in validate_aws_ecs_settings(settings)
    }["session_db_url"]
    assert check.ok is False
    assert alternate not in check.detail
```

Retain and update the existing duplicate-mode and duplicate-root tests so tuple
query values remain rejected.

- [ ] **Step 2: Run the URL tests and verify alternate paths still pass**

```bash
env -u VIRTUAL_ENV uv run --frozen pytest -n0 -q \
  tests/unit/web/test_deployment_contract.py \
  tests/unit/web/test_aws_ecs_startup.py \
  tests/unit/web/test_doctor.py
```

Expected: new alternate-path cases fail because the validator accepts any
nonblank root.

- [ ] **Step 3: Require the exact canonical query**

Import the module, not copied constants:

```python
from elspeth.web import aws_rds_trust
```

Replace `_has_approved_aws_ecs_tls_query` with:

```python
def _has_approved_aws_ecs_tls_query(parsed: URL) -> bool:
    sslmode = parsed.query.get("sslmode")
    sslrootcert = parsed.query.get("sslrootcert")
    return (
        type(sslmode) is str
        and sslmode == "verify-full"
        and type(sslrootcert) is str
        and sslrootcert == str(aws_rds_trust.AWS_RDS_GLOBAL_BUNDLE_PATH)
    )
```

Change the static failure detail to:

```python
f"{env_var} must require authenticated PostgreSQL TLS with "
"sslmode=verify-full and the immutable ELSPETH AWS RDS trust root"
```

- [ ] **Step 4: Run the URL and current startup/doctor unit suites**

Use the Step 2 command.

Expected: all selected tests pass.

- [ ] **Step 5: Commit the canonical URL contract**

```bash
git add src/elspeth/web/deployment_contract.py \
  tests/unit/web/test_deployment_contract.py \
  tests/unit/web/test_aws_ecs_startup.py \
  tests/unit/web/test_doctor.py
git commit -m "fix(web): require the immutable RDS trust path"
```

## Task 5: Gate Doctor and Web Startup on Trust Verification

**Files:**

- Modify: `src/elspeth/web/aws_ecs_startup.py`
- Modify: `src/elspeth/web/doctor.py`
- Modify: `tests/unit/web/test_aws_ecs_startup.py`
- Modify: `tests/unit/web/test_doctor.py`

- [ ] **Step 1: Add failing startup-order and redaction tests**

In `test_aws_ecs_startup.py`, add an autouse success fixture:

```python
@pytest.fixture(autouse=True)
def _verified_trust_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        startup.aws_rds_trust,
        "verify_aws_rds_trust_bundle",
        lambda: startup.aws_rds_trust.AwsRdsTrustBundleReport(
            path=str(startup.aws_rds_trust.AWS_RDS_GLOBAL_BUNDLE_PATH),
            expected_sha256=startup.aws_rds_trust.AWS_RDS_GLOBAL_BUNDLE_SHA256,
            actual_sha256=startup.aws_rds_trust.AWS_RDS_GLOBAL_BUNDLE_SHA256,
            certificate_count=108,
        ),
    )
```

Add:

```python
def test_trust_root_failure_precedes_settings_validation_and_database_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        startup.aws_rds_trust,
        "verify_aws_rds_trust_bundle",
        lambda: (_ for _ in ()).throw(
            startup.aws_rds_trust.AwsRdsTrustBundleError(
                "digest_mismatch",
                actual_sha256="f" * 64,
            )
        ),
    )
    monkeypatch.setattr(
        startup,
        "validate_aws_ecs_settings",
        lambda *_args, **_kwargs: pytest.fail("settings validation must not run"),
    )

    with pytest.raises(startup.AwsEcsStartupContractError) as caught:
        startup.enforce_aws_ecs_contract(_settings(tmp_path))

    assert "trust root" in str(caught.value)
    assert "digest_mismatch" in str(caught.value)
    _assert_redacted(caught.value)
```

- [ ] **Step 2: Add failing doctor trust-root tests**

Patch a green trust check in the existing doctor auxiliary helper:

```python
monkeypatch.setattr(
    doctor,
    "_aws_rds_trust_root_check",
    lambda: ContractCheck("rds_trust_root", True, "immutable RDS trust root verified"),
)
```

Add:

```python
def test_failed_trust_root_blocks_every_database_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import elspeth.web.doctor as doctor

    events: list[str] = []
    _patch_database_states(
        monkeypatch,
        SchemaState.CURRENT,
        SchemaState.CURRENT,
        events,
    )
    monkeypatch.setattr(
        doctor,
        "_aws_rds_trust_root_check",
        lambda: ContractCheck(
            "rds_trust_root",
            False,
            "immutable RDS trust root verification failed (digest_mismatch)",
        ),
    )

    checks = _by_name(collect_checks(_settings(tmp_path)))

    assert checks["rds_trust_root"].ok is False
    assert checks["session_schema"].ok is False
    assert checks["landscape_schema"].ok is False
    assert events == []
```

- [ ] **Step 3: Run the two unit modules and verify failures**

```bash
env -u VIRTUAL_ENV uv run --frozen pytest -n0 -q \
  tests/unit/web/test_aws_ecs_startup.py \
  tests/unit/web/test_doctor.py
```

Expected: the new trust-root tests fail because neither admission path calls
the verifier.

- [ ] **Step 4: Verify at web startup before static validation**

In `aws_ecs_startup.py`, add:

```python
from elspeth.web import aws_rds_trust
```

At the beginning of `enforce_aws_ecs_contract`, add:

```python
try:
    aws_rds_trust.verify_aws_rds_trust_bundle()
except aws_rds_trust.AwsRdsTrustBundleError as exc:
    raise _contract_error(
        "AWS ECS immutable RDS trust root failed verification "
        f"({exc.code})."
    ) from None
```

- [ ] **Step 5: Add the doctor check and make it a database prerequisite**

In `doctor.py`, import `aws_rds_trust` and add:

```python
def _aws_rds_trust_root_check() -> ContractCheck:
    try:
        report = aws_rds_trust.verify_aws_rds_trust_bundle()
    except aws_rds_trust.AwsRdsTrustBundleError as exc:
        actual = (
            f", actual_sha256={exc.actual_sha256}"
            if exc.actual_sha256 is not None
            else ""
        )
        return ContractCheck(
            "rds_trust_root",
            False,
            "immutable RDS trust root verification failed "
            f"({exc.code}, path={aws_rds_trust.AWS_RDS_GLOBAL_BUNDLE_PATH}, "
            f"expected_sha256={aws_rds_trust.AWS_RDS_GLOBAL_BUNDLE_SHA256}"
            f"{actual})",
        )
    return ContractCheck(
        "rds_trust_root",
        True,
        "immutable RDS trust root verified "
        f"(path={report.path}, sha256={report.actual_sha256}, "
        f"certificates={report.certificate_count})",
    )
```

In `_collect_deployment_checks`, append this check only for AWS, rebuild
`by_name` after it is appended, and require `by_name["rds_trust_root"].ok` in
`database_prerequisites_pass`.

- [ ] **Step 6: Run focused admission tests**

```bash
env -u VIRTUAL_ENV uv run --frozen pytest -n0 -q \
  tests/unit/web/test_aws_rds_trust.py \
  tests/unit/web/test_deployment_contract.py \
  tests/unit/web/test_aws_ecs_startup.py \
  tests/unit/web/test_doctor.py
```

Expected: all selected tests pass, including failure-before-database assertions.

- [ ] **Step 7: Commit admission integration**

```bash
git add src/elspeth/web/aws_ecs_startup.py src/elspeth/web/doctor.py \
  tests/unit/web/test_aws_ecs_startup.py tests/unit/web/test_doctor.py
git commit -m "fix(web): verify RDS trust before database admission"
```

## Task 6: Record PostgreSQL TLS Evidence for Both Databases

**Files:**

- Modify: `src/elspeth/web/doctor.py`
- Modify: `tests/unit/web/test_doctor.py`

- [ ] **Step 1: Add failing `pg_stat_ssl` tests**

Add:

```python
@pytest.mark.parametrize(
    ("label", "row", "ok"),
    [
        ("session_schema", (True, "TLSv1.3", 256), True),
        ("landscape_schema", (True, "TLSv1.2", 256), True),
        ("session_schema", (False, None, None), False),
        ("landscape_schema", None, False),
    ],
)
def test_postgres_tls_check_is_named_redacted_and_fail_closed(
    label: str,
    row: tuple[object, ...] | None,
    ok: bool,
) -> None:
    connection = MagicMock(spec_set=Connection)
    connection.execute.return_value.one_or_none.return_value = row

    check = postgres_tls_check(label, connection)

    expected_name = "session_tls" if label == "session_schema" else "landscape_tls"
    assert check.name == expected_name
    assert check.ok is ok
    assert "pg_stat_ssl" not in check.detail
    assert str(connection.execute.call_args.args[0]) == (
        "SELECT ssl, version, bits FROM pg_catalog.pg_stat_ssl "
        "WHERE pid = pg_backend_pid()"
    )
```

Extend the `_inspect_database` test to assert an AWS inspection queries TLS on
the same connection before the schema probe and returns a third named check.

- [ ] **Step 2: Run the doctor tests and verify they fail**

```bash
env -u VIRTUAL_ENV uv run --frozen pytest -n0 -q \
  tests/unit/web/test_doctor.py
```

Expected: failures because `postgres_tls_check` and the third inspection result
do not exist.

- [ ] **Step 3: Implement the transport check**

Add:

```python
def postgres_tls_check(label: str, connection: Connection) -> ContractCheck:
    name = "session_tls" if label == "session_schema" else "landscape_tls"
    row = connection.execute(
        text(
            "SELECT ssl, version, bits FROM pg_catalog.pg_stat_ssl "
            "WHERE pid = pg_backend_pid()"
        )
    ).one_or_none()
    ok = (
        row is not None
        and row[0] is True
        and isinstance(row[1], str)
        and row[1].startswith("TLSv")
        and isinstance(row[2], int)
        and row[2] >= 128
    )
    if not ok:
        return ContractCheck(name, False, "authenticated PostgreSQL TLS is not active")
    return ContractCheck(
        name,
        True,
        f"authenticated PostgreSQL TLS is active ({row[1]}, {row[2]} bits)",
    )
```

Import `postgres_tls_check` in the test module. Change `_inspect_database` to
accept `require_authenticated_tls: bool` and return:

```python
tuple[SchemaState | None, ContractCheck, ContractCheck | None]
```

Use this connection body:

```python
with engine.connect() as connection:
    tls_result: ContractCheck | None = None
    if require_authenticated_tls:
        tls_result = postgres_tls_check(label, connection)
        if not tls_result.ok:
            result = (
                None,
                ContractCheck(
                    label,
                    False,
                    f"{_human_schema_label(label)} inspection was blocked "
                    "because authenticated PostgreSQL TLS was not active",
                ),
                tls_result,
            )
        else:
            state = probe_fn(connection)
            result = (state, schema_check(label, state), tls_result)
    else:
        connection.execute(text("SELECT 1"))
        state = probe_fn(connection)
        result = (state, schema_check(label, state), None)
```

The existing exception branches return the same static schema failure plus a
static failed TLS check when `require_authenticated_tls` is true. Preserve the
existing one-shot engine disposal in `finally`; a disposal failure replaces
the schema result while retaining the already collected TLS result.

Update `_collect_deployment_checks` to:

- pass `require_authenticated_tls=include_aws_checks`;
- emit `session_tls` and `landscape_tls` before schema results for AWS;
- emit static blocked TLS checks when AWS prerequisites fail; and
- include both TLS checks in the complete preflight before initialization.

- [ ] **Step 4: Update existing doctor test doubles to the three-value result**

Change `_patch_database_states` so its replacement accepts the keyword and
returns:

```python
tls_check = (
    ContractCheck(
        "session_tls" if label == "session_schema" else "landscape_tls",
        True,
        "authenticated PostgreSQL TLS is active (TLSv1.3, 256 bits)",
    )
    if require_authenticated_tls
    else None
)
return state, schema_check(label, state), tls_check
```

Update ordered check-name assertions to include:

```python
"rds_trust_root",
"session_tls",
"landscape_tls",
```

- [ ] **Step 5: Run the doctor and CLI unit tests**

```bash
env -u VIRTUAL_ENV uv run --frozen pytest -n0 -q \
  tests/unit/web/test_doctor.py \
  tests/unit/cli/test_doctor_command.py
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit TLS evidence**

```bash
git add src/elspeth/web/doctor.py tests/unit/web/test_doctor.py
git commit -m "feat(web): report authenticated PostgreSQL TLS"
```

## Task 7: Harden the Terraform-Generated Runtime

**Files:**

- Modify: `deploy/aws-ecs/terraform/modules/scenario/locals.tf`
- Modify: `deploy/aws-ecs/terraform/modules/scenario/storage_identity.tf`
- Modify: `deploy/aws-ecs/terraform/modules/scenario/ecs.tf`
- Modify: `deploy/aws-ecs/terraform/modules/scenario/database_bootstrap.tf`
- Modify: `tests/unit/deployment/test_aws_ecs_terraform_package.py`

- [ ] **Step 1: Replace the old-positive test with fail-closed assertions**

Update `test_database_topology_is_aurora_with_separate_databases_and_roles`:

```python
canonical_root = "/etc/elspeth/rds/global-bundle.pem"
assert storage.count("sslmode=verify-full") == 5
assert storage.count(f"sslrootcert=${{local.rds_ca_bundle_path}}") == 5
assert canonical_root in _text("modules/scenario/locals.tf")
assert 'rds_ca_identifier = "rds-ca-rsa2048-g1"' in _text(
    "modules/scenario/locals.tf"
)
assert re.search(
    r"ca_cert_identifier\s+=\s+local\.rds_ca_identifier",
    storage,
)
assert "truststore.pki.rds.amazonaws.com" not in _all_text()
assert "urllib.request" not in _all_text()
assert "/tmp/rds-global-bundle.pem" not in _all_text()
assert "${local.data_dir}/rds-global-bundle.pem" not in _all_text()
```

Add:

```python
def test_every_elspeth_container_has_read_only_root_filesystem() -> None:
    ecs = _text("modules/scenario/ecs.tf")
    bootstrap = _text("modules/scenario/database_bootstrap.tf")
    assert ecs.count("readonlyRootFilesystem = true") == 5
    assert "readonlyRootFilesystem = true" in bootstrap
    assert "readonlyRootFilesystem" not in ecs[
        ecs.index("cloudwatch_agent_container = {"):
        ecs.index("candidate_web_container = {")
    ]


def test_database_bootstrap_uses_the_image_trust_verifier() -> None:
    bootstrap = _text("modules/scenario/database_bootstrap.tf")
    assert "verify_aws_rds_trust_bundle" in bootstrap
    assert "urllib.request" not in bootstrap
    assert "Path(" not in bootstrap
```

- [ ] **Step 2: Run the package test and verify it fails**

```bash
env -u VIRTUAL_ENV uv run --frozen pytest -n0 -q \
  tests/unit/deployment/test_aws_ecs_terraform_package.py
```

Expected: trust-download, URL, CA, and read-only assertions fail.

- [ ] **Step 3: Add canonical Terraform locals and pin Aurora**

In `locals.tf` add:

```hcl
rds_ca_bundle_path = "/etc/elspeth/rds/global-bundle.pem"
rds_ca_identifier  = "rds-ca-rsa2048-g1"
```

In `aws_rds_cluster_instance.database` add:

```hcl
ca_cert_identifier = local.rds_ca_identifier
```

Change all five Secrets Manager URL queries to:

```hcl
?sslmode=verify-full&sslrootcert=${local.rds_ca_bundle_path}
```

- [ ] **Step 4: Remove downloads and make application containers read-only**

Delete the `rds_ca=...` block from `local.ecs_identity_wrapper`. Keep metadata
lookup, telemetry export, CLI normalization, and `exec`.

Add this exact property to:

- `candidate_web_container`;
- `schema_init_doctor_container`;
- `runtime_doctor_container`;
- `payload_container`; and
- `local_auth_container`.

```hcl
readonlyRootFilesystem = true
```

Rollback web and doctor containers inherit the property through their existing
`merge(...)` calls. Do not add it to `cloudwatch_agent_container`.

In `database_bootstrap_container`, add the same property. Replace the bootstrap
imports and first operation with:

```python
from elspeth.web.aws_rds_trust import verify_aws_rds_trust_bundle

verify_aws_rds_trust_bundle()
```

Delete `urllib.request`, `Path`, the public URL, and the `/tmp` write.

- [ ] **Step 5: Format and run Python plus native Terraform tests**

```bash
terraform fmt -recursive -check deploy/aws-ecs/terraform
terraform -chdir=deploy/aws-ecs/terraform/scenario-a init \
  -backend=false -reconfigure -input=false
terraform -chdir=deploy/aws-ecs/terraform/scenario-a test \
  -filter=codeblind.tftest.hcl -no-color
env -u VIRTUAL_ENV uv run --frozen pytest -n0 -q \
  tests/unit/deployment/test_aws_ecs_terraform_package.py
```

Expected: formatting is clean, the native mocked plan passes, and the Python
package contract passes.

- [ ] **Step 6: Prove candidate and rollback inheritance in source**

Extend `test_every_elspeth_container_has_read_only_root_filesystem` with:

```python
assert "rollback_web_container = merge(local.candidate_web_container" in ecs
assert "rollback_doctor_container = merge(local.runtime_doctor_container" in ecs
```

Run:

```bash
env -u VIRTUAL_ENV uv run --frozen pytest -n0 -q \
  tests/unit/deployment/test_aws_ecs_terraform_package.py::test_every_elspeth_container_has_read_only_root_filesystem
```

Expected: the five base ELSPETH definitions and database bootstrap are
read-only, rollback web/doctor inherit from the read-only bases, and the
CloudWatch Agent remains outside the assertion. Task 11 verifies the rendered
live definitions.

- [ ] **Step 7: Commit Terraform hardening**

```bash
git add deploy/aws-ecs/terraform/modules/scenario \
  tests/unit/deployment/test_aws_ecs_terraform_package.py
git commit -m "fix(deploy): remove mutable RDS trust bootstrap"
```

## Task 8: Prove the Contract Against Real TLS PostgreSQL

**Files:**

- Modify: `tests/testcontainer/web/conftest.py`
- Modify: `tests/testcontainer/web/test_doctor_aws_ecs_postgres.py`
- Modify: `tests/testcontainer/web/test_aws_ecs_validate_only_startup.py`
- Modify: `tests/testcontainer/web/test_aws_ecs_readiness_postgres.py`

- [ ] **Step 1: Make the test CA explicitly a CA**

Add this OpenSSL argument pair in the shared certificate command:

```python
"-addext",
"basicConstraints=critical,CA:TRUE",
```

- [ ] **Step 2: Add a function-scoped test trust override**

Add imports:

```python
import hashlib
import stat
from pathlib import Path

from elspeth.web import aws_rds_trust
```

Add:

```python
@pytest.fixture
def aws_rds_trust_test_override(
    external_deployment_postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed = make_url(external_deployment_postgres_url)
    root = parsed.query["sslrootcert"]
    assert isinstance(root, str)
    path = Path(root)
    file_stat = path.stat()
    monkeypatch.setattr(aws_rds_trust, "AWS_RDS_GLOBAL_BUNDLE_PATH", path)
    monkeypatch.setattr(
        aws_rds_trust,
        "AWS_RDS_GLOBAL_BUNDLE_SHA256",
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(
        aws_rds_trust,
        "AWS_RDS_GLOBAL_BUNDLE_CERTIFICATE_COUNT",
        1,
    )
    monkeypatch.setattr(
        aws_rds_trust,
        "AWS_RDS_GLOBAL_BUNDLE_OWNER_UID",
        file_stat.st_uid,
    )
    monkeypatch.setattr(
        aws_rds_trust,
        "AWS_RDS_GLOBAL_BUNDLE_MODE",
        stat.S_IMODE(file_stat.st_mode),
    )
```

This remains test-only: production callers cannot supply a path or digest.

- [ ] **Step 3: Apply the fixture to all AWS PostgreSQL modules**

Use:

```python
pytestmark = [
    pytest.mark.testcontainer,
    pytest.mark.usefixtures("aws_rds_trust_test_override"),
]
```

in the three AWS testcontainer modules.

- [ ] **Step 4: Assert doctor reports both trust and transport**

After parsing doctor JSON, assert:

```python
checks = {item["name"]: item for item in report}
assert checks["rds_trust_root"]["ok"] is True
assert checks["session_tls"]["ok"] is True
assert checks["landscape_tls"]["ok"] is True
assert "TLSv" in checks["session_tls"]["detail"]
assert "TLSv" in checks["landscape_tls"]["detail"]
```

Keep all existing redaction and concurrent-initialization assertions.

- [ ] **Step 5: Run the sequential real-PostgreSQL suite**

```bash
CI=1 env -u VIRTUAL_ENV uv run --frozen pytest -q -n 0 \
  -m testcontainer \
  tests/testcontainer/web/test_schema_probe_postgres.py \
  tests/testcontainer/web/test_external_deployment_postgres.py \
  tests/testcontainer/web/test_aws_ecs_validate_only_startup.py \
  tests/testcontainer/web/test_doctor_aws_ecs_postgres.py \
  tests/testcontainer/web/test_aws_ecs_readiness_postgres.py \
  tests/testcontainer/web/test_landscape_write_gate_postgres.py
```

Expected: the complete sequential web PostgreSQL suite passes. Doctor evidence
contains trust, session TLS, and Landscape TLS without URLs or credentials.

- [ ] **Step 6: Commit real PostgreSQL proof**

```bash
git add tests/testcontainer/web
git commit -m "test(web): prove immutable trust over PostgreSQL TLS"
```

## Task 9: Update Source-Free Production Qualification Instructions

**Files:**

- Modify: `deploy/aws-ecs/terraform/README.md`
- Modify: `docs/runbooks/aws-ecs-deployment.md`
- Modify: `tests/unit/web/test_aws_ecs_runbook_contract.py`

- [ ] **Step 1: Add failing documentation contract tests**

Add assertions:

```python
def test_runbook_requires_immutable_rds_trust_before_release_promotion() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "/etc/elspeth/rds/global-bundle.pem" in text
    assert "e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3" in text
    assert "rds-ca-rsa2048-g1" in text
    assert "readonlyRootFilesystem" in text
    assert "session_tls" in text
    assert "landscape_tls" in text
    assert "c5e65357b7470cf1a702eeb084e865f0f5e0e43ab9741b76e872fa7568029700" in text
    assert text.index("session_tls") < text.index("0.7.2-RC-280726")
    assert text.index("landscape_tls") < text.index("0.7.2-RC-280726")
```

Add a Terraform README contract asserting the same path, CA identifier,
read-only field, and two doctor TLS checks.

- [ ] **Step 2: Run the runbook tests and verify they fail**

```bash
env -u VIRTUAL_ENV uv run --frozen pytest -n0 -q \
  tests/unit/web/test_aws_ecs_runbook_contract.py
```

Expected: the new release-admission assertions fail.

- [ ] **Step 3: Add the source-free immutable trust section**

Add this operational contract to both deployment documents:

```markdown
## Immutable RDS trust-root admission

The image must contain `/etc/elspeth/rds/global-bundle.pem` with SHA-256
`e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3`.
Its OCI CA label must be `rds-ca-rsa2048-g1`. Every ELSPETH container in the
task definitions must set `readonlyRootFilesystem` to `true`.

The schema and runtime doctor JSON must report all of these checks as green
before the web service is enabled:

- `rds_trust_root`
- `session_tls`
- `landscape_tls`
- `session_schema`
- `landscape_schema`

The task definitions and bootstrap must not contain
`truststore.pki.rds.amazonaws.com`, `/tmp/rds-global-bundle.pem`, or
`/var/lib/elspeth/rds-global-bundle.pem`.

OCI digest
`sha256:c5e65357b7470cf1a702eeb084e865f0f5e0e43ab9741b76e872fa7568029700`
predates this contract. It is an acceptance-attempt artifact and is not
eligible for `0.7.2-RC-280726`.
```

Add exact inspection commands:

```bash
docker buildx imagetools inspect "$CANDIDATE_IMAGE"
test "$(docker inspect --format \
  '{{ index .Config.Labels "io.elspeth.rds-ca-bundle-sha256" }}' \
  "$CANDIDATE_IMAGE")" = \
  e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3
test "$(docker inspect --format \
  '{{ index .Config.Labels "io.elspeth.rds-ca-certificate-identifier" }}' \
  "$CANDIDATE_IMAGE")" = rds-ca-rsa2048-g1

aws --profile "$AWS_PROFILE" --region "$AWS_REGION" rds \
  describe-db-instances \
  --db-instance-identifier "$DB_INSTANCE_IDENTIFIER" \
  --query 'DBInstances[0].CACertificateIdentifier' \
  --output text | grep -Fx rds-ca-rsa2048-g1

aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecs \
  describe-task-definition \
  --task-definition "$TASK_DEFINITION" \
  --query 'taskDefinition.containerDefinitions[?name!=`cloudwatch-agent`].readonlyRootFilesystem' \
  --output json | jq -e 'length > 0 and all(.[]; . == true)'
```

The existing schema/runtime task execution remains authoritative for obtaining
doctor JSON and checking the three trust/TLS names before service enablement.
The final tag command remains after those checks and teardown proof.

- [ ] **Step 4: Run documentation contracts**

```bash
env -u VIRTUAL_ENV uv run --frozen pytest -n0 -q \
  tests/unit/web/test_aws_ecs_runbook_contract.py \
  tests/unit/deployment/test_aws_ecs_terraform_package.py
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit source-free instructions**

```bash
git add deploy/aws-ecs/terraform/README.md \
  docs/runbooks/aws-ecs-deployment.md \
  tests/unit/web/test_aws_ecs_runbook_contract.py
git commit -m "docs: require immutable RDS trust for AWS release"
```

## Task 10: Run the Complete Local Release Gate

**Files:**

- No new files.
- Fix any failure in its owning source or test file, rerun the narrow
  regression, commit the fix, then restart this task from Step 1.

- [ ] **Step 1: Prove a clean, fully pinned tree**

```bash
test -z "$(git status --porcelain)"
env -u VIRTUAL_ENV uv sync --frozen --all-extras
env -u VIRTUAL_ENV uv lock --check
git diff --check
```

Expected: all commands pass and the worktree remains clean.

- [ ] **Step 2: Run static and repository contract gates**

```bash
env -u VIRTUAL_ENV uv run --frozen ruff check \
  src/ tests/ scripts/ examples/ elspeth-lints/src/
env -u VIRTUAL_ENV uv run --frozen ruff format --check \
  src/ tests/ scripts/ examples/ elspeth-lints/src/
env -u VIRTUAL_ENV uv run --frozen mypy src/ elspeth-lints/src/
env -u VIRTUAL_ENV uv run --frozen python scripts/check_contracts.py
env -u VIRTUAL_ENV uv run --frozen python \
  scripts/cicd/check_slot_type_cross_language.py
env -u VIRTUAL_ENV uv run --frozen python \
  scripts/cicd/generate_skill_inventory.py --check
```

Expected: every static and contract gate passes.

- [ ] **Step 3: Run the complete non-special pytest lane**

```bash
CI=1 env -u VIRTUAL_ENV uv run --frozen pytest tests/ -v \
  -m "not slow and not stress and not performance and not testcontainer"
```

Expected: all selected tests pass with no collection or warning-policy failure.

- [ ] **Step 4: Run the complete testcontainer lane**

```bash
CI=1 env -u VIRTUAL_ENV uv run --frozen pytest tests/testcontainer/ \
  -v -m testcontainer
```

Expected: all testcontainer tests pass, including the sequential AWS
PostgreSQL trust and TLS checks.

- [ ] **Step 5: Run Terraform gates from a clean initialization**

```bash
terraform fmt -recursive -check deploy/aws-ecs/terraform
terraform -chdir=deploy/aws-ecs/terraform/scenario-a init \
  -backend=false -reconfigure -input=false
terraform -chdir=deploy/aws-ecs/terraform/scenario-a validate -no-color
terraform -chdir=deploy/aws-ecs/terraform/scenario-a test \
  -filter=codeblind.tftest.hcl -no-color
```

Expected: format, validation, and native test all pass.

- [ ] **Step 6: Build and smoke the exact release-profile image**

```bash
release_sha=$(git rev-parse HEAD)
release_image="elspeth-rds-trust:${release_sha}"
docker build --load \
  --build-arg INSTALL_EXTRAS="webui llm aws postgres" \
  --tag "$release_image" .
test "$(docker inspect --format \
  '{{ index .Config.Labels "io.elspeth.rds-ca-bundle-sha256" }}' \
  "$release_image")" = \
  e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3
docker run --rm --read-only --entrypoint python "$release_image" -c \
  'from elspeth.web.aws_rds_trust import verify_aws_rds_trust_bundle; assert verify_aws_rds_trust_bundle().certificate_count == 108'
docker run --rm --read-only "$release_image" --version
docker run --rm --read-only "$release_image" --help
```

Expected: build and every read-only smoke pass.

- [ ] **Step 7: Run all pre-commit checks**

```bash
env -u VIRTUAL_ENV uv run --frozen pre-commit run --all-files
```

Expected: every hook passes and `git status --porcelain` is empty.

## Task 11: Execute Source-Free Live AWS Production Qualification

**Files:**

- No source changes unless a gate fails.
- Use the existing sanitized acceptance evidence store and runbook. Do not
  invent a new plan receipt, approval sidecar, or document-signing process.

- [ ] **Step 1: Freeze one exact candidate**

Record:

```bash
candidate_sha=$(git rev-parse HEAD)
test -z "$(git status --porcelain)"
git show --no-patch --format='%H %T %P' "$candidate_sha"
```

Build and publish the exact lean image by immutable digest through the existing
trusted release-image path. Record `candidate_sha`, OCI digest, install-extras
label, bundle digest label, and CA identifier label. Assert the OCI digest is
not:

```text
sha256:c5e65357b7470cf1a702eeb084e865f0f5e0e43ab9741b76e872fa7568029700
```

- [ ] **Step 2: Start a unique disposable qualification run**

Follow `docs/runbooks/aws-ecs-deployment.md` from identity/account/region
preflight through bootstrap using:

```text
AWS profile: elspeth-acceptance
AWS region: ap-southeast-1
Purpose: ELSPETH 0.7.2 immutable RDS trust-root production qualification
Candidate: the exact candidate_sha and OCI digest from Step 1
Cleanup deadline: no later than four hours after creation
```

Use a new UUID run ID and unique state keys. Do not reuse prior Terraform state,
ECS task definitions, EFS, Aurora, secrets, or image tags.

- [ ] **Step 3: Perform the source-free cold install**

Give the installer only:

- the published Terraform deployment artefact;
- the immutable candidate image digest;
- required AWS account, region, profile, IAM-boundary, backend, model, and
  cleanup inputs;
- `deploy/aws-ecs/terraform/README.md`; and
- official AWS documentation.

Do not provide the ELSPETH source checkout or unpublished operator knowledge.
Run bootstrap, Scenario A init/plan/apply, database bootstrap, schema doctor,
runtime doctor, service enablement, readiness, durable EFS checks, local or
configured auth, and authenticated Composer-to-Bedrock proof exactly as
documented.

- [ ] **Step 4: Prove the production trust and filesystem contract**

Require all of these facts from live AWS and doctor output:

```text
RDS CACertificateIdentifier: rds-ca-rsa2048-g1
Doctor rds_trust_root: true
Doctor session_tls: true
Doctor landscape_tls: true
Doctor session_schema: true
Doctor landscape_schema: true
ELSPETH readonlyRootFilesystem: true
Task image: exact immutable candidate digest
Session database: elspeth_session
Landscape database: elspeth_landscape
```

Inspect every live Scenario A ELSPETH task definition, including database
bootstrap, schema doctor, runtime doctor, payload/local-auth qualification, and
candidate web. Confirm no definition or command contains:

```text
truststore.pki.rds.amazonaws.com
/tmp/rds-global-bundle.pem
/var/lib/elspeth/rds-global-bundle.pem
```

The Terraform source and native tests prove rollback web/doctor definitions
inherit the same contract. Do not use the candidate digest as its own rollback
baseline and call that rollback evidence. A live rollback claim requires a
different, independently qualified digest that already contains the immutable
trust root. If no such predecessor exists for 0.7.2, record live rollback as
unavailable and leave any broader parent rollback gate open; this cold-install
qualification must not manufacture one.

- [ ] **Step 5: Complete application production evidence**

Require successful:

- schema initialization and runtime-only doctor;
- `/api/health` and `/api/ready`;
- session and Landscape writes through their separate runtime targets;
- durable EFS payload/blob behavior;
- authenticated web use;
- Composer-to-Bedrock invocation through the ECS task role;
- CloudWatch/X-Ray operator telemetry expected by the existing runbook; and
- no credential, URL, model-content, certificate-body, or private-path leakage
  in retained evidence.

- [ ] **Step 6: Destroy the complete run and prove zero leftovers**

Execute the runbook teardown in its required order. Confirm Terraform state,
ECS services/tasks, task definitions owned by the run, Aurora, EFS, ALB,
Secrets Manager secrets, CloudWatch resources, Cognito resources when present,
S3 state objects/buckets, ECR qualification tags, IAM roles/policies/boundary,
and every other run-tagged billable resource are absent.

Do not proceed while any run-owned resource remains or teardown evidence is
incomplete.

- [ ] **Step 7: Promote only the qualified digest**

After Steps 1–6 are green, assign:

```text
ghcr.io/johnm-dta/elspeth:0.7.2-RC-280726
```

to the single qualified digest. Inspect the remote manifest after promotion and
assert it resolves to that digest. Assert the disqualified attempt digest
remains untagged by the final RC name.

If any earlier step failed, do not run this step.

## Task 12: Close the Release Blocker with Exact Evidence

**Files:**

- No source changes.

- [ ] **Step 1: Re-audit the final source and artifact identity**

```bash
git status --short --branch
git log --oneline 57b7e9c5c..HEAD
git diff --check 57b7e9c5c..HEAD
```

Expected: clean worktree, only intended commits, no whitespace errors.

- [ ] **Step 2: Comment the complete evidence on `elspeth-ca436b5f1b`**

The comment must include:

- final source commit;
- qualified OCI digest;
- bundle SHA-256 and certificate count;
- RDS CA identifier;
- focused, full, testcontainer, Terraform, Docker, and pre-commit outcomes;
- doctor trust/TLS/schema outcomes for both logical databases;
- task-definition read-only proof;
- source-free installer outcome;
- Bedrock outcome;
- teardown outcome;
- final RC tag resolution; and
- explicit confirmation that the old attempt digest was not promoted.

- [ ] **Step 3: Close the child only after production qualification**

```bash
filigree close elspeth-ca436b5f1b \
  --actor codex-rds-trust-root
```

Expected: the bug is terminal with the final implementation commit and
qualification evidence. If live qualification is incomplete, leave it in
`fixing`.

- [ ] **Step 4: Update the parent coordinator issue**

Add a concise comment to `elspeth-671a17d5c0` stating whether the immutable
trust-root blocker is closed, the exact qualified digest and tag, and whether
any other parent gate remains. Do not close the parent issue on behalf of its
current coordinator.
