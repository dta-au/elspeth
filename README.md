# ELSPETH

**E**xtensible **L**ayered **S**ecure **P**ipeline **E**ngine for **T**ransformation and **H**andling

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
![Status: 0.8.9](https://img.shields.io/badge/status-0.8.9-green.svg)

ELSPETH builds, validates, runs, and audits data and LLM workflows whose
outputs need to be reviewed and explained. You can author a pipeline in
version-controlled YAML or through the authenticated Web Composer. Both paths
use the same plugin contracts, graph validation, execution engine, and
Landscape audit trail.

> **Pre-release status:** ELSPETH may be suitable for carefully evaluated,
> use-case-specific applications, but it is not yet ready for general production use.
> Before relying on it, validate ELSPETH against your requirements and risk controls.
>
> **AI-generated code:** ELSPETH is a rapid prototype, generated and reviewed by AI,
> that is in active development with major systems subject to change. Limited user
> testing has been conducted on a small set of use cases.
>
> **Chroma security advisory:** Do not use Chroma with ELSPETH until upstream
> publishes a fixed release and ELSPETH adopts and verifies it. This includes
> the `rag_retrieval` transform with `provider: chroma`, the `chroma_sink` sink,
> and Chroma examples in all connection modes. ChromaDB has unresolved code-execution vulnerabilities;
> version 1.5.9 is still affected. See the
> [pre-authentication advisory](https://github.com/advisories/GHSA-f4j7-r4q5-qw2c),
> [collection-update advisory](https://github.com/advisories/GHSA-36p7-vc44-83pf),
> and [upstream server and client issue](https://github.com/chroma-core/chroma/issues/6717).

## What it does

- **Builds explicit pipelines.** Sources, transforms, gates, barriers, and sinks
  form a directed graph with named edges and declared data contracts.
- **Validates before execution.** ELSPETH checks settings, plugin declarations,
  graph structure, routes, and compatible schemas. Runtime checks also hold
  plugins to declarations that validation relied on.
- **Records what happened.** Landscape stores source and token lineage,
  external calls, routing, terminal outcomes, and output evidence. Logs and
  telemetry support operations; they do not replace the audit record.
- **Supports recovery.** Durable work records, checkpoints, and sink effect
  protocols support interrupted runs within their documented limits.
- **Offers two authoring paths.** YAML supports direct review and version
  control. The Web Composer uses a provider-driven tool loop to propose changes
  to versioned session state, with validation and review before execution.

```mermaid
flowchart LR
    YAML["YAML + CLI"] --> V["Settings, plugins and graph validation"]
    WEB["Web Composer"] --> V
    V --> E["Engine and plugins"]
    E --> L["Landscape audit and lineage"]
```

The web and CLI paths converge at runtime assembly and execution. They do not
yet run a shared, persisted compiled-pipeline artifact. The
[architecture guide](ARCHITECTURE.md) describes the current boundaries.

## Try a YAML pipeline

You need Python 3.12 or newer and [uv](https://docs.astral.sh/uv/). From a
source checkout:

```bash
git clone https://github.com/dta-au/elspeth.git
cd elspeth
uv sync --frozen
uv run elspeth validate --settings examples/threshold_gate/settings.yaml
uv run elspeth run --settings examples/threshold_gate/settings.yaml --execute
```

The example writes its output under `examples/threshold_gate/output/` and its
Landscape database at the path configured in its settings file. The
[first-pipeline guide](docs/guides/your-first-pipeline.md) walks through the
configuration, results, and lineage. Use `uv run elspeth plugins list` to see
the registered plugins in your checkout.

## Try the Web Composer

The Web Composer adds a FastAPI service and React frontend for authenticated
authoring, validation, review, execution, and run inspection. Its LLM planner
needs a configured model provider; a local login alone does not configure one.
Follow the [local web setup guide](docs/guides/web-local-development.md) for
dependencies, development credentials, model settings, and startup. For a
user walkthrough, see the [Composer guide](docs/release/composer-guide.md).

## Audit and trust

Landscape is the runtime evidence store. ELSPETH distinguishes engine-owned
audit and control records, validated pipeline rows, and untrusted external
input. Source data and provider responses are validated at their entry
boundaries; corruption of audit records fails closed. See
[data trust and error handling](docs/guides/data-trust-and-error-handling.md)
and the [current audit and lineage guarantees](docs/release/guarantees.md).

ELSPETH supports audit export and optional signing. The
[export settings and limits](docs/reference/configuration.md#export-settings)
describe what is exported and how to configure it.

## What changed in 0.8.9

0.8.9 is unreleased. Full-replacement refusals and incremental output refusals
retain their distinct candidate and saved-graph meanings, safe path diagnostics
and repair guidance. Shared-link failures preserve the requested link instead
of opening the recipient's workspace. Configured container token ceilings are
provisioned before serving; configured container storage ceilings bound aggregate
retained and reserved bytes. Automatic title
requests disable the adapter and SDK retry paths. See the [release notes](CHANGELOG.md).

Primary Composer and advisor requests use independently configured Azure AI
Content Safety Prompt Shields, with required fail-closed protection by default.
See [guard setup](docs/reference/composer-prompt-guard.md) for input limits and
failure behavior. Advisor checkpoints retain complete mandatory evidence; this
can increase request cost and latency.

The Azure Container Apps first-install instructions select the GHCR account
explicitly, separate model routing and pricing identities, export the transport
ceiling, and mount both required authoring guards through versioned secret
references. ACA startup and doctor require one `sslmode=verify-full` and one
`sslrootcert` set to `system` or an absolute CA-file path. Duplicate TLS
parameters, legacy `requiressl` and competing GSS encryption options are refused;
correctly URL-escaped credentials remain supported.

Doctor measures TLS on its initial schema-inspection connections and blocks
schema initialization when preflight fails. Separate initialization, final
verification and later web connections are outside that measurement. Offline
instruction rehearsal does not establish live cloud acceptance. These ACA
enhancements are attributed to 0.8.8 within the combined 0.8.9 candidate.

Session epoch 78 replaces session epoch 77 and retains its committed graph-review
authority while strengthening quota custody and shared rate-limit policy binding.
Landscape epoch 50 replaces epoch 49 and records durable provider-attempt identity
on run calls. Preserve epoch 75, both epoch 76 session-store contracts, epoch 77
session stores and epoch 49 Landscape stores; use a separately provisioned epoch 78
session store and epoch 50 Landscape store. Keep old workers isolated from the new stores.
Compatible stores still require exact schema admission; unsupported stores are
preserved and refused before incompatible writes. Backwards compatibility is
permitted and considered from 0.8.5 within explicitly supported boundaries. This
candidate authorizes no in-place migration, reset or downgrade. Package metadata
is 0.8.9; the package version alone does not establish qualification or image
publication. Automatic image publication from main is held; verify the release
tag and image digest before installing a published image.

Quota API quantities now use decimal strings for exact signed-64-bit values;
older numeric clients must adapt their response parsing. Proxy deployments must
explicitly configure `ELSPETH_WEB__TRUSTED_PROXY_HOSTS` with their actual numeric
forwarding peers; the default trusts no forwarding headers.

## Previously in 0.8.6

0.8.6 strengthens Composer final-reply review, retained operation outcomes,
and bounded blob inspection. Replies are reviewed against the current request,
saved changes and delivered inspection evidence, or withheld when admission
cannot be established within existing limits. Reply admission does not clear
graph or execution blockers. See the [release notes](CHANGELOG.md).

Session epoch 76 replaces session epoch 75; Landscape epoch 49 is unchanged.
Preserve epoch 75 session stores and use a separately provisioned epoch 76
session store. Keep old workers isolated from the new store. Compatible stores
still require exact schema admission; unsupported stores are preserved and
refused before incompatible writes. Backwards compatibility is permitted and
considered from 0.8.5 within explicitly supported boundaries. This candidate
authorizes no in-place migration, reset or downgrade. Package metadata is 0.8.6;
the package version alone does not establish qualification or image publication.
Automatic image publication from main is held; verify the release tag and image
digest before installing a published image. See the
[release notes](CHANGELOG.md) and [deployment runbooks](docs/runbooks/index.md)
before upgrading.

## Recovery and deployment

To resume an interrupted run, replace the ID in
`elspeth resume <run_id> --execute` with the run you intend to resume, for
example `elspeth resume abc123 --execute`. Without `--execute`,
`elspeth resume <run_id>` checks whether the run can be resumed without
changing it. See the [recovery runbook](docs/runbooks/resume-failed-run.md)
for the eligibility checks and operator procedure.

Set `IMAGE_TAG` only after confirming that the intended container tag was
published; the [Docker guide](docs/guides/docker.md) shows how to inspect it.
The [AWS ECS cold-install runbook](docs/runbooks/aws-ecs-cold-install.md)
describes the tracked Terraform package. Azure Container Apps has
Single/sticky desktop acceptance; live cloud acceptance is a separate gate.
See the [deployment platform reference](docs/reference/deployment-platforms.md)
for the precise support limits.

## Documentation

| Start here | For |
| --- | --- |
| [First pipeline](docs/guides/your-first-pipeline.md) | YAML walkthrough and result inspection |
| [Local web setup](docs/guides/web-local-development.md) | Web Composer development setup |
| [User manual](docs/guides/user-manual.md) | Product workflows and commands |
| [Architecture](ARCHITECTURE.md) | Components, boundaries, stores, and execution paths |
| [Plugin guide](PLUGIN.md) | Writing and testing plugins |
| [Configuration](docs/reference/configuration.md) and [environment variables](docs/reference/environment-variables.md) | Settings and deployment options |
| [Deployment platforms](docs/reference/deployment-platforms.md) | Supported profiles and acceptance limits |
| [Release notes](CHANGELOG.md) and [roadmap](ROADMAP.md) | Changes and planned work |

ELSPETH includes a separate [LLM compatibility gateway](gateway/README.md)
for certain provider integrations. It deploys independently from the engine.

## Contributing and support

See [CONTRIBUTING.md](CONTRIBUTING.md) for development checks and pull request
guidance, [GOVERNANCE.md](GOVERNANCE.md) and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for project participation,
[SECURITY.md](SECURITY.md) for private vulnerability reporting, and
[SUPPORT.md](SUPPORT.md) for help channels and current support limits.

ELSPETH is available under the [MIT License](LICENSE).
