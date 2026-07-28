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
- Explicit normal-installer and IAM-lifecycle AWS profiles, plus the account and
  region chosen by the operator.
- AWS credentials available through both named SDK/CLI profiles.
- A digest-pinned ELSPETH application image.
- Two distinct `bedrock/...` provider IDs plus the exact inference-profile and
  foundation-model ARNs those IDs may invoke.
- An active Bedrock model-access agreement for each chosen model id in the
  target account. Confirm with `aws bedrock get-foundation-model-availability
  --model-id <id>` that `agreementAvailability` reports `AVAILABLE`.
  Third-party models may additionally require an AWS Marketplace
  subscription the account must complete first; first-party Amazon models
  generally have the agreement by default. A correct IAM policy still fails
  invocation until this agreement exists.

The normal provider still requires an explicit AWS profile, account, and region;
the lifecycle provider adds a second explicit profile for the same account and
region.

Before every init, apply, or destroy, verify the named profile, identity, and
region explicitly:

```sh
export AWS_PROFILE=REPLACE_WITH_AWS_PROFILE
export IAM_LIFECYCLE_AWS_PROFILE=REPLACE_WITH_DISTINCT_IAM_LIFECYCLE_AWS_PROFILE
export AWS_REGION=REPLACE_WITH_AWS_REGION
aws --profile "$AWS_PROFILE" --region "$AWS_REGION" sts get-caller-identity
aws --profile "$IAM_LIFECYCLE_AWS_PROFILE" --region "$AWS_REGION" sts get-caller-identity
aws --profile "$AWS_PROFILE" configure get region
aws --profile "$IAM_LIFECYCLE_AWS_PROFILE" configure get region
```

Compare both returned accounts with `aws_account_id` and both configured
regions with `aws_region`. Set the same required `aws_profile` and
`iam_lifecycle_aws_profile` values in the bootstrap and scenario tfvars. Do not
rely on an implicit profile, region, or remembered account. Each provider binds
its resources to its explicit profile while `allowed_account_ids`
independently protects the account boundary.

### Installer policy and task-role boundary

The package deliberately splits IAM authority across two principals:

- `iam/installer-policy.json.tftpl` is for the normal installer. It separates
  discovery reads from mutations, limits named resources, manages only the
  known inline and managed role-policy bindings, and permits `iam:PassRole`
  only to `ecs-tasks.amazonaws.com`. It cannot create or delete the generated
  roles, change their trust or boundary, or manage the boundary policy.
- `iam/lifecycle-policy.json.tftpl` is for a separate IAM lifecycle principal.
  It can create, tag, and delete only the four bounded scenario-role patterns
  and can create, version, and delete only the exact run boundary. Explicit
  denies prevent it from adding role permissions, passing or assuming a role,
  or starting an ECS task.

Render both policies for one account, region, and run before attaching them to
their respective principals:

```sh
export aws_account_id=REPLACE_WITH_12_DIGIT_ACCOUNT
export aws_region=REPLACE_WITH_AWS_REGION
export run_id=REPLACE_WITH_LOWERCASE_UUID
export backend_state_bucket=REPLACE_WITH_EXACT_BOOTSTRAP_STATE_BUCKET
export ecr_repository=REPLACE_WITH_EXACT_BOOTSTRAP_APP_REPOSITORY
export cloudwatch_agent_ecr_repository=REPLACE_WITH_EXACT_BOOTSTRAP_AGENT_REPOSITORY
export iam_permissions_boundary_arn="arn:aws:iam::${aws_account_id}:policy/elspeth-${run_id}-ecs-boundary"
scenario_a_namespace="a-$(printf '%s\0A' "$run_id" | sha256sum | cut -c1-20)"
scenario_b_namespace="b-$(printf '%s\0B' "$run_id" | sha256sum | cut -c1-20)"
compact_run_id="$(printf '%s' "$run_id" | tr -d '-')"
export scenario_a_bucket="elspeth-${scenario_a_namespace}-$(printf '%.12s' "$compact_run_id")"
export scenario_b_bucket="elspeth-${scenario_b_namespace}-$(printf '%.12s' "$compact_run_id")"
mkdir -p bootstrap/.terraform
envsubst '${aws_account_id} ${aws_region} ${run_id} ${backend_state_bucket} ${ecr_repository} ${cloudwatch_agent_ecr_repository} ${scenario_a_bucket} ${scenario_b_bucket}' \
  < iam/installer-policy.json.tftpl \
  > bootstrap/.terraform/installer-policy.json
envsubst '${aws_account_id} ${run_id} ${iam_permissions_boundary_arn}' \
  < iam/lifecycle-policy.json.tftpl \
  > bootstrap/.terraform/iam-lifecycle-policy.json
```

Inspect the rendered JSON and attach each policy using the account's normal IAM
administration path. Keep the two profiles backed by distinct principals for
the supported least-privilege installation. A purpose-built root acceptance
profile may set both provider variables to `elspeth-acceptance` for a smoke
exercise, but that deliberately gives up the privilege separation and is not
the supported least-privilege posture. If an account-specific prerequisite is
missing, use a trusted administrator only to install or amend these policies in
the dedicated disposable account; do not run Terraform with an
account-administrator wildcard policy.

The bucket derivation above is the same deterministic formula used by the
scenario module. The rendered policy consequently limits S3 bucket/object
mutations and ECR image push or force-delete operations to the exact bootstrap,
Scenario A, and Scenario B names for this run. The three bootstrap names must
match `examples/bootstrap.tfvars` exactly.

Bootstrap creates the custom run-scoped boundary named by
`iam_permissions_boundary_arn`; copy the `iam_permissions_boundary_arn`
bootstrap output into each scenario tfvars file. Both generated ECS roles must
use that boundary. The normal installer can add the package's narrow role
policies and pass the roles but cannot widen or remove their boundary. The
lifecycle principal can create or delete the bounded roles but cannot add
permissions or activate them. Effective task permissions remain the
intersection of the lifecycle-principal-controlled boundary and the normal
installer's task/execution policies. The boundary permits only the package
runtime surfaces, including destination foundation models needed by the exact
cross-region Bedrock inference-profile inputs.

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
terraform -chdir=bootstrap output -raw iam_permissions_boundary_arn
```

Bootstrap state is local by design because it creates the remote state bucket.
Preserve that state until both scenario states have been destroyed. Keep both
credential profiles available for the entire lifecycle. Destroy Scenario A and
Scenario B first, then destroy bootstrap last so the boundary, state bucket,
and repositories remain available throughout scenario teardown.

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
Whichever of `composer_model`/`composer_advisor_model` is a cross-region
(`global.`/`us.`/`eu.`/`apac.`) profile also needs a wildcard-region
foundation-model grant (`arn:aws:bedrock:*::foundation-model/<base-model-id>`)
alongside the region-pinned inference-profile ARN, because Bedrock authorizes
that call against the underlying foundation model in whichever region the
profile actually routes to; the module derives this grant automatically, and
the run-scoped permissions boundary already allows the matching wildcard
resource so the grant is not intersected away. The task role also needs
bucket-scoped, unconditioned `s3:ListBucket` on the acceptance bucket,
because without it S3 cannot distinguish a missing object from a forbidden
one and returns `403` instead of `404`; the boundary grants the matching
bucket-level resource.

Aurora creates one administrative database first, then the database bootstrap
task creates independent `elspeth_session` and `elspeth_landscape` databases.
Schema and runtime roles are separate; the runtime role does not own schemas.
This package pins Aurora PostgreSQL `16.13`. That exact version was confirmed
available and orderable with `db.serverless` in `ap-southeast-1`. The variable
validation rejects other engine versions until they are separately verified.

## Immutable RDS trust-root admission

The image must contain `/etc/elspeth/rds/global-bundle.pem` with SHA-256
`e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3`.
Its OCI CA label must be `rds-ca-rsa2048-g1`. Every ELSPETH container in the
task definitions except the web container (`elspeth-web`, candidate and
rollback) must set `readonlyRootFilesystem` to `true`. The web container is
exempt because ECS Exec — which runs the acceptance checks inside it — is
unsupported by AWS with a read-only root filesystem, and its multipart and
telemetry paths write to `/tmp`; its trust root remains immutable through
startup digest verification of the 0444 root-owned baked file.

The schema and runtime doctor JSON must report all of these checks as green
before the web service is enabled:

- `rds_trust_root`
- `session_tls`
- `landscape_tls`
- `session_schema`
- `landscape_schema`

`session_tls` and `landscape_tls` attest only the connection that inspected
each schema: TLS is proven on the same connection the schema probe ran over,
not on every connection a run opens. A `--init-schema` DDL connection uses
the identical URL and `sslmode` posture but is not itself separately probed.

The task definitions and bootstrap must not fetch the CA bundle from AWS's
public RDS truststore endpoint at runtime, and must not stage it under a
world-writable `/tmp` path or under `/var/lib/elspeth/rds-global-bundle.pem`.
Only the baked, root-owned, 0444 image path above is trusted.

OCI digest
`sha256:c5e65357b7470cf1a702eeb084e865f0f5e0e43ab9741b76e872fa7568029700`
predates this contract. It is an acceptance-attempt artifact and is not
eligible for `0.7.2-RC-280726`.

Before promoting a candidate, verify the baked bundle, the OCI CA labels, the
live Aurora CA identifier, and the `readonlyRootFilesystem` split:

```sh
docker buildx imagetools inspect "$CANDIDATE_IMAGE"
test "$(docker inspect --format \
  '{{ index .Config.Labels "io.elspeth.rds-ca-bundle-sha256" }}' \
  "$CANDIDATE_IMAGE")" = \
  e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3
test "$(docker inspect --format \
  '{{ index .Config.Labels "io.elspeth.rds-ca-certificate-identifier" }}' \
  "$CANDIDATE_IMAGE")" = rds-ca-rsa2048-g1

DB_INSTANCE_IDENTIFIER=$(terraform -chdir=scenario-a output -json resolved_inventory \
  | jq -r '.orphan_sweep.rds_db_instance_identifiers[0]')
aws --profile "$AWS_PROFILE" --region "$AWS_REGION" rds \
  describe-db-instances \
  --db-instance-identifier "$DB_INSTANCE_IDENTIFIER" \
  --query 'DBInstances[0].CACertificateIdentifier' \
  --output text | grep -Fx rds-ca-rsa2048-g1

TASK_DEFINITION=$(terraform -chdir=scenario-a output -raw runtime_doctor_task_definition_arn)
aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecs \
  describe-task-definition \
  --task-definition "$TASK_DEFINITION" \
  --query 'taskDefinition.containerDefinitions[?name!=`cloudwatch-agent` && name!=`elspeth-web`].{name: name, readonlyRootFilesystem: readonlyRootFilesystem}' \
  --output json | jq -e 'length > 0 and all(.[]; .readonlyRootFilesystem == true)'

CANDIDATE_TASK_DEFINITION=$(terraform -chdir=scenario-a output -json resolved_inventory \
  | jq -r '.values.CANDIDATE_TASK_DEFINITION')
aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecs \
  describe-task-definition \
  --task-definition "$CANDIDATE_TASK_DEFINITION" \
  --query 'taskDefinition.containerDefinitions[?name==`elspeth-web`].{name: name, readonlyRootFilesystem: readonlyRootFilesystem}' \
  --output json | jq -e 'length == 1 and all(.[]; .readonlyRootFilesystem != true)'
```

Both checks project `{name, readonlyRootFilesystem}` objects rather than the
bare field because a JMESPath filter-then-field projection drops null
results: against a container whose field is absent, the bare-field form
yields the same empty array as a query that matched nothing at all, and any
`all(...)` over an empty array is vacuously true. Run the first check only
against `$TASK_DEFINITION` above — the schema-init or runtime doctor
definition, container name `doctor` — never against the payload or
local-auth verifier task definitions
(`resolved_inventory.values.PAYLOAD_VERIFIER_TASK_DEFINITION` /
`LOCAL_AUTH_VERIFIER_TASK_DEFINITION`): those two bind their read-only
container under the `elspeth-web` container name, so against either of them
the `name!='elspeth-web'` projection returns an empty array and the
`length > 0` guard fails closed rather than silently passing. Run the second
check only against a candidate or rollback web task definition —
`$CANDIDATE_TASK_DEFINITION` above, or
`resolved_inventory.values.PREVIOUS_TASK_DEFINITION` on an upgrade: its
`readonlyRootFilesystem` field is absent (not a literal `false`), so the
projection yields `[{"name": "elspeth-web", "readonlyRootFilesystem":
null}]`; `length == 1` proves the query found exactly the intended container
(a bare-field projection would drop the null and yield `[]`, which an
unguarded `all()` would pass with zero evidentiary value — including against
a wrong task-definition ARN or a typo'd container name), and
`.readonlyRootFilesystem != true` proves the documented exemption rather
than a silent `true` that would break ECS Exec. The payload and
local-auth verifier task definitions run a read-only container
(`readonlyRootFilesystem = true`) under the `elspeth-web` container name;
neither jq command above exercises them — that source-level contract is
asserted directly against `modules/scenario/ecs.tf` by
`tests/unit/deployment/test_aws_ecs_terraform_package.py`.

### Upgrading an existing install

Applying this module version to an existing install rotates all five Secrets
Manager database URLs to the canonical immutable-trust query immediately,
while `aws_ecs_service.web` ignores task-definition changes
(`lifecycle.ignore_changes`). An old-image task that restarts inside that
window injects the new canonical URL, lacks the baked bundle, and
crash-loops. Apply and roll the service in one operation: run
`terraform apply`, then immediately
`aws ecs update-service --force-new-deployment` with the new qualified image
digest. Pinning `ca_cert_identifier` on an existing Aurora instance triggers
a database modification with engine-dependent restart semantics — expect and
schedule it. No pre-trust-root image digest is rollback-eligible after the
upgrade.

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
installation, verify both explicit AWS profiles, account, and region again,
and run the ordinary full destroy:

```sh
terraform -chdir=scenario-a destroy \
  -var-file=../examples/scenario-a.tfvars
```

Use `scenario-b` and its inputs for the B variant. Once every scenario state is
empty, destroy bootstrap last with the same two profiles:

```sh
terraform -chdir=bootstrap destroy \
  -var-file=../examples/bootstrap.tfvars
```

Terraform's provider split and resource dependencies ensure the normal
installer removes inline policies and the known managed attachment before the
lifecycle principal deletes each role. The lifecycle principal then deletes
the custom boundary only during the final bootstrap destroy. Repeated apply
and full `terraform destroy` commands remain the supported idempotent workflow.
