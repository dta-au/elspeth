# Envelope splat repair review

Specification verdict: **PASS** for this bounded repair, including the explicitly authorized two-entry forwarder-census follow-on.

Quality verdict: **PASS**. No blocking or nonblocking findings in the reviewed patch. The broad backend gate remains parent-owned and outstanding; this review does not close the broader D1 work or claim whole-suite clearance.

Reviewed on 2026-11-09 against base/HEAD `ef14c2fc18fba93158ae7a6cfe004a4537c7ef98` in `/workspace/elspeth/.claude/worktrees/composer-remediation-wave1`. Read repository AGENTS.md and the relevant CONTRIBUTING.md whole-tree, test, provenance, and Composer guidance, the canonical brief (including its follow-on authorization), implementation report, supporting baseline analysis, frozen patch, source, and completed evidence. No tests were rerun, production files edited, or agents spawned. This note is the only review write.

## Specification assessment

- `tests/unit/web/composer/test_tool_result_envelope_gate.py:404` implements precisely the missing inline `ast.Dict` and recursive `ast.IfExp` cases. Both alternatives are consumed in order, with no predicate evaluation, exception suppression, or readable-sibling shortcut. Inline dictionaries reuse `_dict_literal_keys`; constants, local resolver, and `dict_forwarded` survive both recursive arms. The inner walk starts with an empty prefix and the caller at line 556 applies the outer prefix once.
- Existing ownership, named/helper attribution, and unreadable-expression behavior remain in place. The conversion to a generator preserves the prior yielded contents of own-data, attributed-name, local, and helper cases. Unknown mappings still raise. Reused `_key_of`, `_dict_literal_keys`, and `_nested_value_keys` preserve dynamic-key, immediate-value-cast, and nested `dict(...)` cast refusal.
- Controls at lines 1488–1578 cover inline/nested mappings, both nonempty alternatives, each empty alternative, ordered prefixed output, imported/module constants, handler-local resolution, and an attributed helper. Each unreadable alternative is parameterized in both arms and matches the intended site and refusal text. All four outer/inner conditional cast combinations are checked; casts in predicates or helper inputs remain outside the shipped mapping.
- The real-producer controls at lines 2529–2584 find the unique conditional splat inside `_execute_explain_validation_error`, establish the original constructor's exact keys and a passing unmutated gate, then separately plant the unique untaught key and `success` into each actual arm. They assert census visibility at the real constructor before requiring the selected teaching/homonym gate to fail for that key. `_parse` returns a fresh AST on each read (line 978), so mutations neither accumulate nor modify the producer file. An old generic `IfExp` refusal cannot satisfy these controls.
- No preceding D1 structural assertion was changed. Static AST comparison against the base reports only `_splat_keys` and `_dict_literal_keys` as changed existing top-level envelope functions, with none removed. This preserves the mutation/alias and consumer checks, constructor ownership, proposal/rejection exact-key pins, and unapplied-state checks at lines 3943–4034. Their broader adequacy is outside this repair review.

## Exact forwarder provenance and controls

The additions at `tests/unit/web/composer/test_error_code_redaction.py:119` are justified by the current producer, not merely by the failing inventory:

- `src/elspeth/web/composer/tools/generation.py:2194` calls `explain_validation_code(error_text)` and emits `error_code=error_text` only in its non-None branch. The accessor at line 2023 rejects non-string/empty input and performs exact `_VALIDATION_GUIDANCE_BY_CODE.get(code)` lookup; unknown input returns None.
- At generation.py:2210, `codes` contains only catalogue keys whose record **is** the first matching regex record. `matched_code` is either an element of that list or None; the conditional at line 2222 omits None. Unowned and undistinguished shared-record prose cannot invent a code.
- `test_error_code_redaction.py:375` retains the live catalogue-subset-of-`REGISTERED_ERROR_CODES` assertion and its planted-unregistered-key control. The live forwarder census and stale-entry check remain unchanged. Runtime catalogue tests at `test_validation_guidance_catalogue.py:18`, `:28`, `:41`, `:47`, `:64`, and `:150` retain runtime-reason omission, canonical codes, unknown/unowned/ambiguous omission, record identity, and exact accessor behavior.
- New controls at test_error_code_redaction.py:344 prove the actual scanner sees one nonliteral site for each authorized tuple. The six near misses at line 351 change expression, enclosing function, or path individually and demand the exact unreviewed tuple. They do not broaden matching or suppress arbitrary forwarders.

No production payload, registry, teaching declaration, fence, signature, fingerprint, or census count was changed. The only inventory change is the two exact tuples expressly authorized by the appended brief.

## Evidence and limits

Independently checked the saved patch SHA256: `4f884d7245ff3516c8a9e223f43bc369060dfbc38fa8b7eb36b48c40b9c3cecd`. The staged diff against the named base equals that artifact byte-for-byte. Current status contains only the two staged test files plus the preexisting untracked frontend `node_modules` path.

Read completed logs together with their exit files:

- Initial focused red: 28 failed / 1 passed, exit 1; focused repair verification: 29 passed, exit 0.
- Initial full affected selection: **877 passed / 1 failed**, exit 1. The sole failure is the real forwarder census identifying exactly `error_text` and `matched_code`; this run is not described as globally green. The envelope and walker-authority portion passed.
- Forwarder-control red: 2 failed / 6 passed, exit 1, identifying the missing exact positive tuples while all near misses remain refused.
- Final census/catalogue/response selection: **664 passed**, exit 0. Scoped Ruff and format checks both exit 0.

Both before/after freeze pairs compare equal. Comparing the two completed freezes finds exactly one changed entry: `test_error_code_redaction.py`. Every current file in the final freeze matches its recorded hash. The envelope file remains `a8b42e436328e7dbcff845d0f3d84310f1c00aef06b7424885349f743369cda7`; final census hash is `14b649597141f1781f658782229eec5bdcf84736bdeb273d669bc1b185398a3b`. Both provenance logs identify the expected worktree package roots and base HEAD. Reusing the expensive envelope result is justified by the unchanged scanner and inputs and follows the brief's express direction.

The historical 14 failures are correctly identified as the prior diagnostic-remediation regression, not an unrelated upstream baseline. This review approves the bounded repair for parent integration subject to the parent-owned broad backend gate; it makes no PostgreSQL or operator-signature clearance claim.
