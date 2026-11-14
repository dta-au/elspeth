# Credential detectors: comparison and proposed redesign (2026-11-15)

Read-only investigation by an agent, assembled by the lead from its messages.
"Run" means the agent executed the detector on an input; "read" means it was
assessed from source. No suites were run. Scripts and raw outputs are in the
gitignored `.lanes/detector-compare/` of the integration worktree. Vendor
format facts in §4 come from the agent's memory of vendor documentation and
must be verified before they ship.

## 1. Is the CI detector better than production?

There is no CI detector. No workflow runs a secret scanner, and there is no
gitleaks, detect-secrets or trufflehog configuration or dependency. What
exists:

| Detector | Location | Vendor and URL strings caught (of 33) | Prose false positives (of 15) |
|---|---|---|---|
| Production (`CREDENTIAL_PATTERN_SPECS`, 16 regexes plus structural rules) | `src/elspeth/contracts/credential_material.py:85-120` | 24 | 7 |
| Pre-commit hook (11 `git grep` patterns, local only) | `scripts/git-hooks/pre-commit-secret-scan.sh:31-43` | 11 | not reported |
| Judge scrubber (31 patterns) | `elspeth-lints/src/elspeth_lints/core/source_excerpt.py:326-560` | more families than production, misses some it catches | 0 |

All seven production prose false positives come from one rule,
`credential_assignment`.

The pre-commit hook's private-key pattern is dead: it starts with `-`, so
`git grep` exits 129, and the script swallows that with `|| true` (`:75`). A
staged private-key header passes the hook (run).

## 2. Production has several detectors, not one

| Id | Detector | Used for |
|---|---|---|
| P1 | `CREDENTIAL_PATTERN_SPECS` | Pattern vocabulary |
| P2 | `find_credential_material` (`:159-170`, `:291-302`): P1 plus PEM header, database URL userinfo, any http(s) URL with userinfo or a sensitive query key, and a structural rule for values under credential-named keys or keys ending `_secret`, `_key`, `_token`, `_password`, `_credential` | Refusal surfaces |
| P3 | `scrub_credential_material` (`:360-373`) | Audit scrubbing |
| P4 | `reject_credential_shaped_content` (`web/validation.py:145-168`): P2 plus SSN and card numbers | Amendments, tool terms, titles |
| P5 | `_redact_sensitive_content` (`web/validation.py:171-196`): P1 regexes plus card and PII shapes; does not call P2, so it lacks the PEM and credential-URL rules | Egress redaction |
| P6 | `_LLM_ERROR_SENSITIVE_PATTERNS` (`web/composer/llm_response_parsing.py:98-109`): an independent older list | Provider error text |
| P7 | `_UNSAFE_TEXT_PATTERNS` (`contracts/plugin_assistance.py:31-43`) | Plugin-authored text |

Today's `sk-` rule has no left boundary and flags
`risk-management-and-compliance-framework-2024` and
`mask-background-color-default` (run).

## 3. Confirmed defects

- **Multi-word egress redaction (run).** `password: correct horse battery
  staple` becomes `<redacted-sensitive:credential_assignment> horse battery
  staple`. The value class stops at the first space.
- **Provider-error scrubber** stores a hyphenated `sk-proj-…` key verbatim
  (see `string-matching-2026-11-15.md`, finding 1).
- **Blob content is not scanned on upload**, only the filename
  (`blobs/routes.py:163, 295`); content is scanned when a composer tool reads
  it (`tools/blobs.py:2350, 2496`).

## 4. Proposed rule set

**Tier 1, known formats, on every surface, anchored at a left boundary.**
AWS access key ids; GitHub tokens (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`,
`github_pat_`); `sk-ant-`, `sk-proj-`, `sk-svcacct-`, `sk-admin-`, legacy
`sk-` of 32 or more characters with a digit and a letter, `sk-or-v1-`;
`gsk_`; `hf_`; `AIza`; `GOCSPX-`; Slack tokens and webhooks; Discord
webhooks; Stripe keys; `whsec_`; `glpat-`; `npm_`; `SG.`; Azure storage key,
`AccountKey=`, `sig=` and the `8Q~` secret form; a JWT with two `eyJ`
segments; a PEM header; `Bearer` plus 20 characters; `Authorization: Basic`;
a URL of any scheme whose userinfo carries a password, with a placeholder
stoplist. No checksum gates.

**Tier 2, "looks like a key".** An unbroken run of 32 to 128 characters from
`[A-Za-z0-9_\-+/]`, not hex, not a UUID, at least three of lower, upper and
digit, entropy at least 4.0, a bounded longest lowercase run, not slug-like,
not `sha512-` integrity text. Measured: 97.6 to 99.9% of 14,400 random
tokens; 37 of 48,565 distinct repository tokens of 32 or more characters
(0.08%); none of 10 constructed legitimate values (sha256, UUID, the real
`composer_skill_hash`, model ids, a secret reference). Residual false
positives: base64 of a 32-byte digest, long camelCase ids with digits, some
test-class and environment-style names. Residual misses: unlabelled hex keys,
short or multi-word passwords, `OPENAI_API_KEY=notarealkey`.

**Keyword assignment** is replaced by a shaped rule: a label, then `:` or
`=`, then one token, firing only when the token has 6 or more characters with
a letter and a digit, or 12 or more with three classes, and is not a
placeholder. On the corpus: prose false positives 7 of 15 to 0 of 15; vendor
and URL hits 24 of 33 to 32 of 33. On 135,915 documentation lines: 52 flagged
lines to 7.

**Field-name suffix rule (run).** `is_secret_field` returns true for
`sort_key`, `join_key`, `group_key`, `partition_key`, `primary_key`,
`cache_key`, `dedup_key`, `next_page_token` and `pagination_token`; only
`data_key` and `alternate_key` are exempt (`:77`). Proposed: keep the exact
names (`api_key`, `secret_key`, `private_key`, `access_key`, `token`,
`password`, `secret`, `client_secret`) and the specific suffixes `_api_key`,
`_secret_key`, `_private_key`, `_access_key`, `_secret`, `_password`,
`_credential`. For any other `*_key` or `*_token` field, refuse only when the
value is a non-empty string that passes Tier 2; secret references stay
exempt. Schema-declared credential fields remain the structural answer.

**Blob content, assessed by reading.** A credential in an uploaded file is
stored at rest unscanned, but no path was found that sends it to a provider
through the composer, because content is scanned when a composer tool reads
it. It can still reach a provider when a pipeline's LLM transform processes
that data at run time, which is the data plane and outside the composer
guard. So it is a storage exposure, not a composer-to-provider one.

## 5. Where the narrow rule must apply

`composer_provider_request` (`provider_gateway.py:536`) re-scans the whole
history, assistant text included. If model output passes a narrow rule while
the replay scan stays wide, the next turn is refused, and every later turn.
So the shaped rule must cover model output and every replay and stored-state
surface. The wide rule can remain for egress redaction only.

## 6. Compatibility

- `detector_version` "1" (`credential_material.py:22`) is stamped on findings
  and persisted evidence; `diagnostic_evidence.py:188` accepts only `{"1"}`.
  A changed detector should stamp "2" and the reader accept both.
- Findings are value-free and no hashes of values are stored, so narrowing
  cannot break stored data. Widening (new Tier 1 families, Tier 2) can refuse
  stored sessions on replay and stored sink-effect targets
  (`sink_effect_identity.py:120`). Stored data was not queried.
- Pinned tests: `test_credential_guard.py:324, 352`;
  `test_routes_state.py:2813, 2916`; `test_diagnostic_evidence.py:327`;
  `test_credential_material.py:50-62, 191-210`;
  `test_visible_tool_event_persistence.py:357`.

## 7. Entry points for user-authored text

A server check exists for chat send, scripted sends and retry
(`messages.py:46`, `operations.py:139`, before the message is stored); fork
(`sessions.py:891`); titles (`:695`, `:809`); interpretation amendments and
review notes (pydantic validators); YAML import (`state.py:1282`); and the
composer MCP surface (`composer_mcp/server.py:385`). Recompose replays stored
text and relies on the provider-request scan. Blob content: see §3. The
secrets panel (`secrets/routes.py:93`) is the legitimate carrier and must be
exempt from any browser check.

A sent body is held in the tab's `sessionStorage`
(`stores/composerOperationCustody.ts:84`) until its operation resolves, so a
pre-submit block also keeps a key out of tab storage. Free-text inputs other
than the chat box: the edit-and-fork textarea (`MessageBubble.tsx:203`) and
the interpretation amendment (`AcknowledgementCard.tsx:816`).

## 8. Transparency gaps

- The user's refusal copy names no tier, family or position.
- When model output is blocked, the user is told the request was "refused by
  the admission policy" and to ask an administrator about access and quota
  (`composer_turn.py:1832-1843`). The model is told nothing; the turn aborts.
- Blocked tool-call arguments raise with no tool result (`_dispatch.py:912`).
  `credential_material_rejected` is in the closed registry
  (`error_codes.py:80`) with no guidance entry.
- On the MCP surface a refusal falls into the plugin-crash path and is
  audited as `PLUGIN_CRASH` (`server.py:895-899`, read).
- Silent redaction: egress markers with no explanation (`validation.py:171`;
  `advisor_context.py`, `advisor_abandonment.py:71`, `turn_effects.py:818`,
  `_auto_title.py:178`); the audit sentinel `<redacted-secret>` carries no
  family.

## 9. The "rotate it" instruction

A detected credential in a user message never reaches the model: it is
refused with 422 before the message is stored. The model sees only what the
detector misses. So the instruction belongs in the user-facing refusal copy,
with a skill sentence near `pipeline_composer.md:653-668` for undetected
cases. The copy edit moves literal-copy pins; the skill edit moves the
planner scaffold byte pin and the skill hash.

## 10. Browser and server sharing one list

Python holds the authority as data; a generator writes
`frontend/src/generated/credentialFormats.json`; a pytest asserts the
committed file equals the regenerated one; a shared corpus of recipes (not
literal keys) is run by both pytest and vitest. The 28 Tier 1 regexes behaved
identically in Node 24 and Python on 86 strings (run).

Chat box: add `onPaste` and `onDrop` handlers that refuse the input and show
an inline alert offering the secrets panel, with a scan at submit as the
backstop; the existing text is not cleared. `ChatInput` has no paste handler
today.

False-positive escape: none at first. If one is needed, a user-only
confirmation for Tier 2, never Tier 1, never model-facing, and audited.
