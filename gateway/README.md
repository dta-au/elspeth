# ELSPETH LLM compatibility gateway

A strict OpenAI Chat Completions-subset gateway that translates canonical
chat requests into an agency's own custom `invoke` API, acquires OAuth2
client-credentials tokens on the agency's behalf, and never logs message
content, credentials, or raw upstream bodies. See
`docs/superpowers/specs/2026-07-30-llm-compatibility-gateway-runtime-design.md`
in the main ELSPETH repository for the full design.

This package (`gateway/`) has no dependency on the rest of ELSPETH: it
ships and runs standalone, with its own `pyproject.toml`, test suite, and
container image.

## A plain OpenAI-compatible endpoint

This gateway is a plain OpenAI-compatible endpoint. Any OpenAI client —
the OpenAI SDK, LiteLLM, ELSPETH's own OpenRouter-style provider, or
anything else that speaks the `POST /v1/chat/completions` shape — can point
its `base_url` at `<base>/v1` and use the gateway's static inbound bearer
token as its API key. No gateway-specific header, SDK, or client library is
required.

```bash
curl -s http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer <the configured inbound bearer>" \
  -H "Content-Type: application/json" \
  -d '{"model": "mock-model", "messages": [{"role": "user", "content": "hello"}]}'
```

Note what this curl example does *not* send: `X-ELSPETH-LLM-Gateway-Contract`.
That header is optional — present only to let a client that wants version
negotiation assert which contract major version it expects (`"1"`, matching
`CONTRACT_MAJOR`). Sending it with the right value is accepted exactly as
before; sending it with any other value (`"2"`, an empty string, garbage)
is still rejected with `contract_mismatch` (400); omitting it entirely is
now accepted too. The static bearer is not optional — every `/v1/*` request
still requires a valid `Authorization: Bearer <token>` regardless of what
contract header, if any, it sends.

Two ways to point ELSPETH itself at this gateway, both documented in the main
repository's `docs/reference/environment-variables.md` ("Custom LLM
Endpoints"):

- **By base URL** — the `llm` transform's `base_url` for pipelines, and the
  `ELSPETH_WEB__COMPOSER_*ENDPOINT_*` settings for the Composer. Nothing
  ELSPETH-side knows this is a gateway.
- **The `gateway` pipeline provider** — additionally verifies contract major,
  adapter identity, model alias, and declared capabilities against `/readyz`
  at startup, and maps this gateway's stable error codes to definite
  retry decisions.

That section also states plainly what ELSPETH can and cannot tell an operator
about an endpoint they chose to put in front of a model. Read it before
putting this gateway, or any other, into a deployment.

### The accepted request subset

The inbound body is a strict *subset* of Chat Completions: `model`,
`messages`, `temperature`, `seed`, `max_tokens`, `tools`, `tool_choice`,
and `response_format`. Anything else — `stream`, `n`, `logprobs`, any
other unsupported OpenAI field, at any nesting level — is a hard
`invalid_request` (400), not a silently ignored extra.

One alias exists: **`max_completion_tokens`**, OpenAI's current spelling of
`max_tokens`, is accepted and folded into `max_tokens`. This is not
cosmetic — LiteLLM's `openai` provider path *translates* a caller-supplied
`max_tokens` into `max_completion_tokens` and drops the original key, so
that alias is the only token cap a plain LiteLLM client ever puts on the
wire. Two rules follow:

- Supplying **both** spellings is rejected (`invalid_request`, 400). Two
  spellings of one cap is an ambiguity the gateway will not resolve by
  silently picking a winner; the check is by key presence, so an explicit
  `"max_tokens": null` alongside the alias is rejected too — the same
  notion of "the client sent this" that governs every unsupported field.
- The configured `ELSPETH_LLM_GATEWAY_MAX_MAX_TOKENS` bound applies
  identically to either spelling; the cap is not escapable by choosing the
  other name.

The conformance kit pins all three behaviours (see
`conformance/test_capabilities.py`), so a derived image that drops the
alias fails qualification.

## Quick-start: the local mock stack

`gateway/mock/stack.py` runs three uvicorn servers side by side, wired
together entirely through the gateway's normal `ELSPETH_LLM_GATEWAY_*`
environment contract: a deterministic mock OAuth token endpoint, a
deterministic mock `/v1/invoke` upstream, and the real gateway app
(with the fictional `reference_v1_invoke` adapter) in front of them. No
real agency credentials are required.

```bash
cd gateway
../.venv/bin/python -m mock.stack
```

Once it prints its ready message, it echoes a ready-to-paste `curl`
command using its own fixed, development-only bearer token (see
`_curl_example` in `mock/stack.py`) — copy that, or use the equivalent
shape below:

```bash
curl -s http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer <the bearer token mock.stack just printed>" \
  -H "X-ELSPETH-LLM-Gateway-Contract: 1" \
  -H "Content-Type: application/json" \
  -d '{"model": "mock-model", "messages": [{"role": "user", "content": "hello"}]}'
```

`/healthz` (liveness, no auth) and `/readyz` (adapter/config readiness, no
auth) are also available on the same port.

## Running the tests

```bash
cd gateway
../.venv/bin/python -m pytest tests conformance -q
```

`tests/` is the gateway's own unit/integration suite. `conformance/` is the
portable conformance kit described below — it runs in-process by default
(building its own mock stack), so this single command exercises both with
no external services and no docker daemon required.

## Building your own adapter

The onboarding path for an agency wiring a real upstream behind this
gateway:

1. **Start the reference stack and run the conformance suite unchanged.**
   Run the mock stack quick-start above, and `pytest conformance` in-process,
   before writing any of your own code — this is your baseline: what
   "conformant" looks like against the fictional adapter this repository
   ships.
2. **Generate or copy the adapter scaffold.** Copy
   `gateway/scaffold/adapter_template/` to your own project and rename
   `yourorg_adapter` throughout — see that directory's own `README.md` for
   the exact rename/implement sequence, module by module
   (`descriptor.py` → `config.py` → `request.py` → `response.py` →
   `errors.py` → `adapter.py`).
3. **Implement mappings against locally supplied sanitized fixtures.**
   Drop sanitized (no secrets, no real customer content) request/response/
   error bodies from your real upstream into your copy of the scaffold's
   `fixtures/`, and write real assertions in `tests/test_adapter.py` (renamed
   from the shipped `.template`) against them.
4. **Build an immutable derived image from an exact adapter artifact.** See
   "Derived images" below — `FROM` this gateway's image *by digest*, and
   install a pre-built adapter wheel whose SHA-256 digest you verified, at
   build time only. Do not let the image build resolve a version from an
   index.
5. **Run the complete conformance suite against the derived image.** See
   "Conformance against a running gateway" below — point the conformance
   kit's `GATEWAY_CONFORMANCE_URL` at a running container of your derived
   image, wired to your real (or a further sanitized/staging) upstream and
   OAuth endpoint. A derived adapter passes only if every mandatory test
   for its declared capabilities passes; omitting an undeclared capability
   is fine, claiming and failing one is not.
6. **Publish the image by digest and admit it through Scenario C's
   provenance gate.** The supported production deployment for this
   delivery is ELSPETH AWS Terraform Scenario C; it admits images by
   digest, not by floating tag.

No stage requires ELSPETH maintainers to receive your agency's live
credentials or proprietary payloads.

### The adapter contract

Five methods are required — `descriptor`, `validate_configuration`,
`build_invoke`, `parse_success`, `classify_error` — and are what
`elspeth_llm_gateway.sdk.protocol.AdapterProtocol` declares.

A sixth, `validate_model_target(target: dict) -> None`, lives in a separate
optional protocol (`ModelTargetValidator`) and is what `/readyz` calls once
per configured `ELSPETH_LLM_GATEWAY_MODEL_MAPPINGS` value. Implement it.
The mapping *value* is opaque to the gateway core — only your adapter knows
what shape its own `build_invoke` reads out of it — so without this hook a
deployment mapped to a target your adapter cannot consume passes readiness
and then fails every completion with `internal_error`. Assert exactly the
keys `build_invoke` reads and no more: readiness must only refuse what a
completion would actually have failed on. The call must be purely
computational (no I/O, no network, no credential lookup — `/readyz` makes no
OAuth call and no upstream call). Raise any exception to reject a target;
your exception message is never published, only the fixed readiness code
`model_target_invalid:<alias>`.

Why optional in the protocol but mandatory in practice: `AdapterProtocol` is
`runtime_checkable`, so adding a sixth member to it would change what
`isinstance(adapter, AdapterProtocol)` returns for an already-shipped adapter
package — a silent break of the adapter API at the same `ADAPTER_API_MAJOR`.
Keeping it separate makes the addition purely additive: **no
`ADAPTER_API_MAJOR` bump, and an existing adapter keeps passing readiness**
(its `/readyz` payload simply reports
`adapter.validates_model_targets: false`). The conformance kit is where the
method becomes mandatory — `conformance/test_identity.py` asserts
`validates_model_targets` is true, so a derived image whose adapter cannot
validate its own targets does not qualify.

## The container image

`gateway/Dockerfile` is a multi-stage build:

- **builder** — installs this package (and its dependencies) into an
  isolated venv at `/venv` via `pip install /build`.
- **final** — `FROM` the same digest-pinned `python:3.12-slim` base,
  copies `/venv` only (no `pip install`, no `ADD`, in this stage), runs as
  the fixed non-root UID/GID `65532:65532`, and starts with an absolute,
  shell-free entrypoint:

  ```
  ENTRYPOINT ["/venv/bin/python", "-m", "uvicorn",
              "elspeth_llm_gateway.core.app:build",
              "--host", "0.0.0.0", "--port", "8787",
              "--factory", "--timeout-graceful-shutdown", "30"]
  ```

  `elspeth_llm_gateway.core.app:build` is a small factory —
  `create_app(load_config(os.environ))` — so the container's entire runtime
  configuration is the `ELSPETH_LLM_GATEWAY_*` environment it is started
  with; an invalid or incomplete environment fails the container at
  startup, before it ever binds a port.

Build it:

```bash
docker build \
  --build-arg GATEWAY_REVISION="$(git rev-parse HEAD)" \
  gateway/ -t elspeth-llm-gateway:dev
```

The image is read-only-rootfs compatible: it never writes anything outside
`/tmp` (and `PYTHONDONTWRITEBYTECODE=1` stops it from ever trying to write
`.pyc` files next to `/venv`), so it can run with `--read-only --tmpfs /tmp`.

### Required environment

At minimum: `ELSPETH_LLM_GATEWAY_INBOUND_BEARER`, `_ADAPTER`,
`_UPSTREAM_ORIGIN`, `_OAUTH_TOKEN_URL`, `_OAUTH_CLIENT_ID`,
`_OAUTH_CLIENT_SECRET`, `_OAUTH_AUTH_METHOD`, `_MODEL_MAPPINGS`,
`_MAX_MESSAGES`, `_MAX_TOOLS`, `_MAX_STRING_CHARS`, `_MAX_SCHEMA_BYTES`,
`_MAX_SCHEMA_DEPTH` (all `ELSPETH_LLM_GATEWAY_`-prefixed). See
`src/elspeth_llm_gateway/core/config.py`'s `KNOWN_ENV` for the complete,
closed set of recognised variables — any other variable in the
`ELSPETH_LLM_GATEWAY_` namespace fails startup. `_UPSTREAM_ORIGIN` must be
`https://…` or exactly `http://127.0.0.1:<port>`.

### Derived images

A real adapter ships as its own installable package, added on top of this
image at **build** time only — never mounted, and never installed at
container start:

```dockerfile
# Pin the exact digest this derived image was built and conformance-tested
# against — never a floating tag.
FROM elspeth-llm-gateway@sha256:<digest>

# The admitted base runs as an unprivileged user. Elevate only for the
# build-time package installation, then restore the fixed runtime identity.
USER root

# Copy and verify the exact, pre-built adapter artifact. The build must not
# contact an index or silently resolve transitive dependencies.
COPY yourorg_adapter-X.Y.Z-py3-none-any.whl /build/yourorg_adapter-X.Y.Z-py3-none-any.whl
ARG ADAPTER_WHEEL_SHA256
RUN test -n "$ADAPTER_WHEEL_SHA256" \
    && printf '%s  %s\n' "$ADAPTER_WHEEL_SHA256" /build/yourorg_adapter-X.Y.Z-py3-none-any.whl \
       | sha256sum -c - \
    && /venv/bin/pip install --no-cache-dir --no-deps /build/yourorg_adapter-X.Y.Z-py3-none-any.whl \
    && rm -f /build/yourorg_adapter-X.Y.Z-py3-none-any.whl

ENV ELSPETH_LLM_GATEWAY_ADAPTER=yourorg_adapter

USER 65532:65532

# The base image labels describe the in-tree reference adapter. A derived
# image must stamp its own source revision and replace all four adapter labels
# with the installed adapter's code-owned descriptor and the fingerprint
# reported by the offline command below.
ARG GATEWAY_REVISION
RUN test -n "$GATEWAY_REVISION"
LABEL org.opencontainers.image.revision="${GATEWAY_REVISION}" \
      io.elspeth.llm-gateway.adapter-name="yourorg_adapter" \
      io.elspeth.llm-gateway.adapter-version="X.Y.Z" \
      io.elspeth.llm-gateway.adapter-api-major="1" \
      io.elspeth.llm-gateway.adapter-fingerprint="REPLACE_WITH_64_LOWERCASE_HEX"
```

The placeholder makes this a deliberate two-build procedure. First build a
provisional local image, run the offline identity command below against it,
and copy its 64-character fingerprint into the Dockerfile. Never sign or
publish that provisional image. Build the final image from the same base digest,
wheel, wheel digest, and install path, then re-run the offline command and
compare its document with every final label before signing that final image
digest. A mismatch means the build inputs were not reproducible and the image
is ineligible.

If the adapter has dependencies beyond the gateway SDK, install each one from
an exact, separately hash-verified artifact before the adapter. `--no-deps` is
intentional: it prevents `pip` from turning an apparently pinned adapter into
an unreviewed dependency-resolution step.

Before signing or deploying the derived digest, inspect the installed identity
without supplying credentials or network access. The container invocation runs
`python -m elspeth_llm_gateway.image_identity` through its absolute venv
interpreter:

```bash
docker run --rm --network none \
  --entrypoint /venv/bin/python \
  yourorg-gateway@sha256:<digest> \
  -m elspeth_llm_gateway.image_identity --adapter yourorg_adapter
```

The canonical JSON is the comparison source for the derived image labels and
the deployment's expected runtime/adapter identity. It reads neither gateway
configuration nor runtime secrets. A label mismatch, missing label, or command
failure makes the image ineligible for secret-bearing deployment.

The revision and identity labels are producer assertions, not provenance by
themselves. Do not stamp a clean commit revision onto an image built from a
dirty worktree. Admission still has to bind the resulting image digest to a
trusted producer attestation, an acceptable scan result, and the exact
runtime/adapter identity expected by Terraform before any secret selector is
placed in a task definition.

Runtime code mounts (bind-mounting adapter source into a running container)
and container-start package installs are forbidden: the whole point of a
derived image is that its digest is the complete, immutable, admitted
artifact — nothing loads or changes after that digest is built.

### Conformance against a running gateway

`gateway/conformance/` runs in two modes, selected by one environment
variable:

- **in-process** (default, no env var set) — builds its own mock OAuth +
  mock upstream + gateway app in-process; this is what `pytest conformance`
  does with no setup.
- **image-qualification** — set `GATEWAY_CONFORMANCE_URL` to a running
  gateway's base URL (e.g. a container under test, wired to a reachable
  instance of this repo's mock OAuth + mock upstream stack, or your own
  staging equivalent):

  ```bash
  GATEWAY_CONFORMANCE_URL=http://127.0.0.1:8787 \
  GATEWAY_CONFORMANCE_BEARER=<the inbound bearer that deployment expects> \
  GATEWAY_CONFORMANCE_MODEL_ALIAS=<a model alias that deployment has mapped> \
    ../.venv/bin/python -m pytest conformance -q
  ```

  `GATEWAY_CONFORMANCE_BEARER` / `GATEWAY_CONFORMANCE_MODEL_ALIAS` default
  to this module's in-process fixed values and only need overriding when
  the deployment under test uses different ones. One fixture
  (`limited_gateway_client`, the capability-limited adapter used to test
  `capability_unsupported`) is in-process only and **skips** in URL mode —
  a derived image ships one fixed adapter, so there is no second variant to
  spin up against it; that skip is expected, not a failure.

See `gateway/conformance/conftest.py` for the exact fixture wiring and the
full set of environment variables each mode reads.
