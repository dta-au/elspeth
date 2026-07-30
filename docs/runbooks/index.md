# Runbooks

Operational procedures for ELSPETH pipeline management.

---

## Quick Reference

| Runbook | When to Use |
|---------|-------------|
| [Resume Failed Run](resume-failed-run.md) | Pipeline crashed or was interrupted |
| [Investigate Routing](investigate-routing.md) | Need to explain why a row was routed |
| [Scheduler Lease Recovery](scheduler-lease-recovery.md) | Token work items stuck `leased`, SCREAM invariant fired, `attempt` churn from lease expiries, or (N>1) dead-leader takeover, a wedged lock-holder, or follower recovery |
| [Sink Effect Recovery](sink-effect-recovery.md) | Sink publication is response-lost, an effect lease expired, reconciliation is `UNKNOWN`, or a successor is blocked |
| [Database Maintenance](database-maintenance.md) | Audit DB growing large, need cleanup |
| [Incident Response](incident-response.md) | Production issue needs investigation |
| [Backup and Recovery](backup-and-recovery.md) | Backup audit trail, restore from backup |
| [Deployment Platforms](../reference/deployment-platforms.md) | Choose a maintained Compose, AWS ECS, native Linux, or Azure Ubuntu VM path; Kubernetes is BYO and Azure Container Apps is deferred |
| [Native Linux and Azure Ubuntu VM Deployment](ansible-ubuntu-deployment.md) | Install one systemd-managed web process on Ubuntu; Azure may use one VM behind Front Door |
| [Caddy Development Install Refresh](caddy-development-refresh.md) | Rebuild the frontend and restart the repository-specific source-checkout service behind Caddy |
| [AWS ECS Cold Install](aws-ecs-cold-install.md) | Create a complete disposable stack with the tracked Scenario A Terraform package, including Aurora, monitoring, and Bedrock |
| [AWS ECS Existing-Service Redeploy](aws-ecs-existing-service-redeploy.md) | Build, scan, and deploy an immutable image to an existing ECS/Fargate service |
| [AWS ECS Full Disposable Acceptance](aws-ecs-deployment.md) | Provision, exercise, and destroy the release-specific two-scenario acceptance environment |
| [AWS ECS Bedrock Opus and Sonnet](aws-ecs-bedrock-opus-sonnet.md) | Configure and validate operator-approved Bedrock Opus and Sonnet profiles on ECS |
| [Audit Tier-1 Violation](audit-tier1-violation.md) | Compose-loop audit counters or audit-grade transcript logging fail |

---

## Common Tasks

### Check Pipeline Status

```bash
# Validate configuration
elspeth validate --settings pipeline.yaml

# List recent runs
sqlite3 runs/audit.db "SELECT run_id, status, started_at FROM runs ORDER BY started_at DESC LIMIT 10;"
```

### Quick Health Check

```bash
elspeth health --verbose
```

### View Available Plugins

```bash
elspeth plugins list
```

---

## Emergency Contacts

> **⚠️ Customize This Section:** Replace these generic contacts with your organization's actual contacts before deploying these runbooks.

| Issue | Contact |
|-------|---------|
| Pipeline failures | On-call engineer (e.g., PagerDuty, Slack #oncall) |
| Data integrity concerns | Data team lead |
| Audit trail questions | Compliance team |

---

## See Also

- [Configuration Reference](../reference/configuration.md)
- [Docker Guide](../guides/docker.md)
- [Your First Pipeline](../guides/your-first-pipeline.md)
