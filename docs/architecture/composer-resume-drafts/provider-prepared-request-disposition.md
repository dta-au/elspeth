# Physical provider request preparation — source-only disposition

Inspected current root callpaths during gate freeze. No tracked/root edits, app imports, tests or execution. Candidate-build design remains open independently.

## Concrete duplicate and smallest safe patch

`composer/diagnostic_evidence.py:121–126` validates retained content with `len(self.content.encode("utf-8"))` and `_digest(self.content)`; `_digest:90–91` encodes the SAME immutable str again. Safe local change: assign `encoded = self.content.encode("utf-8")`, then preserve short-circuit `len(encoded) > bound or hashlib.sha256(encoded).hexdigest() != retained_sha256`, followed by unchanged canonical/decode checks. Keep encoded bytes local to __post_init__, not retained on every historical receipt. Invalid Unicode/oversize/hash/malformed-content behavior and exception translation must stay identical. This removes one full-size transient encoding, not canonical parsing or independent integrity verification. No speed/memory claim until measured.

A similar producer `_retained` serializes then decodes before DiagnosticPayload validates again; that apparent redundancy is NOT approved for deletion: producer maps bound failures to unavailable while decoder constructor rejects malformed persisted data. AuditStorage supplies digest and constructor recomputes it as validation. Keep boundary validation independent unless introducing a separately owned typed construction contract with strict provenance.

## Actual representations and ownership

- provider_gateway.py:758–765 detaches trust evidence, adds provider cache markers, captures private context before provider await. Primary prose has its own capture:975; Advisor checkpoint:521–526; planner:3759. Physical caller binds request to its own resulting call/attempt record. Capture itself is not proof of dispatch.
- provider_context.py:229–258 recursively copies under traversal budget, checks message/tool grammar and credential/dataURL restrictions, canonicalizes the supported envelope, checks encoded bound, then produces PrivateProviderContext. Unsupported/credential/bounds outcomes are explicit unavailable receipts; never retain a safe prefix as complete request.
- contracts/composer_provider_context.py:12–35 stores repr-hidden immutable canonical str/disposition/reasons and independently checks encoded size. It currently has no guard or live-operation authority.
- provider_gateway.py:482 deepcopies kwargs before detaching provenance again; :525–536 checks native thinking and credential material; :537–549 runs promptguard; :550–552 applies transport headers and admits current custody; :554–567 handles callback failure cancellation; :568–569 marks SDKentry and calls SDK. These are ordered physical-attempt boundaries, not work to skip using a precomputed hash.
- prompt_guard.py:195–267 hashes a DIFFERENT canonical object: role, policy fingerprint, genuineuser, exact messages/tools, trust indexes/partitions and size policy using json.dumps(ensure_ascii=True,sorted keys,compact separators,allow_nan=False). Private capture uses RFCcanonical envelope with schema_version/capture_boundary. Their byte lengths/digests cannot be shared as if identical wire content.
- audit_storage.py:147–169 later embeds request canonical in DiagnosticPayload and computes digest; DiagnosticPayload verifies size/hash/canonicality. ComposerDiagnosticEvidence envelope then hashes broader operation/attempt-bound evidence:235. This envelope hash differs from requestcontent and guard hashes.

## Smallest per-attempt owned representation worth reviewing

Prefer evolving the existing PrivateProviderContext boundary over an all-purpose prepared transport request. An internal immutable canonical-content value may carry original canonical str plus locally derived `utf8_length` and `sha256` (init=False, recomputed from current string), unavailable disposition/reasons, and no raw transport kwargs/APIkey. Avoid permanently storing a second full bytes copy unless allocation measurements justify it. No cache keyed by str identity or request equality.

Only pure derivations of that exact immutable canonical string can be reused. Direct constructor/fresh persisted read must still validate independently; dataclasses.replace must recompute; public inputs must not author digest/size. DiagnosticPayload persisted decoder remains strict. Scalar memo may save producer-side repeat computation, but validation at a separate corruption boundary must not trust caller-provided digest. Enforcing nominal type alone is not a minted authority capability.

If broader messages/tools preparation is attempted later, own a deep-frozen canonical supported JSON tree and hand a fresh mutable detached copy to SDK for EVERY attempt. Keep role/provenance/trusted partitions separate and validated against exact copied message positions. NonJSON/provider extensions must retain current unsupported capture behavior while allowed SDK request behavior remains unchanged. Canonical decoding can change numeric representations, so do not reconstruct SDK messages from RFCcanonical text without type-parity proof. Caller/SDK mutation cannot alter capture or future attempts.

Bind prepared artifact to one physical call owner/call ID, operation fence/epoch, role and exact marked message/tool snapshot; preserve omitted tools versus empty tools. New repair/retry/Advisor attempt mints a new receipt even when bytes match. No cross-attempt guard decision, credential approval, quota admission, lease validity, request-budget or publication verdict is cached. Prepared strings are data, not authority. Lease/budget checks stay where they execute today.

## Why not a broad patch now

Capture preparation occurs before the gateway's isolated kwargs and guard processing, while transport adds headers after guard. A unified artifact would need a complete proof linking these variants without moving existing preparation/physicaldispatch timestamps or bypassing unsupported-content handling. SDK ownership, provenance grammar and native thinking differ from private retention grammar. Hash domains are intentionally different. The unconditional deepcopy is a concrete SDK mutation barrier; removing it requires a new sole-owner transfer boundary not present today. Current immutable canonical strings already provide some reuse, and safe derived-scalar memo saves less than the larger independent parser/projection work. Do not advertise this as resolving all request overhead.

## Controls and measurements

1. Local encode reuse: retained/filtered/unavailable, exactly-at/over encoded limit, multibyte, malformed/lone surrogate, wrongdigest, duplicatekeys, noncanonicalnumbers, native wiretext; preserve error types and unavailable mapping. Counter test proves one local encode per validation without bypassing hash/decode checks; compare actual accepted/rejected outputs.
2. Any canonical carrier: direct malicious construction, stale digest/length, replace with changed string, persisted decode, frozen identity, no repr leak; constructor fields init=False, independent parser remains.
3. Caller mutation after capture and SDK nested mutation cannot affect retained receipt or another attempt. Same logical bytes across2attempts still produce2distinct physical records and quota/guard/custody actions.
4. Primary/prose/Advisor/planner paths, cache markers, native thinking, toolsNone versus[], genuineuser/trusted/document partitions, credential fields/dataURLs, unsupported/bounds reasons exactly retained. Guard blocked/unavailable/cancel, preSDKcallback failure, timeout and SDK failure keep actual outcome/audit ordering.
5. Factorize transcriptbytes and attempts, count serializations/encodedbytes/digests/deepcopies and CPU/temporary allocations. No extra raw request logging. Compare correct immutable privatecapture bytes separately from SDK wire/native accounting; existing23%wholefeature timing does not measure this proposal.

Recommendation: approve only the local one-encoding validation simplification as a bounded implementation candidate after review; retain independent hash/canonical verification. Instrument remaining derivations before designing an owned prepared-request transfer. Larger request reuse remains explicitly open.
