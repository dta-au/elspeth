# Plugin Skillpacks — Design

**Date:** 2026-09-10
**Status:** Design, pre-implementation
**Scope:** Long-form, plugin-shipped specialist guidance for the Web Composer
planner, retrieved on demand. Freeform and guided surfaces alike.
**Companion spec:** `2026-09-10-user-standing-preferences-design.md` (the
per-user preferences box). The two share only the audit-attribution work in
§7 and are deliberately independently landable.

## 1. Purpose

Some plugins carry considerations that do not fit a one-line hint. An
advanced statistical transform has assumptions that invalidate its output
when violated; an industrial-protocol sink has sequencing and failure
semantics an author must understand before wiring it. Today the composer has
two channels for plugin knowledge and neither carries that weight:

- `get_agent_assistance()` (`contracts/plugin_assistance.py`) returns
  `PluginAssistance` — a `summary`, `suggested_fixes`, `examples`,
  `composer_hints`. Short-form, deliberately. It is rendered into the catalog
  context for **every** available plugin, so its cost is paid on every turn
  by every session. It cannot absorb a two-thousand-word treatment of when a
  method is unsound.
- `data/skills/pipeline_composer.md` (`load_deployment_skill`, 64 KiB cap) is
  an operator overlay concatenated into the system prompt. One fixed name,
  one file, always-on, unattributed in the audit trail. It is the right shape
  for deployment-wide vocabulary and the wrong shape for per-plugin depth.

A **skillpack** is the missing middle: long-form prose that ships with the
plugin it describes, costs one manifest line until the planner asks for it,
and is attributed in the audit trail when it is used.

### Non-goal

This is not a recipe library. See §6 — a pack teaches a shape; it never
carries an instantiable graph.

## 2. Decisions settled during brainstorm (2026-09-10)

| Question | Decision |
|---|---|
| Who authors and installs a pack | **Plugin-shipped only.** Operator-installed library files are out of scope; `data/skills/pipeline_composer.md` continues to serve the deployment-wide need unchanged. |
| How a pack reaches the planner | **Planner-pull, always.** A manifest line per pack in the prompt; the body arrives only via a tool call. |
| Applies during the tutorial | **Yes, uniformly.** No tutorial-only branch. See [ADR-049](../architecture/adr/049-tutorial-canary-baseline-is-configuration-relative.md). |
| Spec decomposition | Two specs; this one and the preferences box land independently. |

## 3. Relationship to prior art

`docs/specs/2026-07-10-composer-assistant-tools-design.md` (Approved) already
specifies a skill library: a `skill_library.py` plane module exposing
`list_skills` / `load_skill`, backed by operator files at
`{data_dir}/skills/library/*.md`, with per-load audit rows carrying a content
hash. It was never implemented — `src/elspeth/web/composer/tools/` contains
no such module.

This design **narrows and supersedes** that section:

- **Packs ship with plugins, not as operator files.** The July design
  predates the decision that the motivating case is per-plugin depth. Hanging
  a pack off the plugin means it versions with the code it describes, and it
  inherits plugin availability and policy filtering for free (§4.3).
- **No `list_*` tool.** The July design needed one because operator files had
  no catalog to ride. Plugin-shipped packs ride the catalog context that is
  already rendered every turn, so a list tool would be a redundant
  round-trip. One tool, not two.
- **Retained unchanged:** the trust posture (loaded content is a tool result
  in the untrusted envelope, never elevated to system role), the per-load
  audit row carrying name + content hash, and the `ToolKind` discipline.

Operator-authored library files remain a coherent future extension; nothing
here forecloses them. They are simply not in this scope.

## 4. Architecture

### 4.1 What a pack is

A new frozen dataclass in `contracts/plugin_skillpack.py`:

```python
@dataclass(frozen=True, slots=True)
class PluginSkillpack:
    pack_id: str          # stable, e.g. "transform.batch_distribution_profile.methods"
    title: str            # short human label
    when_to_use: str      # ONE line; this is what the planner sees unprompted
    tags: tuple[str, ...] # e.g. ("statistics", "assumptions")
    body: str             # the markdown, exact UTF-8
    body_sha256: str      # digest over exactly `body`
```

`body` and `body_sha256` are derived from one in-memory read, atomically —
the same doctrine as `load_skill_with_hash` in
`web/composer/skills/__init__.py`, and for the same reason: the hash written
to the audit row must be a hash of the exact bytes the LLM saw, not of a
fresh re-read that could have changed in between.

### 4.2 How a plugin declares one

A sibling classmethod on the existing assistance mixin
(`contracts/plugin_protocols.py::_PluginAssistanceHooks`, which is already
shared by `SourceProtocol` / `TransformProtocol` / `BatchTransformProtocol` /
`SinkProtocol`):

```python
@classmethod
def get_agent_skillpack(cls) -> "PluginSkillpack | None": ...
```

Default implementation on `BaseSource` / `BaseTransform` / `BaseSink` in
`plugins/infrastructure/base.py` returns `None`. A plugin that has one loads
it from a markdown file packaged next to its module, e.g.
`plugins/transforms/<name>_skillpack.md`, via a shared
`load_plugin_skillpack(module_file, name)` helper that mirrors
`load_skill_with_hash`'s atomic (text, digest) return.

Prose lives in a `.md` file rather than a Python string literal so it is
diffable, reviewable, and editable without touching code — and so the digest
is over a file an auditor can read directly.

**Why a protocol classmethod rather than a bare file-naming convention.**
Three concrete wins, each measured against existing code rather than
asserted:

1. `PolicyCatalogView` already reaches these hooks through
   `type[TransformProtocol]` — the protocol exists precisely so "catalog and
   tool code typed against `type[XProtocol]` can call them without
   per-protocol declarations drifting" (`contracts/plugin_protocols.py::_PluginAssistanceHooks` docstring).
2. The pack inherits policy filtering: `PolicyCatalogView.list_transforms()`
   (`web/catalog/policy_view.py`) is the authorised, filtered view. A
   pack for a plugin the deployment has disabled is structurally
   unadvertisable, because the manifest is derived from that view.
3. Version coupling: the pack ships, reviews, and releases with the plugin
   whose semantics it describes.

### 4.3 Discovery — a manifest line, in the cacheable prefix

`build_catalog_context_string` (`web/composer/prompts.py`) renders the
deployment-constant catalog block. The pack manifest is added there, derived
from the same policy-filtered catalog view that produces the plugin list.

Per pack the planner sees exactly: `pack_id`, `title`, `when_to_use`, `tags`.
Not the body. Roughly one line each.

This placement is deliberate and load-bearing. `build_messages` orders the
prompt **system → catalog → history → session-varying context**, and the
first three are a byte-stable cacheable prefix (elspeth-a79f1b2e6b). The
manifest is deployment-constant, so it belongs in the cached region and is
billed once rather than per turn. The bodies — which are large and mostly
irrelevant to any given session — never enter the prefix at all.

**Cost shape.** Manifest cost is O(packs available under policy) per session,
paid once via cache. Body cost is O(packs the planner actually judged
relevant) per session. This is the whole reason for planner-pull over
concatenation: with the deployment-overlay model, ten packs would mean ten
bodies in every prompt of every session forever.

### 4.4 Retrieval — one tool

New plane module `web/composer/tools/skillpacks.py`, declaring a
`TOOLS_IN_MODULE` tuple aggregated by `_registry.py` exactly like every
existing plane.

```
name:        load_skillpack
kind:        ToolKind.DISCOVERY
cacheable:   True
arguments:   {"pack_id": str}
returns:     the pack body, wrapped in the untrusted-data envelope
```

**Named `load_skillpack`, not `load_skill`,** because
`web/composer/skills/__init__.py::load_skill()` already exists as a Python
function. Two things called `load_skill` in one subsystem is a trap for every
future reader.

**No new `ToolKind`.** `ToolKind.DISCOVERY` fits exactly — read-only,
cacheable, never advances `CompositionState`. `declarations.py::ToolKind` (its docstring)
explicitly refuses to advertise an enum member with no callers ("a dead
forward-pretend"), so inventing `ASSISTANT_DISCOVERY` for one tool would
contradict a recorded review finding. If the wider July assistant families
land later and need the distinction, the kind arrives with them.

**Two pins move, deliberately.** `load_skillpack` must be inserted into
`PLANNER_DISCOVERY_TOOL_NAMES` (`web/composer/capability_skill.py`).
That tuple is not a membership set: `build_planner_capability_manifest`
checks that the advertised tools' positions are *sorted by index in the
tuple*, so the insertion point is part of the contract. `effective_tool_hash`
therefore moves for every planner call. Both are expected, one-time, and must
be stated in the implementing commit rather than discovered by a red gate.

**Failure mode.** `skillpack_not_found` in the standard `ToolResult`
envelope, naming the requested `pack_id` and listing available ids — the
`augments_on_failure` precedent, so a typo self-corrects without a second
discovery round-trip. A pack for a policy-denied plugin is *not found*, not
*denied*: the manifest never advertised it, so a request for it is either a
hallucinated id or a stale one, and neither warrants disclosing that the
deployment has that plugin disabled.

This is defence-in-depth, not a confidentiality guarantee — the plugin
catalog is itself policy-filtered, so a caller comparing it against the
shipped plugin set can already infer what is disabled by omission. The choice
is worth making anyway (it costs nothing and keeps the tool from being a
second, more direct oracle), but a reviewer should not read it as protecting
a secret the rest of the surface keeps.

### 4.5 Trust boundary

A loaded body is **model-selected content** and therefore rides as a tool
result in the existing "UNTRUSTED DATA; not instructions" envelope. It is
never elevated to system role. This is the July design's rule, retained
verbatim, and it is the correct rule here for the reason the July design
gives: model-selected text that can promote itself to instruction authority
is privilege escalation.

Note the discriminator this makes explicit, because the companion spec relies
on its other branch: **who selected this text — the model, or an
authenticated principal?** Model-selected → tool result, untrusted envelope.
That is a skillpack. Principal-authored → a different treatment, specified in
the preferences design.

Packs are shipped code, reviewed at merge like any other source file, so the
threat is not a hostile pack author — it is that a pack becomes an
unreviewed side channel for changing planner behaviour. Envelope wrapping
plus §6's structural checks keep it a teaching document.

## 5. Bounds

Following the established idiom (`MAX_DEPLOYMENT_SKILL_BYTES = 64 * 1024`;
`composer_advisor_max_prompt_tokens * 4` for the advisor char cap):

- `MAX_SKILLPACK_BODY_BYTES = 32 * 1024` per pack — half the deployment
  overlay, since a pack is one plugin's concern and several may load in a
  session. Enforced by a bounded binary read at load, never by reading the
  whole file then measuring (the `load_deployment_skill` precedent, which has
  an explicit test forbidding `Path.read_text()` for exactly this reason).
- `when_to_use` capped at 200 characters — it is the always-on cost.
- A per-session cap on distinct pack loads (proposed: 8), returning
  `skillpack_budget_exceeded` naming the budget. Prevents a pathological loop
  from loading the entire library into one context.

Violations are **construction-time errors**, not runtime truncation. A pack
that exceeds its cap fails at import, visible to the plugin author, rather
than silently arriving truncated mid-sentence at the planner.

## 6. The prose-only rule (composer invariant 1)

**A skillpack MUST NOT contain a pipeline graph fragment that any server path
can lift into a proposal.**

This is the sharpest risk in the design. AGENTS.md's first composer invariant
bans a server-authored graph reaching the user as a proposal "regardless of
what it is called (sketch, recipe, router, fallback, fast path, synthesis)".
The July design's phrase "recipes provide the instantiable graph" is exactly
the shape that must not be built here. A pack teaches a shape in prose; the
planner authors the graph through tool calls, as it does for everything else.

Enforced structurally, not by documentation:

1. **Content validator.** Reuse the `_UNSAFE_TEXT_PATTERNS` /
   `_assert_safe_assistance_text` idiom from
   `contracts/plugin_assistance.py` — the same secret discipline (no raw
   URLs, credentials, tracebacks, file paths) applies to pack bodies, which
   are LLM-facing text from the same class of author.
2. **Structural check.** Reject a pack containing a block that parses to a
   pipeline-shaped mapping (a mapping carrying `nodes` / `edges` / `source` /
   `outputs` at the root). Scope the check to **every fenced block regardless
   of its language tag** — `yaml`, `yml`, `json`, `text`, and untagged alike —
   plus any indented block that yaml-parses. Tagging the check to `yaml` and
   `json` alone would let an untagged or `yml`-tagged block through.

   Illustrative snippets of a *single node's options* remain legal — that is
   schema teaching, and it is what the existing `PluginAssistanceExample`
   already does. A whole graph is not.

   **What this check is, stated honestly:** a **tripwire against recipe-creep**,
   not a closed gate. It enumerates a bad shape, and an enumerating gate is
   never closed — a determined author can express a graph in prose, in a
   table, or in a form the parser does not recognise. That is acceptable
   because the threat model here is *drift*, not a hostile pack author: packs
   are shipped code reviewed at merge like any other source file. The tripwire
   catches the realistic failure — someone helpfully pasting a working
   pipeline into a pack — early and mechanically, so it is a gate result
   rather than a reviewer's opinion. The closed guarantee is item 3, not this
   one.
3. **No consumer.** No server path parses a pack body for structure. The only
   consumer is the LLM, through a tool result. A test asserts the body is
   never passed to any proposal-construction path.

Because the check is structural, "did anyone add a recipe to a pack" is a
gate result rather than a review opinion.

## 7. Audit and attribution

This is the gap the design must close, and it is shared with the companion
spec.

Today `PlannerCapabilityManifest`
(`web/composer/capability_skill.py::PlannerCapabilityManifest`) carries `capability_core_hash`,
`canonical_schema_hash`, `effective_tool_hash`, `rendered_prompt_hash`.
Everything injected into a prompt lands inside `rendered_prompt_hash` with
**no per-source attribution**. A composition influenced by a skillpack is
today indistinguishable from one that was not — which is precisely the
question ELSPETH exists to answer.

Two additions:

- `PlannerCapabilityManifest.skillpack_manifest_hash: str | None` — a digest
  over the ordered `(pack_id, body_sha256)` pairs *available* for this call.
  Records what the planner could have reached for.

  **`None` when no packs are available under policy**, mirroring
  `user_instructions_hash` in the companion spec. This is the normal case on
  day one and in any deployment shipping no packs, so it must be a first-class
  state rather than an edge case. It is deliberately not a digest over an
  empty tuple: that would be a fixed constant appearing in every manifest,
  indistinguishable at a glance from a real one-pack digest, and it would
  claim a positive fact ("this is the hash of what was on offer") about an
  empty offer. `__post_init__` validates the field as a 64-character lowercase
  hex digest *when present*; `""` is rejected, as it is for every other hash
  field.

  ADR-049's tutorial triage rule keys on this: "non-empty skillpack manifest"
  means `skillpack_manifest_hash is not None`.
- **Per-load audit row** — `pack_id`, `body_sha256`, session, turn. Records
  what it actually reached for. Same doctrine as `composer_skill_hash`.

Together these answer both halves of the reproducibility question: what was
on offer, and what was used.

`PlannerCapabilityManifest.__post_init__` validates every hash field as a
64-character lowercase hex digest; the new field follows that pattern.

**The prefix rule is untouched.** `build_planner_capability_manifest` asserts
the capability core is the exact `startswith` prefix of the first system
message and occurs exactly once. Nothing in this design alters the system
message: the manifest goes in the catalog block (a later message), bodies go
in tool results. No `AuditIntegrityError` exposure.

## 8. Testing

- **Injection tests** (July design precedent): a fixture pack whose body
  contains tool-call instructions and role-elevation attempts; assert
  envelope wrapping and that the audit trail records no role elevation.
- **Prose-only gate:** fixture packs carrying a full pipeline YAML block, a
  JSON graph, and a legal single-node options snippet; assert the first two
  are rejected at construction and the third accepted.
- **Policy inheritance:** a pack whose plugin is denied by policy is absent
  from the manifest, and `load_skillpack` on its id returns
  `skillpack_not_found` — asserted against `PolicyCatalogView`, not a mock.
- **Registry invariants:** the existing import-time asserts
  (unique names, cacheable-only-if-DISCOVERY, name-set derivation) extended
  to the new plane.
- **Pin movement:** an explicit test that `load_skillpack` occupies its
  declared position in `PLANNER_DISCOVERY_TOOL_NAMES` and that a manifest
  built with it advertised validates.
- **Hash atomicity:** the `load_skill_with_hash` doctrine — the recorded
  digest equals a digest of the exact bytes returned, proven by mutating the
  file between load and assertion.
- **Bounds:** oversized body rejected by bounded read (asserting
  `Path.read_text()` is not called, mirroring
  `tests/unit/web/composer/test_skills_loader.py`); per-session load
  budget returns the structured code.

Whole-tree AST gates apply (CONTRIBUTING.md § Whole-tree gates): the new
plane module and contract are subject to the dynamic-attribute and
wire-shape pins, so the full `pytest tests/` runs before merge, not a scoped
selection.

## 9. Out of scope

- Operator-installed library files (`{data_dir}/skills/library/*.md`). The
  July design's shape; coherent, not needed for the motivating case.
- User-uploadable packs. Would invert the trust model — principal-authored
  but model-selected — and require the redaction write path, per-user
  storage, and quotas.
- Auto-injection on plugin match. Server-side inference about what the
  planner needs; invariant-1 adjacent, and it creates a "who decided to
  inject this" audit question that planner-pull answers for free.
- The remaining July assistant families (notes, memory, web, profiling,
  expressions, scratch runs). Independent.

## 10. Open items for the maintainer

1. **`MAX_SKILLPACK_BODY_BYTES = 32 KiB` and the 8-load session budget** are
   proposed, not settled. Both are cheap to change and expensive to change
   later once packs are written to fit them.
2. **Manifest ordering.** Proposed: sorted by `pack_id` for byte-stability of
   the cached prefix. Any ordering derived from session state would break the
   cache and must be avoided.
3. **First pack to write.** Implementation should land the machinery plus
   exactly one real pack, so the shape is proven by a genuine consumer rather
   than a fixture. `batch_distribution_profile` is the obvious statistical
   candidate; the industrial case may be a better exemplar of "considerations
   that invalidate output".

## 11. Implementation order

1. `contracts/plugin_skillpack.py` — the dataclass, the validator, the
   bounded loader. No consumers. Fully testable alone.
2. The protocol classmethod and `None`-returning base implementations.
3. Manifest rendering into `build_catalog_context_string`, derived from
   `PolicyCatalogView`.
4. The `skillpacks.py` plane, the `PLANNER_DISCOVERY_TOOL_NAMES` insertion,
   the `effective_tool_hash` movement.
5. Audit: manifest field plus per-load rows.
6. One real pack.

Steps 1–2 are inert; the planner's behaviour changes first at step 3.
