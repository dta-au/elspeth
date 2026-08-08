# Phase 4: Terraform Shared-Module Generalization + Scenario C

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.
>
> Owed since Phase 3 close ("written at Phase 3 close", master plan row 4). Scope per
> `docs/superpowers/specs/2026-07-30-elspeth-llm-gateway-integration-design.md` §"AWS Terraform
> Scenario C" and §"Terraform and deployment" acceptance criteria 1–10. The Phase 3 rescope
> (2026-07-31) left this phase **unchanged in shape**: the gateway sidecar is still a sidecar;
> ELSPETH points at it via `composer_endpoint_base_url` = `http://127.0.0.1:8787/v1`.

## Ground truth (surveyed 2026-08-09, release/0.7.2 @ 328da9888)

- Shared module: `deploy/aws-ecs/terraform/modules/scenario/` (~3,500 lines, 10 files).
- Scenario-letter inference sites to replace with explicit axes:
  - `locals.tf:23` `deployment_mode = var.scenario_id == "A" ? "first" : "upgrade"`
  - `locals.tf:448` auth provider `local` vs `oidc` keyed on letter; `locals.tf:462,504-505` Cognito/OIDC wiring keyed on `== "B"`.
  - `variables.tf:14` `scenario_id ∈ {A, B}`; `variables.tf:151-183` `composer_model`/`composer_advisor_model` hard-require `bedrock/` prefix; `bedrock_inference_profile_arns` / `bedrock_foundation_model_arns` required non-empty unconditionally.
  - `iam_observability.tf:95-102` Bedrock invoke + guardrail grants; guardrail resources themselves.
- `scenario_id` also serves **identity** (naming, per-letter CIDRs `locals.tf:25-26`, `ELSPETH_ACCEPTANCE_SCENARIO_ID`). Identity stays letter-keyed and gains `C`; behavior moves to the axes. This split is the design's core demand ("Scenario-letter conditionals no longer infer either concern implicitly").
- Contract-test harness: `terraform test` with `mock_provider` (scenario-a `web_policy.tftest.hcl`, `codeblind.tftest.hcl`) — runs locally, no AWS credentials. Terraform v1.15.7 confirmed on this box.
- Python-side `scenario_id` consumers exist (`src/elspeth/web/_aws_ecs_acceptance/approvals.py` `_INFRASTRUCTURE_APPROVAL_SCOPES`, others) — surveyed in Task 5.

## Global constraints

- **A/B behavior frozen** (Terraform acceptance criterion 1). Scenario-a tftests must pass **unmodified** — they pin rendered policy strings the acceptance controller hashes. Any edit to those files to make them pass is a red flag, not a fix.
- **No terraform apply / no live AWS from agent scope.** Live disposable acceptance for Scenario C is operator-gated (master plan). The Sydney stack was torn down 2026-08-07 and Round-8 battery work is driving live infra from this tree — module edits must be plan/validate/test-verified only.
- Scenario C is **cold-install** (`first + custom_gateway`), local auth, no OIDC — a sibling of A, not B.
- Secrets: Terraform accepts **selectors/ARNs only**, never literals. Bearer → both containers; OAuth client ID/secret → gateway only; ELSPETH app/db secrets → never the gateway.
- Gateway sidecar: essential, loopback :8787 only, **no port mapping**, non-root, read-only rootfs, no EFS, own CPU/memory reservations (task total increased explicitly, not overcommitted), metadata-only logs, healthcheck `/readyz`, Web `dependsOn` gateway `HEALTHY`.
- Image admission: gateway digest goes through provenance equivalent to the Web candidate gate (`image_provenance.tf`) — approved account/region/repo, immutable digest, scan status, contract major, adapter identity. **No placeholder images, no admission bypass.**
- Full `pytest tests/` is the phase gate; `elspeth-lints check` exit 0; commit by pathspec; judge-signed fingerprint drift recorded, never signed ([O1]).

## Tasks

### Task 1: Introduce the two axes without changing behavior

**Files:** `modules/scenario/variables.tf`, `locals.tf`; `scenario-a/main.tf`, `scenario-b/main.tf` (pass axes explicitly); scenario-a tftests (must stay green UNMODIFIED).

- [ ] Add `deployment_lifecycle` (`first`|`upgrade`) and `llm_backend` (`bedrock`|`custom_gateway`) variables with closed-vocabulary validation. Add cross-checks pinning today's pairs: `A ⇒ first+bedrock`, `B ⇒ upgrade+bedrock` (fail loudly on contradiction rather than silently preferring one source of truth).
- [ ] Replace behavior conditionals: `deployment_mode` reads `deployment_lifecycle`; auth-provider/OIDC/Cognito conditionals key on lifecycle (first→local, upgrade→oidc). Identity concerns (naming, CIDR map, `ELSPETH_ACCEPTANCE_SCENARIO_ID`) stay letter-keyed; extend the letter domain to `C` with its own CIDR block (10.73.0.0/16 family) and `C ⇒ first+custom_gateway` pairing.
- [ ] Gate: `terraform test` in scenario-a green with **zero tftest edits**; `terraform validate` green in scenario-a and scenario-b; `terraform init -backend=false` acceptable for both roots.
- [ ] Commit: `refactor(terraform): explicit deployment-lifecycle and llm-backend axes in the scenario module`

### Task 2: Make every Bedrock surface conditional on `llm_backend`

**Files:** `modules/scenario/variables.tf`, `locals.tf`, `iam_observability.tf`; new tftest coverage.

- [ ] `composer_model`/`composer_advisor_model` validation becomes backend-aware: `bedrock` keeps today's `bedrock/` rule verbatim; `custom_gateway` requires a non-bedrock model ID and distinct primary/advisor (two-model independence preserved).
- [ ] `bedrock_inference_profile_arns` / `bedrock_foundation_model_arns` become optional with a backend-conditional check: required non-empty when `bedrock`, **must be empty/unset when `custom_gateway`** (criterion 2: "requires no Bedrock model or ARN input" — reject, don't ignore, stray Bedrock inputs).
- [ ] Bedrock IAM statements, guardrail resources, and bedrock-plugin policy defaults created only when backend is `bedrock`. Survey `plugin_preferences` defaults and the rendered web plugin policy for bedrock-plugin references that must swap under `custom_gateway`.
- [ ] Gate: scenario-a tftests still green unmodified; new mock-provider tftest proving a `custom_gateway` configuration validates with no Bedrock inputs and that Bedrock inputs are *rejected*.
- [ ] Commit: `feat(terraform): llm_backend=custom_gateway drops every Bedrock surface`

### Task 3: Gateway sidecar — topology, secrets, admission

**Files:** `modules/scenario/ecs.tf`, `image_provenance.tf`, `variables.tf`, `outputs.tf`, `network.tf` (egress note only).

- [ ] Container definition per the constraint block above; explicit task CPU/memory arithmetic; `dependsOn` `[cloudwatch-agent, gateway HEALTHY]` for Web; gateway env carries only bearer + OAuth + gateway config.
- [ ] Secrets selectors: `gateway_bearer_secret` (both containers, respective env names), `gateway_oauth_client_id_secret` / `gateway_oauth_client_secret_secret` (gateway only). Execution role gains exactly the new secret + gateway-image ECR access; task role unchanged.
- [ ] Gateway image admission mirroring the Web candidate gate: pinned digest variable, approved repo/account/region checks, contract-major and adapter-identity expectations bound into the task definition and inventory outputs. Mutable tag ⇒ plan-time failure.
- [ ] No inbound SG rule for :8787 anywhere; loopback only (assert structurally in tftest: gateway container has no `portMappings`).
- [ ] Gate: new tftests for topology (exact 3-container set, essentiality, dependsOn, no port mapping, secret separation — OAuth absent from Web's secrets, app/db secrets absent from gateway's).
- [ ] Commit: `feat(terraform): elspeth-llm-gateway sidecar with admission, secrets separation, and health gating`

### Task 4: Scenario C root

**Files:** new `deploy/aws-ecs/terraform/scenario-c/` (`main.tf`, `variables.tf`, `outputs.tf`, `versions.tf`, tftests, `codeblind-compatibility.json`, examples); runbook instructions.

- [ ] Root module passing `scenario_id="C"`, `deployment_lifecycle="first"`, `llm_backend="custom_gateway"`; composer endpoint settings target `http://127.0.0.1:8787/v1`; profile JSON references the Web-side bearer env name. Runnable without editing shared-module source (criterion: no module forks).
- [ ] tftests: plans as `first + custom_gateway` with zero Bedrock variables; sidecar topology assertions from Task 3 exercised through the C root; outputs disclose both image digests + contract + adapter fingerprint but no secret names/values.
- [ ] Codeblind compatibility data + variable example + backend example modeled on scenario-a's.
- [ ] Runbook: cold-install instructions ± the documented differences from Scenario A (gateway digest input, three secrets, egress destinations documented without baking an agency hostname).
- [ ] Commit: `feat(terraform): Scenario C — cold-install with custom-gateway sidecar`

### Task 5: One-off task parity + Python-side scenario admission

**Files:** `modules/scenario/ecs.tf` (one-off task definitions), `src/elspeth/web/_aws_ecs_acceptance/` (survey first), rollback preflight surface.

- [ ] Survey every Python-side `scenario_id` literal (`approvals.py` `_INFRASTRUCTURE_APPROVAL_SCOPES`, runbook controllers, evidence bindings). Extend admission to `C` where the code enumerates scenarios; **parity-sweep discipline**: grep every discriminator keyed on scenario before assuming the set is closed.
- [ ] LLM-exercising one-off tasks (runtime doctor, live acceptance) launch the identical pinned sidecar + secret/config bindings; schema-init/storage-only tasks do not. Rollback preflight: Web rollback image must support gateway contract major 1 when the deployed sidecar is present (reject direct-Bedrock-only rollback before service mutation).
- [ ] Commit: `feat(deploy): one-off task sidecar parity and scenario-C admission`

### Task 6: Phase close

- [ ] `terraform test` green: scenario-a (unmodified tests), scenario-c (new); `terraform validate` all three roots.
- [ ] Full `pytest tests/` green modulo operator-disregarded in-flight failures (compare against the 2026-08-09 corpus: the 11 lane-G masquerade failures are not ours).
- [ ] `elspeth-lints check` exit 0 vs prior corpus; record judge-signed drift untouched.
- [ ] Update master plan row 4 (plan doc reference + delivery state); Filigree evidence comment on `elspeth-bd130706dc`; hand the operator the live-acceptance worklist (deploy, health verification, disposable acceptance — criteria 3–10 live halves) since agent scope ends at plan/validate/test.

## Explicitly out of agent scope (operator-gated)

Live AWS: stack deploy, deployment/health verification against real ECS, disposable Scenario C acceptance, secret creation. The issue's done_definition ("deploys ... and passes deployment/health verification") therefore **cannot be discharged by the agent alone**; Task 6 ends with the staged worklist and evidence that everything short of live apply is green.
