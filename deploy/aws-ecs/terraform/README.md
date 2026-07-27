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
installation. Its tfvars must name the nonempty Cognito subject that the
acceptance run owns; the resolved inventory binds that subject to the
Terraform-owned pool.

Both scenarios create a disposable self-signed ALB certificate. It is valid for
only 24 hours. Terraform outputs its CA certificate so a client can trust it
temporarily; this is not a production certificate strategy.

## Prerequisites and identity checks

- Terraform `>= 1.14, < 2.0`.
- An explicit AWS profile, account, and region chosen by the operator.
- AWS credentials available through that named SDK/CLI profile.
- A digest-pinned ELSPETH application image.
- Two distinct `bedrock/...` provider IDs plus the exact inference-profile and
  foundation-model ARNs those IDs may invoke.

Before every init, apply, or destroy, verify the named profile, identity, and
region explicitly:

```sh
export AWS_PROFILE=REPLACE_WITH_AWS_PROFILE
export AWS_REGION=REPLACE_WITH_AWS_REGION
aws --profile "$AWS_PROFILE" --region "$AWS_REGION" sts get-caller-identity
aws --profile "$AWS_PROFILE" configure get region
```

Compare the returned account with `aws_account_id` and the configured region
with `aws_region`. Set the same required `aws_profile` value in the bootstrap
and scenario tfvars. Do not rely on an implicit profile, region, or remembered
account. The providers bind all AWS resources to that profile while
`allowed_account_ids` independently protects the account boundary.

### Installer policy and task-role boundary

`iam/installer-policy.json.tftpl` is the Terraform installer policy template.
It separates discovery reads from mutations, limits named resources and IAM
roles, requires the run tag where the AWS action supports request or resource
tags, and permits `iam:PassRole` only to `ecs-tasks.amazonaws.com`. Render it
for one account, region, and run before attaching it to the installer
principal:

```sh
export aws_account_id=REPLACE_WITH_12_DIGIT_ACCOUNT
export aws_region=REPLACE_WITH_AWS_REGION
export run_id=REPLACE_WITH_LOWERCASE_UUID
export backend_state_bucket=REPLACE_WITH_EXACT_BOOTSTRAP_STATE_BUCKET
export ecr_repository=REPLACE_WITH_EXACT_BOOTSTRAP_APP_REPOSITORY
export cloudwatch_agent_ecr_repository=REPLACE_WITH_EXACT_BOOTSTRAP_AGENT_REPOSITORY
scenario_a_namespace="a-$(printf '%s\0A' "$run_id" | sha256sum | cut -c1-20)"
scenario_b_namespace="b-$(printf '%s\0B' "$run_id" | sha256sum | cut -c1-20)"
compact_run_id="$(printf '%s' "$run_id" | tr -d '-')"
export scenario_a_bucket="elspeth-${scenario_a_namespace}-$(printf '%.12s' "$compact_run_id")"
export scenario_b_bucket="elspeth-${scenario_b_namespace}-$(printf '%.12s' "$compact_run_id")"
mkdir -p bootstrap/.terraform
envsubst '${aws_account_id} ${aws_region} ${run_id} ${backend_state_bucket} ${ecr_repository} ${cloudwatch_agent_ecr_repository} ${scenario_a_bucket} ${scenario_b_bucket}' \
  < iam/installer-policy.json.tftpl \
  > bootstrap/.terraform/installer-policy.json
```

Inspect the rendered JSON and attach it using the account's normal IAM
administration path. If an account-specific prerequisite is missing, use a
trusted administrator only to install or amend this policy in the dedicated
disposable account; do not run Terraform with an account-administrator
wildcard policy.

The bucket derivation above is the same deterministic formula used by the
scenario module. The rendered policy consequently limits S3 bucket/object
mutations and ECR image push or force-delete operations to the exact bootstrap,
Scenario A, and Scenario B names for this run. The three bootstrap names must
match `examples/bootstrap.tfvars` exactly.

Both generated ECS roles must use the AWS-managed
`arn:aws:iam::aws:policy/PowerUserAccess` permissions boundary. The installer
can attach that boundary but cannot create, version, replace, or delete any
managed policy. This prevents it from widening the boundary before adding
inline role permissions and passing a role to ECS. Effective task permissions
remain the intersection of the installer-immutable boundary and this package's
narrow task/execution policies, including the exact cross-region Bedrock
foundation-model ARNs supplied in scenario inputs.

Networking relationships, EFS mount targets, Cognito children, EventBridge
targets, and X-Ray resources use supported ARN and run-tag conditions. The
`DedicatedAccountOnlyUntaggedMutations` residual contains only CloudWatch Logs
account-level resource-policy mutations, whose API does not support
resource-level authorization. The statement is region-limited and contains no
wildcard service actions, but another principal's Logs resource policy in that
region could still be affected. This installer policy is therefore supported
only in a dedicated empty account and is **not supported in a shared account**.

## 1. Bootstrap state and image repositories

Copy `examples/bootstrap.tfvars.example` to an ignored
`examples/bootstrap.tfvars`, replace every placeholder, then:

```sh
terraform -chdir=bootstrap init
terraform -chdir=bootstrap apply -var-file=../examples/bootstrap.tfvars
```

Bootstrap state is local by design because it creates the remote state bucket.
Preserve that state until both scenario states have been destroyed. Destroy
Scenario A and Scenario B first, then destroy bootstrap last so the state
bucket and repositories remain available throughout scenario teardown.

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
region/profile placeholders:

```sh
cp examples/scenario-a.s3.tfbackend.example examples/scenario-a.s3.tfbackend
cp examples/scenario-b.s3.tfbackend.example examples/scenario-b.s3.tfbackend
```

The A and B files use separate state keys, S3 server-side encryption, and S3
native state locking (`use_lockfile = true`). Their explicit `profile` must
match the scenario tfvars. Do not make their keys equal.

## 3. Install Scenario A

Copy `examples/scenario-a.tfvars.example` to an ignored
`examples/scenario-a.tfvars`, replace every placeholder, and check that both
Bedrock provider IDs are present and distinct.

Scenario A has no rollback or acceptance-coordinator inputs. Its compatibility
inventory derives the candidate baseline and absolute tracked source paths from
the package itself. `scenario-a/codeblind-compatibility.json` records the
standalone-install facts and the absence of a pre-existing transaction-search
baseline. It is deterministic package metadata, not an acceptance binding
artifact. Scenario B retains its acceptance-only inputs.

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
database cannot enter a failing restart loop. The schema-init task definition
uses schema-owner database URLs and is reserved for
`doctor aws-ecs --init-schema --json`. The separate runtime doctor definition
uses the same runtime-only database URLs as the web service and runs
`doctor aws-ecs --json`.

Run them in that order with the explicit task definitions, network
configurations, and command overrides:

```sh
export AWS_PROFILE=REPLACE_WITH_AWS_PROFILE
export AWS_REGION=REPLACE_WITH_AWS_REGION
ECS_CLUSTER=$(terraform -chdir=scenario-a output -raw cluster_name)

SCHEMA_TASK_DEFINITION=$(terraform -chdir=scenario-a output -raw schema_init_doctor_task_definition_arn)
SCHEMA_NETWORK=$(terraform -chdir=scenario-a output -raw schema_init_doctor_network_configuration)
SCHEMA_OVERRIDES=$(terraform -chdir=scenario-a output -raw schema_init_doctor_overrides)
SCHEMA_TASK_ARN=$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecs run-task \
  --cluster "$ECS_CLUSTER" \
  --task-definition "$SCHEMA_TASK_DEFINITION" \
  --launch-type FARGATE \
  --network-configuration "$SCHEMA_NETWORK" \
  --overrides "$SCHEMA_OVERRIDES" \
  --count 1 \
  --query 'tasks[0].taskArn' \
  --output text)
aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecs wait tasks-stopped \
  --cluster "$ECS_CLUSTER" --tasks "$SCHEMA_TASK_ARN"
test "$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecs describe-tasks \
  --cluster "$ECS_CLUSTER" --tasks "$SCHEMA_TASK_ARN" \
  --query 'tasks[0].containers[?name==`doctor`].exitCode | [0]' \
  --output text)" = 0

RUNTIME_TASK_DEFINITION=$(terraform -chdir=scenario-a output -raw runtime_doctor_task_definition_arn)
RUNTIME_NETWORK=$(terraform -chdir=scenario-a output -raw runtime_doctor_network_configuration)
RUNTIME_OVERRIDES=$(terraform -chdir=scenario-a output -raw runtime_doctor_overrides)
RUNTIME_TASK_ARN=$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecs run-task \
  --cluster "$ECS_CLUSTER" \
  --task-definition "$RUNTIME_TASK_DEFINITION" \
  --launch-type FARGATE \
  --network-configuration "$RUNTIME_NETWORK" \
  --overrides "$RUNTIME_OVERRIDES" \
  --count 1 \
  --query 'tasks[0].taskArn' \
  --output text)
aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecs wait tasks-stopped \
  --cluster "$ECS_CLUSTER" --tasks "$RUNTIME_TASK_ARN"
test "$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecs describe-tasks \
  --cluster "$ECS_CLUSTER" --tasks "$RUNTIME_TASK_ARN" \
  --query 'tasks[0].containers[?name==`doctor`].exitCode | [0]' \
  --output text)" = 0
```

Both task exit codes must be `0`. Then print, inspect, and explicitly run
`service_enable_command`; never enable the service after only the privileged
schema-init check.

The service lifecycle ignores later desired-count and task-definition changes
so ordinary image deployments remain an explicit operator action.

The ECS task role is the only Bedrock credential source. The package does not
accept static AWS keys, a profile, a custom endpoint, a model gateway, or an
AgentCore setting. Composer model names are ordinary non-secret environment
values. `bedrock:InvokeModel` is limited to the ARNs supplied in tfvars.

Aurora creates one administrative database first, then the database bootstrap
task creates independent `elspeth_session` and `elspeth_landscape` databases.
Schema and runtime roles are separate; the runtime role does not own schemas.
This package pins Aurora PostgreSQL `16.13`. That exact version was confirmed
available and orderable with `db.serverless` in `ap-southeast-1`. The variable
validation rejects other engine versions until they are separately verified.

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
installation, verify the explicit AWS profile, account, and region again, and
run:

```sh
terraform -chdir=scenario-a destroy \
  -var-file=../examples/scenario-a.tfvars
```

Use `scenario-b` and its inputs for the B variant. Destroy scenarios before
destroying bootstrap resources.
