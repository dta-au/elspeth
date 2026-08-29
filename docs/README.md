# ELSPETH Documentation

Index of the documentation shipped in this repository.

**Framework status:** `0.7.2`
**Archive note:** current release, architecture, contract, guide, reference, and
runbook docs remain visible here. Implemented plans, superseded specs, generated
review sidecars, and other internal work product are removed from active public
docs once they stop being current. Maintainer checkouts may keep those files in
a local ignored `docs-archive/`; public provenance remains available through git
history.

---

## Start Here

| You are... | Read this first |
| ---------- | --------------- |
| New to ELSPETH | [Your First Pipeline](guides/your-first-pipeline.md) then [User Manual](guides/user-manual.md) |
| Deploying a new AWS stack | [AWS ECS Cold Install](runbooks/aws-ecs-cold-install.md), then the [Terraform package reference](../deploy/aws-ecs/terraform/README.md) |
| Building or operating pipelines | [Configuration Reference](reference/configuration.md), [Runbooks](runbooks/index.md), and [Troubleshooting](guides/troubleshooting.md) |
| Investigating audit data | [Landscape MCP Analysis](guides/landscape-mcp-analysis.md) and [Architecture Overview](../ARCHITECTURE.md) |
| Developing plugins | [Data Trust and Error Handling](guides/data-trust-and-error-handling.md), [Plugin Development Guide](../PLUGIN.md), then [Plugin Protocol](contracts/plugin-protocol.md) |
| Contributing to the codebase | [Contributing](../CONTRIBUTING.md) |
| Evaluating ELSPETH | [Composer Guide](release/composer-guide.md), [Platform Architecture](release/platform-architecture.md), and [Audit and Lineage Guarantees](release/guarantees.md) |
| Reviewing delivery confidence and decisions | [Project Control](project-control/README.md) — the control registers are maintained by the project but not published in the repository |

---

## Project Control

ELSPETH uses a lean four-document control set, not a full project-management
method: a Project Control Report with supporting T&M, RAID, and milestone and
forecast registers. They are maintained by the project but are not published
in this repository; [docs/project-control/README.md](project-control/README.md)
explains the arrangement and how to request them.

---

## Architecture

Current architecture and design references.

- [Repository Directory Strategy](repository-structure.md) — purpose of every top-level folder and where new files belong
- [Maintainer Toolchain](maintainer/toolchain.md) — how the maintainer's own agents work (tracker, code map, delegation); not a requirement for contributors
- [Architecture Overview](../ARCHITECTURE.md) — C4 model, data flows, and system-level orientation
- [System Overview](architecture/overview.md) — compatibility pointer to the maintained root architecture overview
- [Requirements Matrix](architecture/requirements.md) — compatibility pointer to current requirement and contract sources
- [Subsystems](architecture/subsystems.md) — compatibility pointer to current subsystem diagrams and ADRs
- [Token Lifecycle](architecture/token-lifecycle.md) — row identity through forks and joins
- [State Engine](architecture/state_engine/README.md) — canonical durable scheduler, barrier, sink-effect, proof-catalog, and assessment authority
- [DAG Information and Completeness](architecture/dag/README.md) — live criteria, executable scenario evidence, current verdict, and delivery ownership
- [Landscape System](architecture/landscape.md) — audit trail architecture
- [Landscape Entry Points](architecture/landscape-entry-points.md) — where audit records are created
- [Barrier Machinery](architecture/barrier-machinery.md) — aggregation and coalesce as structural twins; paired-surfaces table and paired-change checklist
- [LLM Compatibility Gateway](../gateway/README.md) — the standalone `elspeth-llm-gateway` service: a strict OpenAI Chat Completions subset over an organisation's own invoke API, deployed separately from ELSPETH
- [ADR Index](architecture/adr/README.md) — accepted architecture decisions

## Contracts

Formal protocol definitions and token outcome guarantees. The narrative
assurance surface is [`release/guarantees.md`](release/guarantees.md); the
documents in this section formalise specific contracts that the engine, plugin
authors, and integrators must uphold.

- [Plugin Protocol](contracts/plugin-protocol.md)
- [System Operations](contracts/system-operations.md)
- [Execution Graph](contracts/execution-graph.md)
- [Token Outcome Assurance](contracts/token-outcomes/README.md)

## Guides

Tutorials and operator/developer how-to material.

- [Your First Pipeline](guides/your-first-pipeline.md)
- [User Manual](guides/user-manual.md)
- [Web Composer in One Hour — training plan (draft)](guides/composer-training-one-hour.md)
- [Test System](guides/test-system.md)
- [Data Trust and Error Handling](guides/data-trust-and-error-handling.md)
- [Telemetry Guide](guides/telemetry.md)
- [Tier-2 Tracing](guides/tier2-tracing.md)
- [Landscape MCP Analysis](guides/landscape-mcp-analysis.md)
- [Troubleshooting](guides/troubleshooting.md)
- [Docker](guides/docker.md)
- [Deployment Platforms](reference/deployment-platforms.md)

## Reference

Lookup material for configuration, tools, and plugin-specific behavior.

- [Configuration Reference](reference/configuration.md)
- [Environment Variables](reference/environment-variables.md)
- [Composer Tools](reference/composer-tools.md)
- [ChaosLLM](reference/chaosllm.md)
- [ChaosLLM MCP Server](reference/chaosllm-mcp.md)
- [Web Scrape Transform](reference/web-scrape-transform.md)

## Operations

Runbooks and production procedures.

- [Runbook Index](runbooks/index.md)
- [Resume Failed Run](runbooks/resume-failed-run.md)
- [Investigate Routing](runbooks/investigate-routing.md)
- [Incident Response](runbooks/incident-response.md)
- [Database Maintenance](runbooks/database-maintenance.md)
- [Backup and Recovery](runbooks/backup-and-recovery.md)
- [Configure Key Vault Secrets](runbooks/configure-keyvault-secrets.md)
- [Ansible Ubuntu Deployment](runbooks/ansible-ubuntu-deployment.md)
- [Caddy Development Install Refresh](runbooks/caddy-development-refresh.md)
- [AWS ECS Cold Install](runbooks/aws-ecs-cold-install.md)
- [AWS ECS Existing-Service Redeploy](runbooks/aws-ecs-existing-service-redeploy.md)
- [AWS ECS Full Disposable Acceptance](runbooks/aws-ecs-deployment.md)

## Release History

Audience-facing release and evaluation documents. See the
[release docs README](release/README.md) for the full index.

- [Composer Guide](release/composer-guide.md) — current user-facing guide to the web authoring surface
- [Platform Architecture](release/platform-architecture.md) — current platform architecture, trust-boundary, and operational-responsibility overview
- [Audit and Lineage Guarantees](release/guarantees.md) — long-lived assurance narrative; refreshed per release (current contract surface; §1–§10 RC-3 base, §11–§14 RC-5.2 additions)
- Per-period progress and velocity reports (RC-1 to RC-5) are internal work
  product and no longer ship as active public docs.
- Superseded RC snapshots such as `feature-inventory.md`,
  `rc4-executive-brief.md`, `rc-3-release-notes.md`, and
  `rc-2-checkpoint-fix-postmortem.md` are historical context only and are
  available through git history or maintainer-local archives.

## Historical Snapshots

Intentional point-in-time documents are retained through git history and, for
maintainers, optional local ignored archives. They are not part of the active
public docs index because they describe superseded implementation details.
