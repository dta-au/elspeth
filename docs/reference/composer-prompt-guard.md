# Composer prompt guard

Version 0.8.9 uses Azure AI Content Safety Prompt Shields in front of Composer
and advisor model requests. Detection belongs to Azure; ELSPETH sends the
supported input, enforces its result and records a sanitized authoring audit.
Graph validation, independent advisor review, credential redaction and operator
approvals still apply.

## Operator configuration

Configure each role independently with an HTTPS Content Safety resource origin
and a dedicated subscription key. These settings are read once at Web startup.
Neither role inherits the other role's guard or any model-provider credential.

| Environment variable | Value |
| --- | --- |
| `ELSPETH_WEB__COMPOSER_PROMPT_GUARD` | JSON object for primary Composer requests |
| `ELSPETH_WEB__COMPOSER_ADVISOR_PROMPT_GUARD` | Independent JSON object for advisor requests |
| `ELSPETH_WEB__COMPOSER_PROMPT_GUARD_MODE` | `required` by default; explicit `debug-when-unconfigured` for development |

Each guard object has exactly these fields:

| Field | Meaning |
| --- | --- |
| `adapter` | `azure_prompt_shields`; may be omitted because it is the only 0.8.9 adapter |
| `endpoint` | HTTPS resource origin, such as `https://example-resource.cognitiveservices.azure.com`; no path, userinfo, query or fragment |
| `subscription_key` | Required subscription credential, injected from the deployment's secret store |
| `timeout_seconds` | Positive number, at most 60 seconds; defaults to 10 seconds shared by all Azure batches for one inspection |

For example, the object shape is:

```json
{
  "adapter": "azure_prompt_shields",
  "endpoint": "https://example-resource.cognitiveservices.azure.com",
  "subscription_key": "replace-from-secret-store",
  "timeout_seconds": 10.0
}
```

Supply each JSON object through the deployment's existing secret injection
mechanism. Do not commit a populated object or put the credential in pipeline
options. A literal JSON `null` clears that role's mount. The application may
load with an unconfigured mount, but protected authoring is refused in the
default `required` mode. Operators upgrading existing installations must
configure both mounts before using protected authoring.

`debug-when-unconfigured` explicitly permits a missing mount and records
`debug_bypass`. That state is unprotected. It never bypasses a configured
guard's block, timeout, service failure or invalid response. This is a separate
Web configuration key; signing/linter modes and raw-secret development flags
do not control it.

## Input and failure behavior

The adapter uses the
[standalone Prompt Shields 2024-09-01 API](https://learn.microsoft.com/en-us/rest/api/contentsafety/text-operations/shield-prompt?view=rest-contentsafety-2024-09-01).
Genuine latest-user text goes into `userPrompt`; untrusted graph and tool text
goes into `documents`. Trusted deployment instructions and exact registered
catalog references remain outside attack classification. User, graph, model
and tool argument values remain untrusted. Credentials are screened before
guard egress.

Azure accepts a prompt of at most 10,000 characters and at most five documents
totaling 10,000 characters per call. Owned producers retain complete semantic
units of structured context and decoded tool arguments. Their concatenation
must exactly match the original provider text; the metadata never reaches the
model provider. ELSPETH packs complete units and history messages into bounded
documents, requires every batch to pass, and never cuts a string value or
truncates history to fit. The logical check accepts at most 128 packed documents
of at most 10,000 characters each, plus the user prompt. Its finite local UTF-8
envelope is derived from those limits: `(128 + 1) × 10,000 × 4` = **5,160,000
bytes**, including document framing. This logical envelope is separate from
Azure's per-call limits; it does not guarantee process memory use or model
context capacity. An individual oversized user prompt or atomic value,
unsupported image/audio/file input, invalid Unicode or excessive document count
is refused, including in debug mode. These are explicit coverage limits, not
clean verdicts. See
[Azure input limits](https://learn.microsoft.com/en-us/azure/ai-services/content-safety/region-availability).

Larger inputs require more sequential Azure batches, each repeating the complete
user prompt. All Azure batches for one inspection share the configured timeout:
it defaults to 10 seconds and permits at most 60 seconds. Request preparation
and required audit settlement are outside that inspection interval. All batches
must pass within it; a large supported input can still time out and refuse dispatch.
Inspection can incur Azure work even if later model admission refuses. The
byte envelope does not alter credential screening or other resource limits.
Both configured and unconfigured policy fingerprints bind the numerical input
limits. Historical audits retain their original policy and request hashes.

A detected attack blocks the protected model request. A missing/malformed
verdict, document-analysis count mismatch, HTTP/network error, timeout or
response exceeding 65,536 bytes makes the guard unavailable and also refuses
dispatch. The client does not follow redirects, retry, rewrite content or fall
back to a local detector. A separate authoring audit records the decision;
raw input, provider error bodies and credentials are excluded from that audit.

The historical advisor regex prescan no longer produces decisions. Existing
prescan audit records remain readable. A service block preserves existing
graph state; it does not undo valid work committed earlier in the request.
When a new session needs a generated title, its protected title call finishes
before primary composition and consumes the same admitted operation deadline.
A title guard refusal stops that turn before primary work begins.

Sessionless startup model probes skip paid verification because they have no
durable authoring lifecycle for the guard audit. The standalone AWS ECS
`verify-bedrock` model probe also refuses with
`bedrock_prompt_guard_audit_unavailable` until it has that custody. Neither
probe qualifies model access, and the debug mode does not grant an audit
exemption. Separate runtime Bedrock guardrail checks keep their Landscape
audit lifecycle.

## Support boundary

0.8.9 implements Azure text checking with subscription-key authentication.
Bedrock and GCP adapters and open integrations remain reserved. Their earlier
planning labels, 0.8.8 and 0.8.9 respectively, do not make those APIs
configurable in release 0.8.9. Runtime graph prompt-shield/content-safety transforms are configured
separately and are not executed by this authoring mount.

An API version identifies the Azure protocol, not immutable detector model
bytes. Offline contract tests verify integration and refusal behavior; they
do not establish detector accuracy, false-positive rates or deployment health.
Qualify the configured resource separately with benign and attack controls
before relying on its protection.
