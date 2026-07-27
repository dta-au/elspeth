# ELSPETH AWS ECS Terraform package

This directory is the supported Terraform source for a disposable ELSPETH
installation on ECS/Fargate. It was recovered from a previously exercised
deployment topology, then made portable and brought up to the current
installation contract. It is source code, not a claim about any current AWS
environment.

## Choose a topology

### Scenario A: cold install (recommended)

`scenario-a/` is the normal path. It creates a fresh VPC, ECS service, Aurora
PostgreSQL cluster, separate session and Landscape databases, EFS data storage,
S3 object storage, logs/metrics/traces, and local ELSPETH authentication.

### Scenario B: OIDC acceptance variant

`scenario-b/` exists only for acceptance of the Cognito/OIDC path and upgrade
or rollback exercises. Its state key, namespace, networking, database, secrets,
and Cognito hostname are isolated from Scenario A. Do not start here for a cold
installation.

Both scenarios create a disposable self-signed ALB certificate. It is valid for
only 24 hours. Terraform outputs its CA certificate so a client can trust it
temporarily; this is not a production certificate strategy.

## Prerequisites and identity checks

- Terraform `>= 1.14, < 2.0`.
- An explicit AWS account and an explicit AWS region chosen by the operator.
- AWS credentials available through the normal SDK/CLI credential chain.
- A digest-pinned ELSPETH application image.
- Two distinct `bedrock/...` provider IDs plus the exact inference-profile and
  foundation-model ARNs those IDs may invoke.

Before every init, apply, or destroy, verify identity and region explicitly:

```sh
aws sts get-caller-identity
aws configure get region
```

Compare the returned account with `aws_account_id` and the configured region
with `aws_region`. Do not rely on an implicit profile, region, or remembered
account.

## 1. Bootstrap state and image repositories

Copy `examples/bootstrap.tfvars.example` to an ignored
`examples/bootstrap.tfvars`, replace every placeholder, then:

```sh
terraform -chdir=bootstrap init
terraform -chdir=bootstrap apply -var-file=../examples/bootstrap.tfvars
```

Bootstrap state is local by design because it creates the remote state bucket.
Preserve that state until both scenario states have been destroyed.

The bootstrap creates separate ECR repositories for ELSPETH and the
shell-bearing CloudWatch agent. Temporary application tags may expire. The
agent repository expires only untagged images after 30 days. Deploy the
CloudWatch agent by digest (`repository@sha256:...`), never by tag.

Build `cloudwatch-agent-image/Dockerfile`, publish it to that dedicated
repository under a retained tag, resolve the immutable digest, and place the
digest reference in the scenario tfvars. The resolved inventory binds the
digest and SHA-256 hashes of both tracked telemetry configuration files.

## 2. Generate partial backend inputs

The scenario roots contain only `backend "s3" {}`. Generate ignored
`.tfbackend` inputs from the matching examples and replace the bucket and
region placeholders:

```sh
cp examples/scenario-a.s3.tfbackend.example examples/scenario-a.s3.tfbackend
cp examples/scenario-b.s3.tfbackend.example examples/scenario-b.s3.tfbackend
```

The A and B files use separate state keys, S3 server-side encryption, and S3
native state locking (`use_lockfile = true`). Do not make their keys equal.

## 3. Install Scenario A

Copy `examples/scenario-a.tfvars.example` to an ignored
`examples/scenario-a.tfvars`, replace every placeholder, and check that both
Bedrock provider IDs are present and distinct.

```sh
terraform -chdir=scenario-a init \
  -backend-config=../examples/scenario-a.s3.tfbackend
terraform -chdir=scenario-a workspace show
terraform -chdir=scenario-a workspace select default
terraform -chdir=scenario-a plan \
  -var-file=../examples/scenario-a.tfvars
terraform -chdir=scenario-a apply \
  -var-file=../examples/scenario-a.tfvars
terraform -chdir=scenario-a output
```

Use only an explicitly selected workspace. The `default` workspace is the
documented cold-install choice. Re-run the account and region checks before
apply.

The service is initially registered with desired count zero so an uninitialised
database cannot enter a failing restart loop. Use the
`doctor_task_definition_arn`, `doctor_network_configuration`, and
`resolved_inventory` outputs to run the doctor task with
`doctor aws-ecs --init-schema --json`. Require exit code zero, run the ordinary
doctor once more without `--init-schema`, then use `service_enable_command`.
The service lifecycle ignores later desired-count and task-definition changes
so ordinary image deployments remain an explicit operator action.

The ECS task role is the only Bedrock credential source. The package does not
accept static AWS keys, a profile, a custom endpoint, a model gateway, or an
AgentCore setting. Composer model names are ordinary non-secret environment
values. `bedrock:InvokeModel` is limited to the ARNs supplied in tfvars.

Aurora creates one administrative database first, then the database bootstrap
task creates independent `elspeth_session` and `elspeth_landscape` databases.
Schema and runtime roles are separate; the runtime role does not own schemas.
Set `aurora_engine_version` to an exact available version in the configured
major line. The validation rejects a version from another major line.

## Scenario B

Use the B backend and tfvars examples only when the OIDC acceptance variant is
required:

```sh
terraform -chdir=scenario-b init \
  -backend-config=../examples/scenario-b.s3.tfbackend
terraform -chdir=scenario-b workspace show
terraform -chdir=scenario-b workspace select default
terraform -chdir=scenario-b apply \
  -var-file=../examples/scenario-b.tfvars
```

## Outputs and teardown

The roots output the public URL, temporary CA certificate, ECS cluster and
service names, task-role ARN, runtime database secret ARN, resolved inventory,
and a teardown reminder. These are intended to be useful without reading the
Terraform source.

For teardown, select the same backend config and exact workspace used for
installation, verify the explicit AWS account and region again, and run:

```sh
terraform -chdir=scenario-a destroy \
  -var-file=../examples/scenario-a.tfvars
```

Use `scenario-b` and its inputs for the B variant. Destroy scenarios before
destroying bootstrap resources.
