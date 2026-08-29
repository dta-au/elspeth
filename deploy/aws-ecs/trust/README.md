# AWS RDS commercial-region trust root

Source: `https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem`
Retrieved: 2026-07-28 UTC
SHA-256: `e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3`
Certificate count: 108

ELSPETH qualifies Aurora PostgreSQL in the commercial AWS partition with
`rds-ca-rsa2048-g1`. In `ap-southeast-1`, AWS RDS reported SHA-1 thumbprint
`aa6b73726c21176cc714815278d6ebc932f293b3`, valid from 2021-05-21 through
2061-05-21.

## Rotation and emergency distrust

- RDS server-certificate rotation under the unchanged selected root does not
  require an ELSPETH image change.
- A change to the selected RDS root or the AWS global bundle requires a new
  immutable image and complete source-free AWS production qualification.
- An AWS emergency distrust notice makes every affected image digest
  immediately ineligible. Deploy a newly qualified image digest.

Never patch trust material on EFS, patch a running task through ECS Exec, or
download a CA bundle at runtime.

This file is updated only by a reviewed maintainer change. Build and runtime
must not download it. To rotate it, retrieve the official bytes into a review
worktree, inspect every X.509 certificate, update **every** site where the
SHA-256 (or certificate count) appears — all in the same commit:

- `src/elspeth/web/aws_rds_trust.py` (`AWS_RDS_GLOBAL_BUNDLE_SHA256`,
  `AWS_RDS_GLOBAL_BUNDLE_CERTIFICATE_COUNT`)
- `deploy/aws-ecs/trust/global-bundle.pem.sha256` (the sidecar)
- `Dockerfile` (the `RDS_CA_BUNDLE_SHA256` build-argument default)
- `tests/unit/test_build_push_release_checks.py` (`RDS_BUNDLE_SHA256`)
- `docs/runbooks/aws-ecs-deployment.md` (admission section)
- `deploy/aws-ecs/terraform/README.md` (admission section)
- `tests/unit/web/test_aws_ecs_runbook_contract.py` (documentation contract)
- the active implementation-plan document, if one is in flight

`.github/workflows/build-push.yaml` reads the expected digest from the
committed sidecar and needs no edit for that value, but its acceptance job
hardcodes the certificate count (108) and the CA identifier
(`rds-ca-rsa2048-g1`) in two places (build and push jobs) and both must be
updated to match. Then build a new image digest and repeat the complete
source-free AWS production qualification before moving a release tag.
