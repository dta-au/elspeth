# Azure Container Apps acceptance package

Two acceptance methodologies ship in 0.8.0 on one shared core. The AWS ECS
harness (`_aws_ecs_acceptance/`, ~15,000 lines) is **thick Python**: it owns
the scenario engine, the control manifest, the gate ledger, HMAC approvals, the
orphan sweep and the operator-telemetry round trip, because it was written
before a second provider existed and Phase 6 runs on exactly those surfaces.
This package is **thin Python over the platform**: `az`, `psql`, KQL and
Resource Graph are driven by the bash runbook through its protected capture
wrappers, and Python only validates what was captured, scores the replica
probes with the shared decision tables, encodes receipts bound to a
control-plane-verified replica, and keeps a directory receipt store. Both
providers bind the same `_acceptance_common`: one `schema_facts` derivation,
one bounded-receipt validator, one exec-receipt envelope generalised by a
provider descriptor, one compatibility-gate predicate, one `testcontainer-run`
gate, one probe driver and one closed `mechanism` vocabulary. Neither provider
package imports the other (`tests/unit/architecture/test_azure_container_apps_acceptance_dependencies.py`).

## The v3 consolidation trigger (plan §6.4, recorded on 6b-9)

A provider-neutral receipt family (`elspeth.deployment-compatibility-receipt.v3`
with a `provider` discriminator and a closed `deployment_subject`) was
rejected for 0.8.0 because it would rename ECS fields and re-pin the ECS
receipt tests while Phase 6 runs on them. It is scheduled by the **earlier**
of:

- (a) a third acceptance provider entering planning — the shared core is
  extracted first, before any provider-specific code;
- (b) the first post-0.8.0 change to the compatibility-record field set or
  the exec-receipt envelope — the change lands once, in the v3 shape, not
  twice;
- (c) 0.8.1 planning, where it is a named item to accept or explicitly defer
  with a reason.

## Modules

| module | layer | contents |
|---|---|---|
| `receipt_contracts.py` | 0 | `ReplicaBinding` (`sha256("<app ARM id>/revisions/<revision>/replicas/<replica>")`), the twelve check kinds with owned `TypedDict` detail shapes, the per-kind validators and `mechanism` subsets, the `azure` `ExecReceiptDescriptor`, exec-receipt encode/extract, the Scenario A compatibility record, the stored-receipt admission (twelve exec kinds plus `testcontainer-run`). |
| `controller.py` | 1 | The platform ports: `ContainerAppsReplicaController` (label-URL addressing, role-revocation partition as the P3 primary, grace-0 `revision deactivate` as the secondary), `RoleRevocationPartition` (the kept-open-session sequence from platform facts §4.2), `PostgresEvidenceObserver` (the database facts the probes score), the probe topology (`Multiple` mode, affinity `none`, 50/50 labels) and the 240 s ingress constant. |
| `evidence.py` | 2 | Tier-3 projections of `az` / KQL / Resource Graph JSON and of the driver's P3/P4 observation documents onto the detail shapes; the receipt store (`<dir>/<sha256>.json`, 0600, plus `index.json` in the shared `ReceiptIndexRow` shape); `bundle_check`, which binds the shared `testcontainer_run_gate` exactly as ECS `evidence.py` does. |

`controller.py` and `evidence.py` are the re-targeted modules (plan §8.2):
they meet the real control plane and log tables at the first live run and
carry the slippage budget.

## Receipt honesty rules the validators enforce

- Every kind's `details` is a closed field set with a `mechanism` from a closed
  enum; a receipt cannot claim more than the tree proves.
- The replica kinds are re-admitted through the shared `ProbeResult`: P4b is
  `owner_affine` and `cannot_pass` by construction; a P3 whose owner row read
  `stopped`/`draining` is downgraded to `graceful_stop` and refused as a pass.
- `verify-connection-budget` wraps the shared budget validator under
  `elspeth.postgres-flexible-connection-budget.v1`.
- `testcontainer-run` is stored under
  `elspeth.azure-container-apps-testcontainer-run.v1`; `bundle_check` refuses
  the bundle unless exactly one passing run is on record.
- No receipt kind claims that a live acceptance ran: the kinds are what a run
  *would* record; the first live run is 6b-7's and needs an operator-owned
  subscription.

## Not reproduced from ECS (plan §5)

Metadata-endpoint identity (no endpoint on Container Apps — the binding is
control-plane verified instead), the operator-metric round trip, the gate
ledger and HMAC approvals, the fourteen-surface orphan sweep, Textract /
Bedrock lanes, Scenario B and the multi-arch smoke.
