# Power Automate implementation quality review

**Final verdict: GO for implementation quality.** This is an independent code quality review of the full feature diff against `487ac85a377f135e012bb206e3769cd65f6fadb8`, including new untracked implementation files. Five independently measured defects were repaired during review and independently rechecked. The separately supplied archive-status, JSON media-variant and external-token boundary findings were also rechecked and are attributed below. No unresolved blocker was found in the reviewed scope.

This verdict concerns implementation quality. It does not certify a frozen tree, whole-tree gates, PostgreSQL tests, signing, local merge or hosted CI. The requested feature has no live tenant acceptance step or prerequisite.

## Q2 — High: canonical-incompatible external JSON aborts audit insertion

Resolved and independently rechecked. The PA response path now validates its parsed sidecar against canonical JSON before constructing the final DTO. Unsupported canonical values produce an owned `invalid_json` error, retaining the complete original decoded evidence with `body=None`. The guide explicitly documents the supported integer and Unicode domain, whole-page refusal and absence of coercion. The reviewer's completed focused command passed **59 tests**, including real CLI integer/surrogate error persistence, safe scalar discard, PA HTTP policy controls and source replay tests: `pytest_exit=0`; `59 passed, 2 warnings in 5.48s`. The warnings are Typer/Click deprecations. The original finding evidence follows.

Follow-up supplied by the independent specification reviewer: uppercase/mixed-case JSON media types accepted by the PA operation client were still parsed through the generic text-body branch, bypassing the canonical numeric/Unicode admission. That follow-up is not claimed as an independent discovery here. Its repair is complete: PA media admission now selects the JSON parser independently of the generic parser's legacy behavior. The final independent selection passed lowercase, uppercase and mixed-case real CLI integer/surrogate refusal and safe scalar controls, alongside generic behavior controls.

`plugins/infrastructure/clients/http.py:_policy_json_body` admits parsed JSON without proving the resulting audit sidecar can be serialized by ELSPETH's RFC 8785 encoder. `_build_response_payload` inserts that value into `HTTPCallResponse.body`; the actual audit recorder hashes it. A JSON integer outside the encoder's safe integer domain, or an escaped lone Unicode surrogate, therefore raises an unclassified encoder exception at audit insertion. The complete bounded decoded body has already been captured, but the HTTP call and evidence never reach Landscape.

The reviewer controlled the instrument with an ordinary scalar row and then changed only that scalar, using the real CLI, real plugin manager/graph/engine/Landscape and durable controlled flow fixture. The page also contained the same valid business row in both cases:

```text
candidate 42 exit 0 exception NoneType http_calls 6 validation_rows 1 actions 1
candidate 9007199254740992 exit 4 exception SystemExit http_calls 0 validation_rows 0 actions 0
rfc8785._impl.IntegerDomainError: 9007199254740992 exceeds safe integer domain for JSON floats
```

The first control discards the malformed scalar candidate and publishes the valid candidate. The second fails before either candidate can follow ordinary row policy and renders the external number in its traceback. Independent controlled transport probes also measured:

```text
ordinary JSON string: success
escaped lone surrogate: CanonicalizationError input contains non-UTF-8 codepoints
```

These probes used synthetic credentials and intercepted public-IP HTTP requests; no tenant was contacted. The call counts in the CLI probe came from `calls_table`, validation counts from `validation_errors_table`, and action counts from the durable emulator target.

Required repair: keep the parsed audit sidecar canonicalizable under the PA policy and persist the exact bounded decoded bytes independently. Handle unsupported canonical numeric/Unicode values explicitly with closed boundary diagnostics; do not silently round or coerce external values. Preserve the documented candidate-row policy where applicable, or explicitly reject a noncanonical whole envelope before it reaches owned audit hashing. Add integer and Unicode controls that inspect persisted call evidence and rendered diagnostics through the real entry point, alongside an ordinary valid-value control. Source verification must apply the same explicit supported JSON domain.

## Q3 — High: replay drops linked quarantine validation evidence

Resolved and independently rechecked. Archive reconstruction now admits the complete source validation ledger, preserves original candidate shapes, and checks the linkage between each retained quarantine row and its decision. Replay records those decisions through the ordinary recorder, obtains new run-local error IDs, consumes pending linkage entries and binds each quarantined row explicitly. Missing, duplicate, foreign or ambiguous custody remains a refusal. Structured missing-field, extra-field and type-mismatch metadata is reconstructed using owned violation classes and a closed type registry. VERIFY compares complete ordered validation decisions. The final independent selection passed all source replay tests and the real LIVE → REPLAY → REPLAY-of-REPLAY → VERIFY control containing `None`, a nonmapping list, a genuine `_raw` mapping and a valid row; the tests check both original shape and run-local ledger links. The original finding evidence follows.

Before repair, the new scalar/list quarantine reconstruction correctly required original validation evidence to distinguish a nonmapping candidate from a genuine mapping whose only key is `_raw`. However, `engine/orchestrator/source_replay.py:replay_source_rows` restored only `validation_discards` and then yielded archived rows. Its replayed quarantined rows had no validation error record to link. A completed replay therefore could not itself be replayed: the next archive reader refused its ambiguous `_raw` payload.

The reviewer's short archive selection completed with `pytest_exit=1`: `1 failed, 5 passed, 2 warnings in 9.65s`. Empty-source replay/verify and all three accepted complete-run statuses passed. The quarantine candidate decision test failed because the replay run's `validation_errors` ledger was empty while LIVE contained two original candidate decisions. This was investigated beyond the supplied archive-status report rather than assumed to be a production defect from that failed assertion alone.

An independent real CLI replay-chain control completed successfully as an instrument and measured:

```text
live exit 1 ambiguous_raw False
first_replay exit 1 ambiguous_raw False
second_replay exit 4 ambiguous_raw True
source_reads 1 target_actions 1
```

The controlled page contained `None`, `["bad"]` and a valid business row, with source failures routed to an append-mode JSONL quarantine sink. Exit 1 for the first two runs is the expected completed-with-failures result. The second replay instead fails with `ambiguous _raw quarantine payload` during archive reconstruction. No extra source read or target action occurred. This exposes an incomplete retained archive, not an external-service failure.

Required repair: restore complete source validation decisions through the ordinary recorder path during replay and bind quarantined candidates to their restored error records. Preserve original candidate shape, error, destination and schema mode; do not weaken the ambiguity refusal or synthesize a guess from wrapped payloads. Prove chained replay for scalar/list candidates and a genuine `_raw` mapping, as well as ordinary discard behavior.

## Q4 — High: Azure SDK warnings bypass credential error sanitization

Resolved and independently rechecked. A permanent chained public `LogRecordFactory` now sanitizes relevant vendor records before passing arguments to previous factories. A ContextVar selects only the PA SDK import, construction, token and close scope. Logger names, messages, arguments, exception and stack details are fixed before formatting; unrelated contexts retain their original diagnostics. The factory supports `logging.makeLogRecord`'s unnamed placeholder, used by the configured processor formatter. The reviewer caught and rechecked that placeholder issue during repair, including the logging error handler's stderr path. The final independent selection passed real service-principal and managed-identity SDK warning/error controls under both console and JSON logging, a concurrent non-PA positive control, and real SDK teardown controls. A separate rerun of the original configured-formatter probe exited 0 with all four combinations measuring `safe_scoped=True unscoped_positive=True caller_chain_free=True`. The original finding evidence follows.

Before repair, the auth wrapper correctly returned `authentication_failed` without an external exception chain, but the real Azure identity SDK logged the exception itself before it returned control to that wrapper. Both `azure.identity._internal.get_token_mixin` and the SDK's `log_get_token` decorator emit token failures at WARNING with the exception as a formatting argument. ELSPETH's existing noisy-logger policy permits WARNING, including when the root is configured at WARNING. Replacing the credential with a simple fake in wrapper tests bypasses this SDK path.

The reviewer used the actual `ClientSecretCredential` with only its token-cache lookup and token-request operation controlled, preventing network access. A root `StreamHandler` captured records after actual `configure_logging(level='WARNING')`. The request raised a synthetic value-bearing SDK error. Measured output:

```text
caller_code authentication_failed context_is_none True
sdk_warning_contains_marker True
ClientSecretCredential.get_token failed: fake-private-sdk-secret-sentinel
```

The marker is synthetic; no credential was exposed. This demonstrates the real warning egress path the plan requires the wrapper to constrain. It can render endpoint, response or credential material carried by an SDK error despite the safe caller-facing error.

Required repair: establish scoped vendor diagnostic protection before SDK construction/token/close operations, covering both service principal and managed identity warning/error paths before external exception formatting. Preserve fixed operator diagnostics and unrelated plugins' concurrent logging. Temporary process-global logger level changes would introduce a race and are insufficient. Recheck with actual SDK logging behavior and a concurrent non-PA positive control, not only a fake credential that raises without logging.

## Q1 — Medium: unused outer client consumed ambient proxy configuration

Resolved and independently rechecked. `request_ssrf_safe` created an outer generic `httpx.Client` with the default `trust_env=True`, even though PA dispatch subsequently used a separate deadline client with proxies disabled. An unsupported ambient HTTPS proxy prevented the unused outer client from constructing and stopped an otherwise admitted PA request.

Before repair:

```text
ambient proxy absent: success, dispatches 1
HTTPS_PROXY=unsupported://fake-proxy-sentinel: HTTPPolicyError transport_failed, dispatches 0
```

The implementation now uses `trust_env=self._audit_policy is HTTPAuditPolicy.GENERIC` for that outer construction. Independent rerun:

```text
ambient_proxy False result=success dispatches 1
ambient_proxy True result=success dispatches 1
```

The scoped expression preserves generic clients' existing environment behavior. The HTTP owner also reported completed regression controls for generic proxy failure and legacy HTTP behavior; those reported suite results are not presented as this reviewer's own execution.

## Q5 — Medium: invocation-field normalization hides malformed archived values

Resolved and independently rechecked. Archive admission now validates exact canonical mode strings, a string-or-null replay reference and the producer's required nonlive reference before normalization. It binds those admitted values to the actual run record, while preserving the existing producer's ignored LIVE string reference. Effective-settings comparison requires complete owned top-level and concurrency keysets and a positive exact integer worker limit before explicitly deleting intentional invocation differences. The final late-boundary selection passed **116 tests**, including all PA client, CLI nonlive and core nonlive-loading tests and actual chained replay and empty-source controls: `pytest_exit=0`; `116 passed, 2 warnings in 18.08s`. A separate actual-Landscape rerun of the original instrument exited 0 and measured:

```text
intact accepted
malformed_mode AuditIntegrityError:power_automate_archive_settings_invalid
malformed_ref AuditIntegrityError:power_automate_archive_settings_invalid
contradictory_valid_mode AuditIntegrityError:power_automate_archive_settings_invalid
valid_ignored_live_ref accepted
```

The contradictory-mode control supplies both a valid VERIFY mode and a valid nonempty reference, proving that binding to the actual LIVE run is checked beyond simple value-domain admission. The original finding evidence follows.

The root's keyless review supplied an archive key-admission defect: defaulting removal of `run_mode`, `replay_from` and `concurrency.max_workers` concealed missing owned settings. The owner repaired keyset admission and the worker-limit domain. This reviewer then independently checked values in the fields intentionally removed before configuration comparison. The actual Landscape producer, with its original source and node records intact, accepted hash-recomputed archives containing `run_mode={"unexpected":42}` or `replay_from=[]`. The intact archive was accepted and `max_workers=0` was refused, controlling the instrument in both directions.

These fields are owned persisted evidence. Existence alone does not establish their domain, and removing them before comparison concealed malformed values. The repair admits the archived mode and replay reference domains and their relationship before normalization, retaining valid LIVE and chained nonlive archives.

## Scope and assessment

The reviewer read the plan and applicable repository instructions, then inspected the complete production changes and their supporting tests, schemas, examples, operating guide and inventory updates. The reviewed production paths include authentication and endpoint admission; decoded capture, decompression and absolute socket budgets; source pagination, schema and row decisions; member plans, receipts and sticky rejection; nominal factory admission and attempt revocation; archived models, loading and CLI propagation; scoped source verification; Web origin policy and evidence; and the shared JSON parser move and its consumers.

The implementation uses closed nominal authority interfaces, keeps live credentials in private typed configuration, checks admitted origin before credential attachment, and performs sink status before publication. Sink uncertainty remains non-success and cannot become row diversion. Deadline transport uses public interfaces and an owned short-write loop, and guards are checked again after waiting and at return. Ordinary source snapshot recovery uses the existing engine path rather than a second recovery mechanism. Replay and verify construct sinks without live HTTP authority. These choices are maintainable within the current architecture. The final CLI settings and HTTP request DTO changes also preserve owned immutable containers: secret resolutions are a tuple, while request JSON is detached and frozen before object and canonical-domain checks.

The full test scope was inspected, including controlled auth/transport tests, exact registry and schema pins, durable real-pipeline accounting and process-death recovery. The final independent focused command completed with `pytest_exit=0`: **135 passed, 2 warnings in 14.46s**. The warnings are Typer/Click deprecations. The selection comprises the complete PA operation-client, PA HTTP-policy, source-replay and HTTP request-contract modules, plus real CLI canonical-domain, quarantine replay-chain and empty-source controls and complete archive-status admission controls. Its lane-private log is `/tmp/pa-quality-final-recheck.log`. This review did not launch a full suite or PostgreSQL suite. It independently executed the positive/negative production probes quoted above. `git diff --check` completed with exit 0 after the final production repairs. `git check-ignore -v` returned no output for this report path before creation and final update.

The owner supplied an additional archive-admission issue found by the integration implementer: PA's early archive check required only COMPLETED, while the established engine accepts COMPLETED_WITH_FAILURES and EMPTY as well. This repair passed all three status controls and real quarantine/empty-source replay and verification in the independent final selection. It is not an independent finding of this review. The specification review's earlier snapshot identity and layer dependency findings were read for repair context; neither is claimed as newly discovered here.

The root's late keyless review also supplied an ADR-032 boundary defect: token admission checked the external SDK's nominal `AccessToken` class. The repaired boundary reads the foreign `token` attribute once, validates an exact nonempty ASCII value with no header whitespace or control characters, and constructs an owned frozen token DTO. It retains no unused expiry state. The reviewer inspected the complete repair and independently executed its real-SDK, dynamic-external-value, malformed-value, single-read and exception-context controls in the completed 116-test late-boundary selection. Its lane-private log is `/tmp/pa-quality-late-boundary-recheck.log`.

No cosmetic cleanup, additional approval ceremony or live-service gate is requested. The implementation is ready for the owner's frozen whole-tree gates. Those gates and subsequent local integration remain separate delivery evidence.
