---
title: Add a plan-time guard against deploying an image incompatible with the Terraform package
labels: [area/deployment, type/task]
---

The AWS ECS Terraform package ships environment-variable names that must exist in the
container image being deployed. Nothing checks that before the deployment runs, so a mismatch
is silent at apply time and fatal at boot. This covers the half of the guard that is buildable
and testable without an AWS account; publishing an image is not.

## Background

ELSPETH ships a Terraform package that deploys the web service to AWS ECS. The module under
`deploy/aws-ecs/terraform/modules/scenario/` configures the service entirely through
`ELSPETH_WEB__*` environment variables, which the container parses at boot.

## The incompatibility

`settings_from_env` in `src/elspeth/web/config.py` raises
`RuntimeError("Unknown ELSPETH_WEB__ setting: …")` (`config.py:1546`) for any name absent from
`WebSettings.model_fields`, and `WebSettings` is declared `extra="forbid"` — so an unrecognised
name is a boot failure, not a warning. The scenario module ships about fifty distinct
`ELSPETH_WEB__*` names in three syntactic shapes: `runtime_environment` entries and bare
`plugin_policy_projection` keys in `modules/scenario/locals.tf`, and export lines in the ECS
identity wrapper in `modules/scenario/ecs.tf`. The contract is that the shipped names must be a
subset of the `WebSettings` compiled into the image being deployed.

## What already exists

`tests/unit/deployment/test_aws_ecs_terraform_package.py` asserts the shipped names are a
subset of `WebSettings` at the current tip — package-versus-tip, not package-versus-image, so
it says nothing about the image an operator actually deploys. Commit `bd1be4aab` added a
computed floor test proving the documented minimum revision is exactly right. All image
admission today runs through `terraform_data` plus `local-exec` in
`modules/scenario/image_provenance.tf`, which is apply time, not plan time. The module carries
four `precondition` blocks (in `iam_observability.tf`, `ecs.tf` twice, and
`storage_identity.tf`) and none of them touches an image.

## Where to start

`deploy/aws-ecs/terraform/modules/scenario/image_provenance.tf` is where image admission lives
and where the new preconditions would go; `tests/unit/deployment/` is where the offline proof
goes. `tests/unit/deployment/test_aws_iam_policy_oracles.py` is the pattern to copy — tests
that hold a committed expected value and assert the package still produces it.

**Size.** Multi-day, and it has a decision embedded in it that is not the implementer's to
make (see *Sequencing* below). The offline half — record schema, tests, digest-resolution
helper — is self-contained and can be finished without an AWS account or any credentials. It
also needs someone comfortable reading Terraform, not only Python.

## Proposed shape

A committed record — schema, shipped names, minimum revision, approved image rows — read with
`jsondecode` by plan-time preconditions in `image_provenance.tf`, with offline tests in the
committed-expectation style above.

Two traps the design must avoid:

- **Pin the amd64 child manifest digest, never the multi-arch index.** An image published for
  several CPU architectures has one index digest pointing at one child manifest per
  architecture, so pinning the index does not identify the bytes that actually run.
  `deploy/aws-ecs/terraform/examples/scenario-a.tfvars` already pins the child and says why.
- **`candidate_sha` is a git commit SHA while `candidate_image` is a registry digest
  reference.** These two variables have been conflated before.

## Sequencing constraint

A membership precondition over an approved-image record fails closed on an empty record: it
rejects what it cannot prove, so an empty record blocks `terraform plan` entirely — in an
environment where nobody can publish an image to populate the record, that makes the package
unusable. Land the offline-provable pieces first and gate the preconditions themselves on an
explicit decision from the maintainer.

Do not seed a digest nobody can verify. The shipped example pins `sha256:8adff75e…` as the
amd64 child, and confirming that against the registry needs `aws ecr batch-get-image` in an
AWS account that was removed on 2026-08-10.

## A companion contradiction to resolve

Two documented helpers disagree on digest shape. `resolve_scannable_digest`
(`docs/runbooks/aws-ecs-cold-install.md:512`) passes a plain child manifest through unchanged.
`resolve_target_ecr_digest` (`deploy/aws-ecs/terraform/README.md:597`, reproduced in the
runbook and pinned by `test_aws_ecs_terraform_package.py`) restricts accepted media types to
index/list and aborts with `expected one parent index` (`README.md:619`) when handed a child.
Since the shipped example pins the child, the documented post-deploy verification step is
already unrunnable against it. One of the two is wrong; deciding which is part of this work.

## Acceptance

The record, the offline tests and the resolver fix exist, and the plan-time preconditions are
either in place or explicitly deferred with that decision recorded here.

You would know the guard holds by mutating the thing it must catch: change a shipped
`ELSPETH_WEB__*` name to one the recorded image does not carry, and the test must go red. A
guard that has never been seen to fail is not evidence of anything. The resolver fix is
verified when both documented helpers accept the digest shape the shipped example actually
pins.
