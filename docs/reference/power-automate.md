# Power Automate

The `power_automate` source and sink call HTTP-triggered cloud flows using
`elspeth.power-automate.v1`. The source reads a finite, ordered snapshot. The
sink publishes selected fields with an engine delivery ID and reconciles
interrupted writes against a durable target. A trigger acknowledgement alone
does not establish completion.

The [example](../../examples/power_automate/README.md) provides settings and
credential-free emulator commands. ELSPETH does not provision flows, expose an
inbound endpoint or administer connections. This integration supports public
cloud HTTPS endpoints on port 443.

## Operating prerequisites

Provide two flows: a read flow and a status/write flow. Their owners need
permission to the dataset and durable target, appropriate Power Automate and
Dataverse licensing, and connector access permitted by the environment's DLP
policy. Size page counts and throughput for the environment's request/action
quotas. OAuth trigger availability varies by region. These are deployment
requirements; automated verification uses controlled transport and durable
flow emulation. [Microsoft authentication guidance](https://learn.microsoft.com/en-us/power-automate/oauth-authentication),
[limits and configuration](https://learn.microsoft.com/en-us/power-automate/limits-and-config).

Use the current URL copied from the saved trigger in the designer. Preserve
its complete encoded path and query rather than reconstructing it from a flow
ID or an old hostname. Microsoft has changed trigger URLs and endpoint
formats. [Trigger troubleshooting](https://learn.microsoft.com/en-us/troubleshoot/power-platform/power-automate/flow-run-issues/triggers-troubleshoot).

## Authentication and configuration

Every source and sink requires `allowed_origin`: an exact HTTPS origin with
no path, query, fragment, user information or wildcard. The actual URL must
match it. Public IP validation, pinned dispatch and redirect refusal apply
independently; a matching origin does not permit private network access.
Credentials cannot select a different destination.

Choose exactly one authentication variant. Unlisted fields, arbitrary
headers, authored scopes and authority overrides are refused.

### Signed URL

```yaml
auth:
  method: sas_url
  trigger_url_secret: "${POWER_AUTOMATE_READ_TRIGGER_URL}"
allowed_origin: https://flows.example.test
```

The secret contains the entire current designer URL, including a nonempty
`sig` query parameter. Do not also supply `trigger_url`. Keep it in the
existing environment/secret-store facility. Treat anyone with this URL as
able to invoke the flow; configure the trigger's corresponding signed-URL
authentication mode.

### Service principal

```yaml
auth:
  method: service_principal
  tenant_id: YOUR_TENANT_ID
  client_id: YOUR_APPLICATION_CLIENT_ID
  client_secret: "${POWER_AUTOMATE_CLIENT_SECRET}"
trigger_url: https://flows.example.test/read
allowed_origin: https://flows.example.test
```

Install the `azure` extra. Configure the trigger for **Specific users in my
tenant** and populate Allowed users with the service principal's **object
ID**, distinct from the application client ID. An empty Allowed users field
admits the tenant rather than the intended principal. ELSPETH uses
`ClientSecretCredential` lazily; the trigger URL must be credential-free.
[Microsoft trigger authentication](https://learn.microsoft.com/en-us/power-automate/oauth-authentication).

### User-assigned managed identity

```yaml
auth:
  method: managed_identity
  client_id: YOUR_USER_ASSIGNED_IDENTITY_CLIENT_ID
trigger_url: https://flows.example.test/read
allowed_origin: https://flows.example.test
```

Install the `azure` extra and assign this identity to the execution host.
Authorize its service principal object ID in the same trigger setting.
ELSPETH explicitly uses `ManagedIdentityCredential` with the configured
user-assigned client ID. It does not use an ambient credential chain or a
system-assigned fallback. A constructor/catalog probe acquires no token.

The fixed public-cloud token audience is
`https://service.flow.microsoft.com/`, including the trailing slash. ELSPETH
requests the SDK scope `https://service.flow.microsoft.com/.default`;
`/.default` selects the resource's configured application permissions.
Configured principal admission remains the flow owner's responsibility.
[Audience requirements](https://learn.microsoft.com/en-us/power-automate/oauth-authentication),
[client credentials scopes](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow).

### Secrets, rotation and Web Composer

Secret-bearing live configurations require the existing
`ELSPETH_FINGERPRINT_KEY`. Supply it through the operator-managed secret
facility and retain it across the supported recovery window. Audit-safe
configuration stores HMAC fingerprints in place of signed URLs/client
secrets, even when the global development raw-secret switch is enabled.
Keep secret values and fingerprint keys out of committed YAML, logs and
support reports. Retained bounded response evidence can contain business
data; apply the payload store's access and retention controls.

Changing the configured URL, secret, identity or other durable options
changes the admitted configuration. Old-run resume/verify refuses that
change. Start a fresh run after deliberate rotation; new runs use new
delivery IDs and can create new business effects. Bearer renewal for the
same admitted principal is supported. Preserve old safe audit evidence;
do not edit it to force admission.

Web Composer separately requires operator
`power_automate_allowed_origins` (environment
`ELSPETH_WEB__POWER_AUTOMATE_ALLOWED_ORIGINS`). Its default is empty, denying
Power Automate destinations. Add exact approved HTTPS443 origins; no
wildcards or private-host exceptions are available. Credential wiring is a
second policy: existing secret authorization must permit the exact secret,
component type, `power_automate` plugin and option key. For SAS the option key
is `auth.trigger_url_secret`; for service principals it is
`auth.client_secret`. Server secrets also require their existing server
secret allowlist admission. An authored `allowed_origin` grants neither
operator approval nor secret permission. Policy changes invalidate stale
execution admission. CLI/batch execution uses the configured origin and
SSRF guards; web operator policy is applied by the web runtime.

## Build the read flow

1. Create an instant cloud flow with **When an HTTP request is received**.
   Choose the authentication mode above and save to obtain its current URL.
2. Define the request using
   [read.schema.json](../../examples/power_automate/contracts/read.schema.json).
   Enforce the closed protocol/operation and required fields. The published
   schemas use JSON Schema 2020-12; if the designer does not enforce a
   keyword, enforce that condition explicitly before data access.
3. Bind `query` to an approved dataset. It is fixed authored JSON, containing
   no credential fields or secret locators. Reject unsupported queries.
4. Resolve `snapshot_id` to a retained immutable dataset version. Null asks
   for a fresh version; return its nonempty ID. Apply a stable ordering with
   a unique tie breaker. Continuations must address that same version,
   ordering and query; do not paginate a changing live table.
5. Validate the cursor and page size, read at most the requested number of
   candidates, and return the next opaque cursor or null at EOF. A cursor
   is data, not a URL to follow. Do not mutate records while reading.
6. Return synchronous HTTP 200, `Content-Type: application/json`, with this
   exact shape after retrieval completes:

```json
{
  "protocol": "elspeth.power-automate.v1",
  "snapshot_id": "snapshot-42",
  "rows": [{"record_id": "A1", "result": "approved"}],
  "next_cursor": null
}
```

The response schema is
[response.schema.json](../../examples/power_automate/contracts/response.schema.json).
No additional envelope fields are accepted. Each page must maintain the
same snapshot ID, matching an explicitly configured ID from its first page.
An empty final page is valid. Empty intermediate pages consume page budget.
Repeated cursors, oversized pages, snapshot changes or bounds reached with
a remaining continuation fail the source rather than truncate to success.

Source `schema` uses the existing fixed/flexible/observed modes, normalization,
`field_mapping` and source coercion. Scalar, null or list row candidates
follow `on_validation_failure` individually and retain their source index;
a malformed page fails the source. `max_rows` includes every candidate,
including quarantined/discarded rows. A complete page envelope is admitted
before any candidate from it is yielded.

When source validation failures and sink write rejections share a JSONL
quarantine destination, set its `mode: append`, as the shipped example does.
These roles deliver separate groups; append preserves each group's rows.
Choose a fresh output location for each independent run.

## Build the status/write flow

The demonstrated business effect is one immutable Dataverse delivery record.
Its atomic keyed creation also stores completion evidence. Extending the
flow to email, payments or multiple downstream actions requires equivalent
deduplication and authoritative recovery for every claimed effect.

### Durable target

Create a **standard Dataverse table** with an active unique alternate key on
`delivery_id`. Store `payload_sha256`, all selected business data (or its
lossless JSON representation), terminal outcome, optional rejection code,
receipt ID and original flow-run ID in the same record. Use lowercase
64-hex strings for the delivery ID/hash. Keep records immutable and prevent
other writers from bypassing the keyed create contract. Retain them for the
entire supported recovery window, including delayed invocations.

For the shipped `record_id`/`result` example, these columns make the flow
branches concrete (use your solution publisher prefix in logical names):

| Column | Type / use |
| --- | --- |
| `delivery_id` | Required single-line text, 64 characters; unique alternate key |
| `payload_sha256` | Required single-line text, 64 characters |
| `record_id`, `result` | Text columns containing the selected input values |
| `outcome` | Required text constrained to `applied` or `rejected` |
| `reason_code` | Nullable text, one permitted rejection code when rejected |
| `receipt_id`, `original_flow_run_id` | Text, required for applied records; <= 256 UTF-8 bytes |

Wait until the alternate key reports **Active** before using it; its index
is created asynchronously. [Define alternate keys](https://learn.microsoft.com/en-us/power-apps/maker/data-platform/define-alternate-keys-reference-records).
Map selected fields directly into those columns and compare each value on
duplicate read-back as well as the hash. Thus a caller reusing a hash with
different data cannot obtain a matching completion. Capture a receipt
identifier once in a Compose action using `guid()` and the current run
identifier using `workflow().run.name`, before the create. Store both in the
create body and construct subsequent responses from the stored values.
[Workflow expression functions](https://learn.microsoft.com/en-us/azure/logic-apps/expression-functions-reference).

The target's business effect is creation of an `applied` record; a
`rejected` record stores a no-business-effect decision. Prepare complete
data, the terminal decision and identifiers before a single atomic create.
Do not insert an empty claim row followed by a separate completion update.
Do not use a separate receipt table as the only proof of business delivery.

Use the Dataverse Web API alternate-key URL with create-only semantics:

```http
PATCH <organization>/api/data/v9.2/<delivery_entity_set>(<delivery_id_column>='<delivery_id>')
If-None-Match: *
Content-Type: application/json
```

Place all non-key data/outcome/receipt/original-flow-run columns in the
request body. Take the key from the URL. Ordinary PATCH upsert updates an
existing record; `If-None-Match: *` prevents that update.
[Dataverse upsert behavior](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/use-upsert-insert-update-record),
[alternate-key addressing](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/use-alternate-key-reference-record).

On duplicate/precondition failure or lost create response, read that exact
alternate key authoritatively. Compare its bound hash and data. A match
returns the **original stored** outcome, receipt and flow-run ID. Never
return the duplicate invocation's run ID. A conflicting hash, inaccessible
record or uncertain completion returns `unknown`; it cannot become a
business rejection.

Publish an explicit deterministic rejection rule before enabling the flow,
for example a denied business record under the target's validation policy.
Persist its reason and bound hash atomically as a sticky `rejected` record.
Later status and repeated writes return the same rejection without invoking
the business action. Permitted reason codes are `validation_failed`,
`policy_denied` and `target_conflict`. The latter means a proven no-effect
business-target conflict, never a delivery-ID/hash collision.

### Trigger and branches

1. Create the second HTTP-triggered instant flow with the same authentication
   controls. Accept the common protocol/operation envelope and use a Switch
   on `operation`. Reject unknown operations before target access.
2. Validate the `status` branch against
   [status.schema.json](../../examples/power_automate/contracts/status.schema.json)
   and the `write` branch against
   [write.schema.json](../../examples/power_automate/contracts/write.schema.json).
   Enforce exact keys and identifier shapes even if the designer's trigger
   schema supports fewer keywords.
3. `status` performs only authoritative keyed retrieval. It must not claim
   a delivery, create/update a record or invoke the business action. Return
   an existing bound terminal outcome, or safe `not_applied` only under the
   immutable create-only/dedup contract with reliable absence and valid
   retention. An expired/deleted ledger, unresolved earlier request or
   inaccessible/conflicting target remains `unknown`.
4. `write` admits the selected data, hash and published business policy, then
   attempts the atomic create described above. Concurrent equal-ID calls
   converge on one stored outcome. A delayed old invocation cannot create
   a second effect. Read back duplicate/uncertain results; preserve the
   original receipt and flow-run ID.
5. Place the synchronous Response action after every effect claimed by
   `applied` is durable. Do not enable asynchronous response. Return HTTP
   200 and JSON containing only the fields for the chosen variant.

Use a Scope for keyed creation and a second Scope whose **Configure run
after** includes failed/timed-out creation to perform keyed read-back. Its
Response must depend on the admitted read-back result, not merely the
first Scope's HTTP status. For a deterministic example rejection policy,
reject an empty `record_id` as `validation_failed` before choosing the
create body; persist that rejected outcome at the same delivery key. Both
successful and rejection creates use the same uniqueness boundary. Keep
connector retries disabled where available; any connector retry that does
occur must still use the identical delivery key and complete body.

| State | Additional fields | Meaning |
| --- | --- | --- |
| `applied` | Nonempty `receipt_id`, `flow_run_id` | Complete durable effect for this delivery/hash |
| `not_applied` | None | Status-only safe absence under the target contract |
| `unknown` | None | No authoritative safe completion or resubmission proof |
| `rejected` | One allowed `reason_code` | Sticky decision proving zero business effects |

All variants also contain `protocol`, `delivery_id`, `payload_sha256` and
`state`. Write cannot return `not_applied`. Example completion:

```json
{
  "protocol": "elspeth.power-automate.v1",
  "delivery_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "payload_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "state": "applied",
  "receipt_id": "receipt-42",
  "flow_run_id": "original-flow-run-42"
}
```

The repeated hex characters illustrate field shapes; they are not the hash
of the read example. ELSPETH hashes the canonical selected `data` separately
from the HTTP envelope. The delivery ID is the original engine member ID,
distinct for different members even when their data is equal.

ELSPETH checks status before the initial write and during recovery. A sticky
rejected status leads to an idempotent write of the same member; that write
returns the same rejection, allowing precise diversion to
`on_write_failure`. A generic 4xx/5xx, authentication error, missing receipt,
202/204, empty 200, pending state, timeout or response loss establishes no
no-effect proof and cannot divert the row as a business rejection.

## Limits, audit and execution modes

| Option / bound | Behavior |
| --- | --- |
| `page_size` | Strict integer 1–1000; default 100 |
| `max_pages`, `max_rows` | Positive strict integers; defaults 1000 and 100000 |
| `fields` on sink | Ordered nonempty unique names, required by its input schema; exports only these fields |
| `max_request_body_bytes` | Positive strict integer; default 1 MiB |
| `max_response_body_bytes` | Source default 4 MiB; sink default 1 MiB; bounded decoded bytes |
| `timeout_seconds` | Finite 0 < value <= 110; default 90 seconds |
| Identifiers | Snapshot/receipt/flow-run <= 256 UTF-8 bytes; cursor <= 4096 |
| URL / JSON | URL <= 16384 UTF-8 bytes; JSON depth <= 64 |

Booleans are refused for numeric bounds. Schema annotations for byte bounds
do not replace runtime checks. Duplicate JSON keys, nonfinite numbers,
invalid UTF-8, extra envelope fields and redirects are refused. HTTP calls
retain bounded decoded-body evidence plus safe/fingerprinted metadata in
the audit payload store; diagnostics use value-free codes. They do not
retain a reconstructable signed URL or arbitrary raw headers.

The protocol codec also enforces the RFC 8785 canonical audit domain.
Integer literals must be within the exact IEEE 754 safe range
`-9007199254740991` through `9007199254740991` (`±(2**53 - 1)`); represent
larger identifiers as strings. Strings must contain valid Unicode code
points, with no lone surrogates. Values are not silently coerced to enter
this domain. JSON Schema shape validation alone does not enforce these
lexical/numeric constraints. Unsupported canonical JSON aborts with the
closed `invalid_json` diagnostic, while its exact bounded decoded response
evidence remains audited. A source validates the whole page before yielding
any row; a codec failure is a page failure rather than a row quarantine.

The timeout is one absolute dispatch-to-decode budget covering connect,
TLS, write and streamed reads. Credential acquisition, DNS and rate-limiter
waits precede it and have their existing lifecycle limits. No automatic POST
retry is performed. A timeout cannot cancel a packet already dispatched;
remote deduplication remains necessary. Power Automate allows a 120-second
inbound response window and can continue actions after Response. The lower
local limit and placement of Response above are deliberate connector rules.
[Microsoft duration limits](https://learn.microsoft.com/en-us/power-automate/limits-and-config).

| Mode | Source | Sink |
| --- | --- | --- |
| LIVE | Audited bounded snapshot pagination | Authoritative status and per-member publication |
| Ordinary resume | Restores durable source data; never rereads the flow | Reconciles original delivery IDs before completing members |
| REPLAY | Archived rows, contracts and validation decisions; no source credentials, SDK, DNS or HTTP | Virtual sink; no credentials or target calls |
| VERIFY | One live source read after archived configuration/request admission; compare complete canonical JSON call evidence and ordered source decisions before downstream work | Virtual sink on match or mismatch; no credentials or target calls |
| AUDIT_EXPORT | Not applicable | Refused before lifecycle |

Set `snapshot_for_resume: true` to materialize the finite source before any
downstream effects. The existing engine snapshot mechanism supports a
single source and at most 64 MiB. A streaming source interrupted before EOF,
an oversized/incomplete snapshot or unsupported multi-source snapshot
cannot be resumed. Remote retention does not bypass these engine limits.
Keep payload-store source snapshots and remote completion evidence for the
intended recovery window. Nonlive resume is refused. The public Web Composer
does not expose replay/verify modes; use the supported CLI/batch surfaces.

For source verification, configure a retained explicit `snapshot_id` and
stable query/order. Canonical comparison tolerates JSON whitespace and
volatile transport headers, while enforcing complete decoded JSON, status,
media type, redirect behavior and all ordered row/validation decisions.
Bearer renewal is allowed for the admitted source identity. Missing/corrupt
payloads or configuration/archive drift refuse verification before
publication. Sink status/write calls are never replayed as external effects.
Strict request admission can also refuse verification after the endpoint's
DNS resolution or pinned IP addresses change, even when its rows are identical.

## Credential-free verification

Run the two integration modules listed in the
[example README](../../examples/power_automate/README.md). They use a durable
SQLite target keyed by delivery ID and controlled HTTP/DNS fixtures rather
than a tenant. Checks inspect source accounting, durable target actions,
receipt identity, offline replay and source verification. Recovery controls
exercise lost responses, interrupted publication, sticky rejection and
uncertain/conflicting outcomes. Authentication variants are covered with
fake SDK credentials and inspected requests.

The implementation's absolute deadline uses public HTTPCore network backend
and connection-pool interfaces so each socket operation receives the
remaining budget. [HTTPCore network backends](https://www.encode.io/httpcore/network-backends/),
[connection pools](https://www.encode.io/httpcore/connection-pools/).
