# Tracker label vocabulary (`p1-class:*`, `lane:*`)

Filigree label namespaces whose meaning lives only in the tracker database.
Written 2026-08-17 because the vocabulary had no definition anywhere in the
tree, so every session re-derived it by sampling issues.

## `p1-class:*` — why is this P1?

A closed 7-value vocabulary answering *what does it cost if this is not done*.
It is **not** a subject-matter axis — that is `lane:*` below.

| Value | Meaning |
|---|---|
| `loud-demo-bug` | Defect a user driving the demo sees directly: the guided plan fails, the composer announces ready when it is not, repair exhausts on the canonical prompt. |
| `conditional-demo-bug` | Breaks the demo only under a condition — concurrency, timing, a timed-out worker. Invisible on a clean single-user pass. |
| `quiet-bug` | Real correctness/integrity defect that never surfaces in the demo: audit lineage, non-atomic persistence, unredacted egress, test/CI contract rot. |
| `demo-enablement` | Not a defect — capability or deployment work the demo needs (multi-replica, Kubernetes, Azure, a new plugin). |
| `release-assurance` | Leaf work that gates or assures a release: staging steps, gate repairs, contract migrations, version surfaces. |
| `coordination-rollup` | A container coordinating multi-part delivery — milestone, phase, active epic, or a closeout task that fans in. |
| `bug-rollup` | A bug ticket coordinating child bugs. Rare (one use). |

Classify by **impact**, not by issue type. Every type is eligible: `bug`,
`task`, `feature`, `milestone`, `phase`, `step`, `epic`.

### Scope: current release horizon only

Work outside the active release horizon sits outside this axis and stays
unlabelled. In practice that means anything carrying **`pre-cutover-archive`**
— the RC5 / RC5.1 clusters and the closed-out 0.7.0 epics. Seven P1 epics were
deliberately left unlabelled on 2026-08-17 for this reason; six carry
`pre-cutover-archive`, and the seventh (`elspeth-1040aa2143`, active,
`release:1.0`) was labelled because it is in-horizon.

Do not mint new values. If something genuinely fits none of the seven, raise it
as a vocabulary decision rather than inventing an eighth.

### First-time extensions made 2026-08-17

Recorded so the next session can tell observed policy from this session's
judgment calls:

- **`task` → `loud-demo-bug`.** Previously bug-only (9/9). Applied to
  `elspeth-1318049ffe` and `elspeth-2ed41f0a4a`, both demo-visible defects
  filed as tasks. Consistent with classifying by impact rather than type.
- **1.0-horizon work → `release-assurance`.** Previously all-0.7.2. Applied to
  the five state-engine contract-closure steps. **`release-assurance` therefore
  spans two release horizons and is no longer a 0.7.2-only filter.** This is
  the one call worth revisiting — see below.
- **`epic` → `coordination-rollup`.** Previously container-but-not-epic
  (milestone/phase/step). Applied to `elspeth-1040aa2143`.

**Open adjudication:** either `release-assurance` legitimately spans release
horizons, or the five state-engine steps want a distinct value. Unresolved.

## `lane:*` — which workstream?

The subject-matter axis. Sparsely applied and open-ended; add values as
workstreams appear. Current: `analyzer`, `attribute-contracts`, `composer`,
`core-runtime`, `judge-gate`, `judge-reliability`, `operator-handoff`,
`plugins`, `residual-policy`, `release-closeout`, `staging`, `web-deployment`,
`web-runtime`.

Use `p1-class:*` to decide *whether* to work something and `lane:*` to decide
*where it belongs*. They are orthogonal: a `quiet-bug` can sit in any lane.

## Keeping this current

`filigree labels --namespace p1-class --top 0` (MCP: `label_list`) prints live
counts including values used only on closed issues — a value absent from open
work is still part of the vocabulary. To find issues missing the namespace
entirely, use MCP `issue_list(not_label="p1-class:")`; the trailing colon makes
it a prefix exclusion. Update this file when a value's meaning shifts, an
adjudication lands, or a namespace is added.
