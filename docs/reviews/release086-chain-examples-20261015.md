# Internal lookup and query chain examples

Qualified two new examples on 2026-10-15, using isolated source/data copies of
base commit `11d1d59e876dda0476aff89cf512dbaef0da5f6b` plus the fourteen current
production-file repairs. Their bytes were unchanged between snapshot and final
comparison. The private evidence records every source hash, command, exit and
input clock. The earlier [81-example qualification](release086-examples-validation-20261015.md)
remains a historical result; it was neither rewritten nor repeated.

| New example | Internal dependency in one plugin node | Measured outcome |
|---|---|---|
| [reference_join_chain](../../examples/reference_join_chain/) | Customer lookup produces `service_region`; the next lookup uses it to select a support policy. The original CSV has no region, tier, queue or SLA fields. | Validation 0; execution PARTIAL/1: three enriched tickets and two original-only quarantines. Audit attributes misses to lookup 0/customers and lookup 1/policies. |
| [llm_ordered_chain](../../examples/llm_ordered_chain/) | Ordered typed `extract` query produces amount/type; `apply` binds those generated fields explicitly. The original CSV has only expense ID and receipt text. | Validation 0; pipeline PARTIAL/1: three results and one original-only quarantine. Self-verifying launcher 0, 6.422 seconds. |

The reference example's successful audit states retain both ordered lookup
results. Reversing the lookups was rejected before execution. A disposable
default-policy control produced four enriched tickets and one quarantine: an
unknown customer received `GLOBAL` from the first lookup, then consumed its
`global-triage` policy through the second lookup.

The LLM example uses list-form named queries, `query_input_mode: ordered` and
`pool_size: 1`. Its request-sensitive ChaosLLM fixture runs on owned loopback
port 8199 with one worker and no injected transport faults. The normal ELSPETH
provider path makes real local HTTP requests. Stored audit payloads show each
second prompt contains that row's first structured response amount/type and
excludes the original receipt text. The three valid rows produce different
extractions and policy decisions. Seven row queries (2+2+2+1), one separate
runtime-preflight query and exactly one LLM graph node were verified. The
invalid numeric extraction fails at query 0, with no consumer query.

Reversing queries, omitting ordered mode and setting pool size 2 each failed
validation with its intended diagnostic and no audit database. The verifier
also rejected a disposable duplicate-success-row mutation. Ruff, formatting,
shell syntax, canonical discovery and the examples index checks passed.
After the two additions and the explicit auxiliary-fixture catalog entry,
canonical discovery measured **106 pipeline configs in 58 directories**
(60 total example directories).

Runs used the existing private Python 3.13.15 environment with both source
roots explicit; no dependencies were installed or synchronized. The LLM
module's raw SHA256 was
`774c2de7c5489f23cca132b1f60396ca091074d7472a1ffe3729be550eb6d83f`;
its declared and independently computed canonical pin both were
`sha256:705f5c0d4bb975a2`. Source/data input clocks remained frozen during each
recorded command. This qualifies local wiring, typed dependency handling and
audit attribution; it makes no real-provider or model-accuracy claim.

Initial attempts are retained privately: a default-control key typo, an
overly short one-second example wait budget that caused a row timeout and
scheduler cleanup cascade, and a verifier assumption that canonical JSON
preserves the lexical float form. The shipped example uses a sixty-second
wait budget and accepts JSON numbers; its final fresh run passes with the
invalid extraction still present. No engine repair or timeout-cleanup
correctness is claimed.

Detailed receipts: `.claude/lanes/release086-examples-20261015/NEW-CHAIN-EXAMPLES.md`,
`NEW-CHAIN-EVIDENCE.json`, `NEW-CHAIN-FINAL-FILES.json` and the isolated
`new-chain-*` case logs. These checks do not include the separate refresh of
the prior twelve affected LLM configs/seven launcher groups or renew their
earlier certificates or the complete release qualification.
