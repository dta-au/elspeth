---
title: Add a plan-time guard against deploying an image incompatible with the Terraform package
labels: [area/deployment, type/task]
---

The AWS ECS Terraform package ships environment-variable names that must exist in the image
being deployed. Nothing checks that at plan time, so a mismatch is silent at apply and fatal
at boot. This covers the half of the guard that is buildable and testable without an AWS
account; publishing an image is not.

## The incompatibility

`settings_from_env` in `src/elspeth/web/config.py` raises
`RuntimeError("Unknown ELSPETH_WEB__ setting: …")` (`config.py:1546`) for any name absent from
`WebSettings.model_fields`, and `WebSettings` is declared `extra="forbid"`. The scenario
module ships about fifty distinct `ELSPETH_WEB__*` names in three syntactic shapes:
`runtime_environment` entries and bare `plugin_policy_projection` keys in
`deploy/aws-ecs/terraform/modules/scenario/locals.tf`, and export lines in the ECS identity
wrapper in `modules/scenario/ecs.tf`. The contract is that the shipped names must be a subset
of the `WebSettings` compiled into the image being deployed.

## What already exists

`tests/unit/deployment/test_aws_ecs_terraform_package.py` asserts the shipped names are a
subset of `WebSettings` at the current tip — package-versus-tip, not package-versus-image.
Commit `bd1be4aab` added a computed floor test proving the documented minimum revision is
exactly right. All image admission today runs through `terraform_data` plus `local-exec` in
`modules/scenario/image_provenance.tf`, that is, at apply time. The module carries four
`precondition` blocks (in `iam_observability.tf`, `ecs.tf` twice, and `storage_identity.tf`)
and none of them touches an image.

## Shape

A committed record — schema, shipped names, minimum revision, approved image rows — read with
`jsondecode` by plan-time preconditions in `image_provenance.tf`, with offline tests matching
the oracle shape already established in
`tests/unit/deployment/test_aws_iam_policy_oracles.py`.

Two traps the design must avoid. Pin the amd64 child manifest digest, never the multi-arch
index; `deploy/aws-ecs/terraform/examples/scenario-a.tfvars` already pins the child and says
why. And `candidate_sha` is a git SHA while `candidate_image` is a digest reference — these
have been conflated before.

## Sequencing constraint

A membership precondition over an approved-image record fails closed on an empty record, so
landing it would make the package unable to run `terraform plan` at all in an environment
where nobody can publish an image to populate the record. Land the offline-provable pieces
first — the record schema, the tests, a digest-resolution helper — and gate the preconditions
themselves on an explicit decision. Do not seed a digest nobody can verify: the shipped
example pins `sha256:8adff75e…` as the amd64 child, and confirming that against the registry
needs `aws ecr batch-get-image` in an AWS account that was removed on 2026-08-10.

## A companion contradiction to resolve

Two documented helpers disagree on digest shape. `resolve_scannable_digest`
(`docs/runbooks/aws-ecs-cold-install.md:512`) passes a plain child manifest through unchanged.
`resolve_target_ecr_digest` (`deploy/aws-ecs/terraform/README.md:597`, reproduced in the
runbook and pinned by `test_aws_ecs_terraform_package.py`) restricts accepted media types to
index/list and aborts with `expected one parent index` (`README.md:619`) when handed a child.
Since the shipped example pins the child, the documented post-deploy verification step is
already unrunnable against it.

## Done

Done means: the record, the offline tests and the resolver fix exist and are
mutation-tested — a package/image mismatch must turn the test red — and the plan-time
preconditions are either in place or explicitly deferred, with that decision recorded here.
