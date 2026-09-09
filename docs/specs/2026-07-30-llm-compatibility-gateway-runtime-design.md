# LLM Compatibility Gateway Runtime Design

**Status:** Approved design
**Date:** 2026-07-30
**Decision owner:** Project developer
**Related design:** [ELSPETH LLM Gateway Integration and AWS Scenario C](2026-07-30-elspeth-llm-gateway-integration-design.md)

## Summary

Create a purpose-built, independently versioned `elspeth-llm-gateway` container and Python adapter SDK. The gateway presents a strict, non-streaming OpenAI Chat Completions subset to ELSPETH and translates it through a pinned agency adapter to a custom upstream invocation API such as `POST /v1/invoke`. The gateway, rather than ELSPETH or the adapter, owns OAuth2 client-credentials acquisition, caching, refresh, and sanitized failure handling.

This is a supported compatibility pattern for deployments whose LLMs sit behind agency-specific gateways. It is deliberately "for but not with": the maintained product supplies the runtime, adapter contract, fictional reference implementation, conformance suite, and deployment seam, but does not contain a real agency schema, endpoint, credential, or certification claim.

## Problem

ELSPETH's current Composer calls LiteLLM directly and its pipeline LLM transform has concrete Azure OpenAI, OpenRouter, and Amazon Bedrock providers. Those paths can use OpenAI-compatible endpoints only when the endpoint already implements the expected Chat Completions schema and accepts a directly usable bearer credential. They do not supply:

- a custom `/v1/invoke` request and response translation layer;
- OAuth2 `client_credentials` token acquisition and refresh;
- a stable adapter ABI for agency-owned schema variants;
- one shared route for Composer tool calls and pipeline structured output; or
- an independently deployable security boundary around those responsibilities.

LiteLLM custom-provider hooks and LiteLLM Proxy were considered, but neither makes the adapter, OAuth lifecycle, fail-closed contract, audit boundary, or AWS admission requirements first-class ELSPETH product surfaces.

## Goals

- Give Composer and the pipeline `llm` transform one stable HTTP contract for custom upstream gateways.
- Support text completion, Composer tool calls and tool results, JSON object output, and JSON Schema output.
- Support OAuth2 client credentials using `client_secret_basic` and `client_secret_post`.
- Keep upstream schema translation in versioned Python adapters delivered in immutable derived images.
- Fail explicitly when a model, field, capability, contract version, or response shape is unsupported.
- Prevent prompts, responses, tool arguments, credentials, or tokens from entering gateway logs.
- Provide a deterministic local stack that an agency can start quickly and use as an adapter template.
- Publish conformance tests that can qualify a derived image without access to ELSPETH internals.

## Non-goals

- Implementing or testing against a real agency adapter in the first delivery.
- Storing an agency endpoint, schema sample, credential, model identifier, or response in this repository.
- Streaming, embeddings, image, audio, batch, fine-tuning, or Assistants APIs.
- Claiming full OpenAI API compatibility; the supported surface is the closed subset in this design.
- OAuth authorization code, device code, refresh-token grants, mTLS, `private_key_jwt`, token exchange, or non-standard token parameters.
- Multi-tenant routing, per-user upstream identities, dynamic adapter loading, or mounted executable code.
- Retrying ordinary rate limits, timeouts, network failures, or upstream 5xx responses inside the gateway.
- Routing evaluation, lint-judge, or release-signing model calls through this interface in the first release.
- Certifying a derived image for an agency. The agency owns its adapter code, upstream contract, deployment approval, and operational qualification.

## Architecture

```text
Composer / pipeline LLM
        |
        | OpenAI Chat subset + static bearer
        v
+-----------------------------+
| elspeth-llm-gateway core    |
| - request validation        |
| - model alias allowlist     |
| - OAuth token lifecycle     |
| - bounded HTTP transport    |
| - safe errors and metadata  |
+--------------+--------------+
               |
               | validated adapter request + OAuth bearer
               v
+-----------------------------+
| pinned Python adapter       |
| - custom request mapping    |
| - custom response parsing   |
| - upstream error mapping    |
+--------------+--------------+
               |
               | agency-specific POST /v1/invoke
               v
       agency LLM gateway
```

One gateway process serves one adapter and one agency trust domain. It may expose several operator-published model aliases, but it is not a general multi-tenant router. Agencies create immutable derived images containing an exact adapter package version. Runtime code mounts and package installation at container start are forbidden.

### Responsibility split

| Concern | Owner |
|---|---|
| OpenAI-subset validation and normalization | Gateway core |
| Static inbound bearer authentication | Gateway core |
| Model alias allowlist and configured target lookup | Gateway core |
| OAuth token request, cache, refresh, and one 401 replay | Gateway core |
| Upstream URL policy, limits, timeout, and HTTP transport | Gateway core |
| Custom invoke body and safe non-secret headers | Adapter |
| Custom success-body parsing and error classification | Adapter |
| Prompt/response audit record | ELSPETH Landscape |
| Adapter implementation and upstream qualification | Agency-derived image owner |

The adapter never performs network I/O, reads credentials, acquires tokens, logs content, or chooses an arbitrary upstream host. It returns a relative path, bounded non-secret headers, and a JSON body to the gateway core. The core joins the path to the configured HTTPS origin, rejects an adapter-supplied `Authorization` header, injects the OAuth bearer, performs the request, and passes the bounded response to the adapter parser.

## Versioned inbound contract

The first contract major is `1`. Every `POST /v1/chat/completions` request carries:

```text
Authorization: Bearer <deployment-static-token>
Content-Type: application/json
X-ELSPETH-LLM-Gateway-Contract: 1
```

The gateway returns the same contract header. A missing, malformed, or unsupported major fails before adapter execution. Minor additions may only be optional and backward compatible; removing or reinterpreting a field requires a new major. The gateway container version, contract major, adapter API major, and adapter package version are separate identities.

The gateway accepts or generates a bounded `X-Request-ID` and echoes the safe value in responses. It does not trust caller-supplied tracing or forwarding headers.

The Chat Completions endpoint accepts exactly one deployment-static bearer at a time. It is supplied only through the runtime secret source, must meet a minimum entropy/length policy, and is compared in constant time after strict `Authorization` header parsing. Query-parameter, cookie, and alternate-header credentials are rejected. Rotation installs a new secret version and replaces the task; overlapping old/new bearer windows are deferred until there is a demonstrated operational need.

### Request subset

The request is a JSON object with these supported fields:

| Field | Contract |
|---|---|
| `model` | Required operator-published logical alias. Arbitrary upstream identifiers are rejected. |
| `messages` | Required non-empty ordered messages using `system`, `user`, `assistant`, or `tool`. |
| `temperature` | Optional finite number within the configured supported range. |
| `seed` | Optional integer, accepted only when the adapter declares `seed`. |
| `max_tokens` | Optional positive integer within gateway bounds. |
| `tools` | Optional OpenAI function-tool definitions, accepted only with `tools` capability. |
| `tool_choice` | Optional `auto`, `none`, `required`, or one explicitly named declared function. |
| `response_format` | Optional `json_object` or `json_schema`, accepted only with the matching capability. |

Assistant messages may contain normalized function tool calls. Tool messages must name the originating `tool_call_id`. Text content is a string in contract major 1; multimodal content parts are unsupported. Only `n=1` behavior exists. `stream`, `logprobs`, arbitrary vendor options, and every unknown top-level or nested contract field are rejected rather than ignored.

Request body, message count, tool count, individual string length, schema size/depth, and total response bytes are bounded by core configuration. Invalid UTF-8, duplicate JSON keys, non-finite numbers, and content outside those bounds fail before the adapter runs.

### Response subset

A successful response is an OpenAI-shaped JSON object containing:

- a gateway-generated response `id` and `created` timestamp;
- `object: "chat.completion"`;
- the configured logical `model` alias;
- exactly one choice with index `0`;
- an assistant message containing either text content or normalized tool calls;
- a normalized `finish_reason`; and
- usage counts when the adapter declares and supplies the `usage` capability.

Usage is never estimated or invented. If a caller profile requires `usage` and the adapter cannot produce validated counts, the request fails with `upstream_response_invalid`. When usage is not required and the adapter lacks that capability, the response omits usage and ELSPETH records it as unavailable rather than zero.

The allowed finish reasons in major 1 are `stop`, `length`, `tool_calls`, and `content_filter`. An unrecognized upstream reason is not silently converted to `stop`; the adapter must map it deliberately or the gateway rejects the response.

## Capability contract

Every adapter declares a closed capability set from:

```text
text
tools
json_object
json_schema
seed
usage
```

`text` is mandatory. The gateway checks request features against the declared set on every call. ELSPETH profiles also declare the capabilities they require, allowing startup and runtime preflight to fail before row processing or an interactive Composer turn.

`GET /healthz` is a content-free liveness check. `GET /readyz` validates static configuration, readable secret injection, model mappings, adapter import and self-validation, and contract compatibility without acquiring an OAuth token or calling the agency endpoint. Its bounded JSON response contains only:

- readiness state and contract major;
- adapter name, version, adapter API major, and package fingerprint;
- declared capabilities;
- logical model aliases and a mapping-generation hash; and
- safe configuration error codes when not ready.

The Chat Completions endpoint requires the static bearer. Health endpoints do not, so an orchestrator can probe them without placing a secret in a health command; they remain network-private and disclose no endpoint or credential values. ELSPETH's runtime preflight performs the real authenticated model request needed to establish operational health.

## Adapter SDK

The Python SDK defines one versioned adapter protocol with these logical operations:

1. `descriptor()` returns immutable identity and capabilities.
2. `validate_configuration()` checks adapter-owned non-secret settings without network access.
3. `build_invoke()` maps a validated canonical request plus selected model target to a relative path, bounded safe headers, and JSON body.
4. `parse_success()` validates and converts an upstream success into the canonical response.
5. `classify_error()` converts an upstream non-success status and bounded body into a stable gateway error classification.

The core passes frozen canonical request objects rather than raw dictionaries. SDK types have closed fields and reject unknowns. An adapter cannot weaken core URL, authentication, size, timeout, logging, or response validation policy.

Adapter identity consists of a code-owned name, semantic version, adapter API major, and package fingerprint. A derived image pins the package by immutable artifact digest. The gateway reports those identities at readiness and on safe operational events; ELSPETH records them with the LLM call's forensic metadata.

## OAuth2 client credentials

The core supports RFC-style `client_credentials` token requests with exactly two client authentication methods:

- `client_secret_basic`: client ID and secret in HTTP Basic authentication;
- `client_secret_post`: client ID and secret in the form body.

Configuration includes an HTTPS token URL, client ID secret reference, client-secret reference, authentication method, optional ordered scopes, request timeout, and bounded refresh skew. The token response must contain a non-empty `access_token`, a supported bearer `token_type`, and a valid positive `expires_in`. A deployment may configure a conservative fixed lifetime only when the token service contract omits `expires_in`; that fact is visible in safe readiness metadata.

Tokens are cached in memory only. Concurrent requests use single-flight acquisition. Refresh occurs before expiry using the bounded skew. OAuth requests do not inherit adapter headers and do not follow redirects across origins. TLS verification is mandatory.

After an upstream `401`, the core invalidates the cached token, performs one single-flight refresh, and replays the invocation once. A second `401` is final. The gateway does not replay on any other status or transport failure. ELSPETH remains the only owner of ordinary rate-limit, network, server, pooling, and row-level retry policy.

No token, client secret, Authorization value, token response body, or upstream error body appears in logs or error messages. OAuth error descriptions are mapped to stable sanitized codes.

## Error contract

Failures use an OpenAI-shaped error object extended with stable machine fields:

```json
{
  "error": {
    "message": "sanitized operator-safe message",
    "type": "gateway_error",
    "code": "upstream_unavailable",
    "retryable": true,
    "request_id": "..."
  }
}
```

Contract major 1 defines at least these codes:

| Code | Typical HTTP status | Retryable |
|---|---:|---:|
| `invalid_request` | 400 | no |
| `inbound_authentication_failed` | 401 | no |
| `contract_mismatch` | 400 | no |
| `model_not_allowed` | 404 | no |
| `capability_unsupported` | 422 | no |
| `context_length_exceeded` | 400 | no |
| `content_policy_rejected` | 400 | no |
| `oauth_token_unavailable` | 503 | yes when transient |
| `upstream_unauthorized` | 502 | no after the one refresh |
| `upstream_rate_limited` | 429 | yes |
| `upstream_timeout` | 504 | yes |
| `upstream_unavailable` | 503 | yes |
| `upstream_response_invalid` | 502 | no |
| `internal_error` | 500 | no |

Adapters classify agency-specific failures into this closed set. Unknown adapter exceptions and malformed upstream responses become sanitized non-retryable failures; their raw values never cross the gateway boundary.

## Configuration and image model

The base image contains only the gateway core, SDK, and reference adapter. A production image is derived with:

- one pinned agency adapter package;
- a code-owned adapter entry point;
- immutable non-secret defaults; and
- no agency credentials or endpoint values in image layers.

Runtime configuration supplies the adapter name, HTTPS invoke origin, token endpoint, model alias mapping, timeouts, bounds, and safe OAuth options. Secrets are injected separately for the inbound bearer, OAuth client ID, and OAuth client secret. Unknown environment variables in the gateway namespace fail startup.

Model mappings are a closed mapping from logical alias to adapter-owned target data. The mapping is validated at startup and hashed into a generation identity. Requests cannot override the target, upstream path, OAuth parameters, or origin.

The container runs as a fixed non-root user with a read-only root filesystem, no Docker socket, no ELSPETH data volume, no shell requirement, and a bounded writable temporary directory only if the runtime needs one. Graceful shutdown stops admission, waits for a bounded in-flight window, clears token state, and exits.

## Logging and audit boundary

ELSPETH Landscape remains authoritative for prompts, responses, tool calls, usage, model results, retries, and row/run provenance. The gateway emits metadata-only operational events:

- request ID and canonical request/response hashes;
- contract major;
- adapter name, version, API major, and fingerprint;
- logical model alias and mapping generation;
- status, latency, response byte count, and upstream status class;
- OAuth cache hit, refresh attempt, and refresh outcome; and
- stable error code.

The gateway never logs message content, structured-output bodies, schemas, tool definitions or arguments, raw headers, query strings, request or response bodies, credentials, tokens, client IDs, client secrets, or OAuth/upstream error bodies. Canonical hashes permit correlation without making the gateway a second content audit store.

## Quick-start reference stack

The maintained distribution includes a development-only local stack that starts:

- the base gateway with a fictional `reference_v1_invoke` adapter;
- a deterministic mock OAuth token endpoint;
- a deterministic mock `/v1/invoke` upstream; and
- conformance probes for text, JSON Schema output, and one tool-call/tool-result round trip.

The fictional schema is intentionally documented and visibly non-agency. The adapter source highlights the translation points an agency must replace. A scaffold command or template creates a new adapter package with descriptor, configuration, request, response, error, and test modules. The local stack is for development and qualification; the supported production deployment in this delivery is ELSPETH AWS Terraform Scenario C.

The onboarding path is:

1. Start the reference stack and run the conformance suite unchanged.
2. Generate or copy the adapter scaffold.
3. Implement mappings against locally supplied sanitized fixtures.
4. Build an immutable derived image with a pinned package.
5. Run the complete conformance suite against the derived image.
6. Publish the image by digest and admit it through Scenario C's provenance gate.

## Verification and acceptance

The gateway delivery is incomplete unless automated tests cover:

- strict request and response schemas, unknown-field rejection, and bounds;
- all declared capabilities and capability-mismatch failures;
- text, JSON object, JSON Schema, tool call, and tool result normalization;
- model alias allowlisting and mapping generation;
- both OAuth client-authentication methods;
- expiry, refresh skew, single-flight acquisition, and concurrent refresh;
- exactly one token refresh and replay after upstream `401`;
- no replay for rate limit, timeout, network, or 5xx failures;
- token, secret, prompt, response, schema, tool-argument, and raw-error non-leakage;
- adapter attempts to set Authorization, escape the configured origin, or exceed bounds;
- graceful shutdown and in-flight bounds;
- `/healthz`, `/readyz`, contract mismatch, and actual-model preflight behavior; and
- a derived-image conformance run using the fictional adapter.

The conformance kit is versioned with the contract major. A derived adapter passes only if every mandatory test for its declared capabilities passes. Capability omission is permitted; claiming and failing a capability is not.

## Versioning and rollout

- Gateway runtime releases use semantic versions and publish immutable image digests.
- The adapter SDK has its own semantic version and explicit adapter API major.
- The HTTP contract uses an explicit major beginning at `1`.
- ELSPETH pins a supported contract-major range and fails closed outside it.
- Derived images pin both gateway runtime and adapter package identities.
- Contract fixtures and JSON Schemas are ordinary runtime compatibility assets, not signed design artifacts.

The first production use should begin with the fictional adapter in an isolated Scenario C deployment, then a locally developed adapter against sanitized fixtures, and only then an agency-controlled qualification environment. No stage requires ELSPETH maintainers to receive the agency's live credentials or proprietary payloads.

## Alternatives considered

### Configure an OpenAI base URL directly

Rejected because it does not translate a custom invoke schema or acquire OAuth client-credentials tokens.

### Implement a LiteLLM `CustomLLM` inside ELSPETH

Rejected because it couples agency code and OAuth lifecycle to every ELSPETH process, expands the web trust boundary, and does not provide an independently admitted deployment artifact.

### Deploy LiteLLM Proxy

Rejected for the first supported pattern because ELSPETH would still need to own custom adapter packaging, OAuth behavior, strict capability/error semantics, and audit-safe AWS admission around a broader proxy product.

### Make mappings declarative

Rejected because agency request, response, tool, and error shapes can require real validation and control flow. Versioned Python adapters are testable and fail closed without turning configuration into an unsafe programming language.
