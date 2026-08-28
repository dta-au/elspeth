# Composer Detail Level (Wave 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the per-user `show_advanced` switch, make the inert FieldTier real, and use both to calm the four Major content surfaces (guided schema form, node inspector, Spec tab, Validation inspector) — without hiding any audit-required element.

**Architecture:** One boolean preference (`show_advanced`, server-persisted via the existing 3-file preferences lockstep) is read through a single frontend selector `useShowAdvanced()`. Backend knob lowering defaults every field's `tier` to `"common"` and plugins opt specific knobs into `"advanced"`. Frontend surfaces partition content into *always* / *behind `<details>`* / *only with `show_advanced`* using the repo's existing disclosure idiom; a new shared `OptionRows` component renders plugin options identically in the node inspector and the Spec tab so the two cannot drift.

**Tech Stack:** FastAPI + SQLAlchemy + Pydantic v2 (backend), React 18 + Zustand + vitest/@testing-library (frontend, `src/elspeth/web/frontend`), pytest (backend).

**Spec:** Design review artifact https://claude.ai/code/artifact/8ec11b6b-1308-4188-87c6-2c4f78f1f4fb and Filigree epic `elspeth-cd8abcba3f` (children: `elspeth-9c11df65f8` switch, `elspeth-9cca900d41` FieldTier, `elspeth-a6ea581e8a` node inspector, `elspeth-b9ebdf9011` Spec tab, `elspeth-27efd1e801` ValidationResult, `elspeth-0bfd019f68` dead parameter). Waves 2–3 are roadmapped at the end and get their own plans.

## Global Constraints

- **Read `docs/agents/recent-code-hints.md` §"Whole-tree gates" before touching code.** No new `getattr`/`hasattr` anywhere (attribute-contracts + masquerade gates scan the whole tree, tests included). Owned types get direct attribute access; optional attributes become real fields with defaults.
- **Trust-tier lint corpus is fail-closed and must not grow.** Capture `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check --rules all --root src/elspeth` output before and after; diff must add nothing. Never hand-edit a `judge_metadata_signature`.
- **Knob-schema golden snapshots** (`tests/golden/web/catalog/knob_schema/*.json`, 55 files) pin the wire shape byte-for-byte. Task 4 regenerates them deliberately with the script it provides; no other task may change them.
- **Composer invariants:** no server-side authoring of pipeline structure; **no tutorial-special paths** (ADR-031). Every disclosure in this plan applies identically in tutorial mode.
- **Audit-required elements stay visible regardless of `show_advanced`:** AuthorityChip, Audit panel rows + Blocks-run/Advisory legend, Run-confirm egress lines, tool-outcome ribbon prefixes, acknowledgement cards, completion honesty gate, "Validation passed · N checks" headline.
- **Debug mode expands disclosures; it never adds surfaces.** Every item hidden when the flag is off has a plain summary in its place.
- **Pre-release: no DB migration path.** A new column ships by DB recreation (`web/sessions/schema.py`). Use `server_default` so existing rows read cleanly.
- **Copy register:** sentence case, no internal identifiers in visible text; raw identifiers go in `title`/`data-*` attributes or a `<code>` inside a `<details>`.
- **Shared checkout:** stage only your own pathspecs; never `git restore`/`clean` files you did not stage; no `git stash` (hook-blocked). Full `pytest tests/` is a background job — cap parallelism at `-n 12` when other agents are running.
- Frontend commands run from `src/elspeth/web/frontend`: `npx vitest run <path>`; backend from repo root with `source .venv/bin/activate`.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/elspeth/web/sessions/models.py` (modify, `user_preferences_table` ~L2382) | add `show_advanced` Boolean column |
| `src/elspeth/web/preferences/models.py` (modify) | `show_advanced: bool` on response + optional on request |
| `src/elspeth/web/preferences/service.py` (modify) | select/read-guard/upsert the new column |
| `tests/unit/web/preferences/test_models.py`, `test_service.py`, `tests/integration/web/test_preferences_routes.py` (modify) | pin round-trip + default |
| `src/elspeth/web/frontend/src/types/api.ts` (modify) | wire field on both payload types |
| `src/elspeth/web/frontend/src/stores/preferencesStore.ts` (modify) | `showAdvanced`, `setShowAdvanced`, `selectShowAdvanced`, `useShowAdvanced` |
| `src/elspeth/web/frontend/src/components/settings/ComposerPreferencesPanel.tsx` (modify) | "Detail level" radio group |
| `src/elspeth/web/catalog/knob_schema.py` (modify) | default `tier` to `"common"`; delete `composer_tier_default` |
| `src/elspeth/plugins/transforms/llm/base.py`, `src/elspeth/plugins/sources/csv_source.py` (modify) | first `composer_tier="advanced"` annotations |
| `tests/golden/web/catalog/knob_schema/*.json` (regenerate) | wire snapshots gain `"tier"` |
| `src/elspeth/web/frontend/src/components/chat/guided/SchemaFormTurn.tsx` (modify) | advanced-tier fields under `<details>` |
| `src/elspeth/web/frontend/src/components/inspector/OptionRows.tsx` (create) | shared plugin-options renderer (essential / advanced / raw JSON) |
| `src/elspeth/web/frontend/src/components/inspector/GraphView.tsx` (modify, `NodeConfigPanel` ~L738-800) | authored content first; connections collapsed |
| `src/elspeth/web/frontend/src/components/workspace/PipelineSpecView.tsx` (modify) | use `OptionRows`; humanise dl rows |
| `src/elspeth/web/frontend/src/components/execution/ValidationResult.tsx` (modify) | humanise failures; gate the pass-state stage list |

---

### Task 1: Backend `show_advanced` preference

**Files:**
- Modify: `src/elspeth/web/sessions/models.py:2382-2440` (`user_preferences_table`)
- Modify: `src/elspeth/web/preferences/models.py`
- Modify: `src/elspeth/web/preferences/service.py:150-170` (`_select_preferences_for_user`), `:199-266` (`get_composer_preferences`, `_row_to_prefs`), `:300-470` (`update_composer_preferences`)
- Test: `tests/unit/web/preferences/test_models.py`, `tests/unit/web/preferences/test_service.py`, `tests/integration/web/test_preferences_routes.py`

**Interfaces:**
- Produces: `ComposerPreferences.show_advanced: bool` (GET/PATCH response), `UpdateComposerPreferencesRequest.show_advanced: bool | None = None` (PATCH body, absent = unchanged). Wire key `show_advanced`.

- [ ] **Step 1: Write the failing model test**

Append to `tests/unit/web/preferences/test_models.py`:

```python
def test_composer_preferences_carries_show_advanced() -> None:
    prefs = ComposerPreferences(
        default_mode="guided",
        banner_dismissed_at=None,
        freeform_intro_dismissed_at=None,
        tutorial_completed_at=None,
        tutorial_stage=None,
        tutorial_session_id=None,
        tutorial_run_id=None,
        tutorial_source_data_hash=None,
        show_advanced=True,
        updated_at=None,
    )
    assert prefs.show_advanced is True


def test_update_request_accepts_only_show_advanced() -> None:
    req = UpdateComposerPreferencesRequest.model_validate({"show_advanced": True})
    assert req.show_advanced is True
    assert req.model_fields_set == {"show_advanced"}


def test_update_request_rejects_non_bool_show_advanced() -> None:
    # The request model is deliberately NOT strict (ISO datetimes must coerce,
    # models.py:12-19), so pydantic's lax bool accepts "yes"/"1"/"true". A list
    # is the value shape lax mode still rejects.
    with pytest.raises(ValidationError):
        UpdateComposerPreferencesRequest.model_validate({"show_advanced": [1, 2]})
```

(`pytest` and `ValidationError` are already imported at the top of that file; check and add `from pydantic import ValidationError` if not.)

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/unit/web/preferences/test_models.py -q`
Expected: 3 failures — `show_advanced` is an unexpected field (`extra="forbid"`).

- [ ] **Step 3: Add the column, model fields, and service plumbing**

`src/elspeth/web/sessions/models.py` — inside `user_preferences_table`, after the `tutorial_source_data_hash` column:

```python
    # Per-user detail level (elspeth-9c11df65f8). False = standard view;
    # True = engineer/auditor detail (raw options JSON, validation stage
    # list, advanced plugin knobs expanded). A plain boolean needs no CHECK
    # constraint; ``server_default`` keeps pre-existing rows readable.
    Column("show_advanced", Boolean, nullable=False, server_default=sa_false()),
```

Add to the imports at the top of that file: `from sqlalchemy import Boolean` and `from sqlalchemy.sql.expression import false as sa_false` (check the existing `from sqlalchemy import ...` line and extend it rather than adding a duplicate).

`src/elspeth/web/preferences/models.py`:

```python
class ComposerPreferences(BaseModel):
    ...
    tutorial_source_data_hash: str | None
    # Detail level (elspeth-9c11df65f8): True shows engineer/auditor detail
    # the standard view keeps behind summaries.
    show_advanced: bool
    updated_at: datetime | None


class UpdateComposerPreferencesRequest(BaseModel):
    ...
    tutorial_source_data_hash: str | None = None
    show_advanced: bool | None = None
    tutorial_completed_via: Literal["exit"] | None = None
```

`src/elspeth/web/preferences/service.py`:

1. `_select_preferences_for_user`: add `user_preferences_table.c.show_advanced,` before `updated_at`.
2. `get_composer_preferences` no-row default: add `show_advanced=False,` before `updated_at=None`.
3. `_row_to_prefs`: add `show_advanced=bool(row.show_advanced),` (SQLite returns 0/1) before `updated_at=row.updated_at`.
4. `update_composer_preferences`:
   - after `intro_in_payload = ...` add `advanced_in_payload = "show_advanced" in payload.model_fields_set`
   - extend `payload_is_empty` with `and not advanced_in_payload`
   - resolve the value the same way the other fields do (place next to `resolved_intro`):
     ```python
     if advanced_in_payload:
         resolved_advanced: bool = bool(payload.show_advanced)
     elif prior_prefs is not None:
         resolved_advanced = prior_prefs.show_advanced
     else:
         resolved_advanced = False
     ```
   - add `"show_advanced": resolved_advanced,` to the `values` dict
   - add `if advanced_in_payload: update_clause["show_advanced"] = resolved_advanced`
   - add `user_preferences_table.c.show_advanced,` to the `stmt.returning(...)` column list
   - where the method constructs the `current` `ComposerPreferences` from the returned row (search for `tutorial_source_data_hash=returned.tutorial_source_data_hash`), add `show_advanced=returned.show_advanced,`.

Search the file for every other `ComposerPreferences(` constructor call (there is one in the empty-PATCH-no-row branch, ~L357) and add `show_advanced=False,` so construction never fails on the new required field.

- [ ] **Step 4: Write the service + route tests**

Append to `tests/unit/web/preferences/test_service.py` (follow the file's distinct-user-id convention):

```python
def test_show_advanced_defaults_false_and_round_trips(service: PreferencesService) -> None:
    before = asyncio.run(service.get_composer_preferences("alice-show-advanced"))
    assert before.show_advanced is False

    transition = asyncio.run(
        service.update_composer_preferences(
            "alice-show-advanced",
            UpdateComposerPreferencesRequest(show_advanced=True),
        )
    )
    assert transition.current.show_advanced is True

    after = asyncio.run(service.get_composer_preferences("alice-show-advanced"))
    assert after.show_advanced is True
    # A later PATCH that does not mention the field leaves it alone.
    asyncio.run(
        service.update_composer_preferences(
            "alice-show-advanced",
            UpdateComposerPreferencesRequest(default_mode="freeform"),
        )
    )
    assert asyncio.run(service.get_composer_preferences("alice-show-advanced")).show_advanced is True
```

Append to `tests/integration/web/test_preferences_routes.py`:

```python
def test_patch_persists_show_advanced(client_as_alice: TestClient) -> None:
    assert client_as_alice.get("/api/composer-preferences").json()["show_advanced"] is False
    response = client_as_alice.patch("/api/composer-preferences", json={"show_advanced": True})
    assert response.status_code == 200
    assert response.json()["show_advanced"] is True
    assert client_as_alice.get("/api/composer-preferences").json()["show_advanced"] is True
```

- [ ] **Step 5: Run the three test files**

Run: `pytest tests/unit/web/preferences tests/integration/web/test_preferences_routes.py -q`
Expected: all pass, including the pre-existing tests (they construct `ComposerPreferences` via the service, never literally, so they are unaffected; if any literal constructor in a test fails, add `show_advanced=False` there too).

- [ ] **Step 6: Commit**

```bash
git add src/elspeth/web/sessions/models.py src/elspeth/web/preferences/models.py src/elspeth/web/preferences/service.py tests/unit/web/preferences/test_models.py tests/unit/web/preferences/test_service.py tests/integration/web/test_preferences_routes.py
git commit -m "feat(preferences): per-user show_advanced detail-level flag (elspeth-9c11df65f8)"
```

---

### Task 2: Frontend store + selector

**Files:**
- Modify: `src/elspeth/web/frontend/src/types/api.ts:91-125`
- Modify: `src/elspeth/web/frontend/src/stores/preferencesStore.ts` (`PreferencesState` ~L68, `INITIAL_STATE` ~L138, `bootstrap` ~L157, after `setDefaultMode` ~L270)
- Test: `src/elspeth/web/frontend/src/stores/preferencesStore.test.ts`

**Interfaces:**
- Consumes: wire key `show_advanced` from Task 1.
- Produces: `usePreferencesStore` state `showAdvanced: boolean`, action `setShowAdvanced(value: boolean): Promise<void>`, exported `selectShowAdvanced(state): boolean`, exported hook `useShowAdvanced(): boolean`. **Every later task reads the flag only through `useShowAdvanced()`.**

- [ ] **Step 1: Write the failing store test**

Append inside the top-level `describe("preferencesStore", ...)` in `preferencesStore.test.ts`:

```ts
  it("loads showAdvanced from the payload and defaults to false before bootstrap", async () => {
    expect(usePreferencesStore.getState().showAdvanced).toBe(false);
    mockFetch.mockResolvedValueOnce({
      default_mode: "guided",
      banner_dismissed_at: null,
      freeform_intro_dismissed_at: null,
      tutorial_completed_at: null,
      tutorial_stage: null,
      tutorial_session_id: null,
      tutorial_run_id: null,
      tutorial_source_data_hash: null,
      show_advanced: true,
      updated_at: "2026-05-15T00:00:00Z",
    });
    await usePreferencesStore.getState().bootstrap();
    expect(selectShowAdvanced(usePreferencesStore.getState())).toBe(true);
  });

  it("setShowAdvanced writes optimistically and reverts on failure", async () => {
    mockUpdate.mockRejectedValueOnce(new Error("offline"));
    await expect(usePreferencesStore.getState().setShowAdvanced(true)).rejects.toThrow("offline");
    const state = usePreferencesStore.getState();
    expect(state.showAdvanced).toBe(false);
    expect(state.writeError).toMatch(/Couldn't save your preference/);
    expect(mockUpdate).toHaveBeenCalledWith({ show_advanced: true });
  });
```

Update the import line to `import { selectShowAdvanced, selectTutorialCompleted, usePreferencesStore } from "./preferencesStore";`. Every existing `mockFetch.mockResolvedValueOnce({...})` / `mockUpdate.mockResolvedValueOnce({...})` payload literal in this test file and in `ComposerPreferencesPanel.test.tsx` must gain `show_advanced: false,` — the payload type becomes non-optional and TypeScript will flag each one.

- [ ] **Step 2: Run to verify it fails**

Run (from `src/elspeth/web/frontend`): `npx vitest run src/stores/preferencesStore.test.ts`
Expected: type error / `selectShowAdvanced is not a function`.

- [ ] **Step 3: Implement**

`types/api.ts` — add `show_advanced: boolean;` to `UserComposerPreferencesPayload` (after `tutorial_source_data_hash`) and `show_advanced?: boolean;` to `UpdateUserComposerPreferencesPayload`.

`stores/preferencesStore.ts`:

```ts
interface PreferencesState {
  ...
  tutorialSourceDataHash: string | null;
  // Detail level (elspeth-9c11df65f8). false = standard view. Read it ONLY
  // through useShowAdvanced()/selectShowAdvanced so consumers cannot drift.
  showAdvanced: boolean;
  loaded: boolean;
  ...
  setDefaultMode: (mode: ComposerMode, activeSessionId?: string | null) => Promise<void>;
  setShowAdvanced: (value: boolean) => Promise<void>;
  ...
}

const INITIAL_STATE = {
  ...
  tutorialSourceDataHash: null as string | null,
  showAdvanced: false,
  loaded: false,
  ...
};
```

In `bootstrap`'s success `set({...})` add `showAdvanced: payload.show_advanced,`. After `setDefaultMode` add:

```ts
  setShowAdvanced: async (value) => {
    if (get().writing) return;
    const previous = get().showAdvanced;
    set({ showAdvanced: value, writing: true, writeError: null });
    try {
      const payload = await updateUserComposerPreferences({ show_advanced: value });
      set({ showAdvanced: payload.show_advanced, writing: false });
    } catch (err) {
      set({
        showAdvanced: previous,
        writing: false,
        writeError:
          err instanceof Error
            ? `Couldn't save your preference: ${err.message}`
            : "Couldn't save your preference.",
      });
      throw err;
    }
  },
```

At the bottom of the module, next to `selectTutorialCompleted`:

```ts
export function selectShowAdvanced(state: PreferencesState): boolean {
  return state.showAdvanced;
}

/** The single consumer entry point for the detail-level flag. */
export function useShowAdvanced(): boolean {
  return usePreferencesStore(selectShowAdvanced);
}
```

- [ ] **Step 4: Run the store and panel tests**

Run: `npx vitest run src/stores/preferencesStore.test.ts src/components/settings/ComposerPreferencesPanel.test.tsx`
Expected: PASS. Then `npx tsc --noEmit -p .` (or the project's `npm run typecheck` if defined in `package.json`) — Expected: clean; any remaining payload literal missing `show_advanced` shows up here.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/web/frontend/src/types/api.ts src/elspeth/web/frontend/src/stores/preferencesStore.ts src/elspeth/web/frontend/src/stores/preferencesStore.test.ts src/elspeth/web/frontend/src/components/settings/ComposerPreferencesPanel.test.tsx
git commit -m "feat(frontend): showAdvanced preference state + useShowAdvanced selector (elspeth-9c11df65f8)"
```

---

### Task 3: "Detail level" control in Composer preferences

**Files:**
- Modify: `src/elspeth/web/frontend/src/components/settings/ComposerPreferencesPanel.tsx` (the `ComposerPreferencesForm` JSX, after the Theme fieldset)
- Test: `src/elspeth/web/frontend/src/components/settings/ComposerPreferencesPanel.test.tsx`

**Interfaces:**
- Consumes: `showAdvanced`, `setShowAdvanced` from Task 2.

- [ ] **Step 1: Write the failing test**

Append inside `describe("ComposerPreferencesForm", ...)`:

```ts
  it("offers a Detail level group and writes show_advanced", async () => {
    const user = userEvent.setup();
    vi.mocked(updateUserComposerPreferences).mockResolvedValueOnce({
      default_mode: "guided",
      banner_dismissed_at: null,
      freeform_intro_dismissed_at: null,
      tutorial_completed_at: null,
      tutorial_stage: null,
      tutorial_session_id: null,
      tutorial_run_id: null,
      tutorial_source_data_hash: null,
      show_advanced: true,
      updated_at: "2026-05-15T00:00:00Z",
    });
    render(<ComposerPreferencesForm />);
    const group = screen.getByRole("group", { name: "Detail level" });
    expect(within(group).getByRole("radio", { name: "Standard (recommended)" })).toBeChecked();
    await user.click(within(group).getByRole("radio", { name: "Show technical detail" }));
    expect(updateUserComposerPreferences).toHaveBeenCalledWith({ show_advanced: true });
    expect(usePreferencesStore.getState().showAdvanced).toBe(true);
    // Store → mounted control (the flag flips from elsewhere, e.g. another tab).
    act(() => usePreferencesStore.setState({ showAdvanced: false }));
    expect(within(group).getByRole("radio", { name: "Standard (recommended)" })).toBeChecked();
  });
```

Add `act, within` to the `@testing-library/react` import and `updateUserComposerPreferences` to the `@/api/client` import in that test file.

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run src/components/settings/ComposerPreferencesPanel.test.tsx`
Expected: FAIL — no group named "Detail level".

- [ ] **Step 3: Implement**

In `ComposerPreferencesForm`, read the state and action alongside the existing selectors:

```ts
  const showAdvanced = usePreferencesStore((s) => s.showAdvanced);
  const setShowAdvanced = usePreferencesStore((s) => s.setShowAdvanced);
```

and a handler next to `onThemeChange`:

```ts
  const onDetailLevelChange = useCallback(
    async (value: boolean) => {
      try {
        await setShowAdvanced(value);
      } catch (err) {
        // Surfaced via writeError -> role="alert" region below.
        console.error("[preferences] setShowAdvanced failed:", err);
      }
    },
    [setShowAdvanced],
  );
```

Insert after the Theme `<fieldset>`:

```tsx
      <fieldset
        disabled={writing}
        aria-busy={writing}
        className="composer-preferences-fieldset"
      >
        <legend className="composer-preferences-legend">Detail level</legend>
        <label className="composer-preferences-option">
          <Input
            type="radio"
            name="composer-detail-level"
            value="standard"
            checked={!showAdvanced}
            disabled={writing}
            onChange={() => void onDetailLevelChange(false)}
          />
          <span>Standard (recommended)</span>
        </label>
        <label className="composer-preferences-option">
          <Input
            type="radio"
            name="composer-detail-level"
            value="technical"
            checked={showAdvanced}
            disabled={writing}
            onChange={() => void onDetailLevelChange(true)}
          />
          <span>Show technical detail</span>
        </label>
        <p className="composer-preferences-hint">
          Technical detail shows raw plugin settings, every validation check, and advanced options. The audit trail is always shown.
        </p>
      </fieldset>
```

Add to `src/components/settings/settings.css` next to `.composer-preferences-fieldset`:

```css
.composer-preferences-hint {
  margin: 0.25rem 0 0;
  font-size: 0.86rem;
  color: var(--color-text-secondary);
}
```

- [ ] **Step 4: Run tests**

Run: `npx vitest run src/components/settings`
Expected: PASS (including the existing focus-trap test — the first radio in the dialog is still `composer-default-mode`).

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/web/frontend/src/components/settings/ComposerPreferencesPanel.tsx src/elspeth/web/frontend/src/components/settings/ComposerPreferencesPanel.test.tsx src/elspeth/web/frontend/src/components/settings/settings.css
git commit -m "feat(settings): Detail level radio group in Composer preferences (elspeth-9c11df65f8)"
```

---

### Task 4: Make FieldTier real on the backend

**Files:**
- Modify: `src/elspeth/web/catalog/knob_schema.py:290-310` (`_lower_nested_field` tail, `_lower_field` signature), `:356-364` (`_attach_tier`), and every `lower_model_to_knob_schema(...)`/`_lower_field(...)` call site (grep `composer_tier_default`)
- Modify: `src/elspeth/plugins/transforms/llm/base.py:114-115,127-135,152-159,182-187`; `src/elspeth/plugins/sources/csv_source.py:43-45`
- Regenerate: `tests/golden/web/catalog/knob_schema/*.json`
- Test: `tests/unit/web/catalog/test_knob_schema_properties.py` (new test), `tests/unit/web/catalog/test_knob_schema_golden.py` (existing)

**Interfaces:**
- Produces: every `KnobField` on the wire carries `tier: "essential" | "common" | "advanced"` (default `"common"`). Frontend `KnobField.tier` stays optional in the type (older payloads), but Task 5 treats absence as `"common"`.

- [ ] **Step 1: Write the failing property test**

Append to `tests/unit/web/catalog/test_knob_schema_properties.py`:

```python
def test_every_lowered_field_carries_a_tier_defaulting_to_common() -> None:
    from pydantic import BaseModel, Field

    from elspeth.web.catalog.knob_schema import lower_model_to_knob_schema

    class Model(BaseModel):
        plain: str = Field("x", description="plain")
        tuned: int = Field(1, description="tuned", json_schema_extra={"composer_tier": "advanced"})

    schema = lower_model_to_knob_schema(Model, plugin_kind="transform", plugin_name="example")
    by_name = {field["name"]: field for field in schema["fields"]}
    assert by_name["plain"]["tier"] == "common"
    assert by_name["tuned"]["tier"] == "advanced"
```

Check `lower_model_to_knob_schema`'s exact keyword names at its `def` (grep it) and match them.

Rewrite the pre-existing `test_tier_absent_when_unannotated` in `tests/unit/web/composer/test_knob_schema_from_model.py:99-105` — it pins the OLD contract and would fail after Step 3:

```python
def test_tier_defaults_to_common_when_unannotated():
    class Opts(BaseModel):
        debug: Annotated[bool, Field(title="Debug", description="Verbose output")] = False

    ks = lower_model_to_knob_schema(Opts, plugin_kind="source", plugin_name="test")
    f = ks["fields"][0]
    assert f["tier"] == "common"
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/unit/web/catalog/test_knob_schema_properties.py tests/unit/web/composer/test_knob_schema_from_model.py -q -k tier`
Expected: FAIL — `KeyError: 'tier'` for the unannotated fields.

- [ ] **Step 3: Default the tier and delete the dead parameter**

`_attach_tier` becomes:

```python
def _attach_tier(field: KnobField, info: FieldInfo) -> None:
    """Attach the composer tier; absent or malformed metadata lowers to "common".

    Plugins opt knobs OUT of the default view with
    ``json_schema_extra={"composer_tier": "advanced"}``; ``"essential"`` is
    reserved for the knobs a guided step asks about by name. Every wire field
    carries a tier so the form never has to guess (elspeth-9cca900d41).
    """
    tier: FieldTier = "common"
    extra = info.json_schema_extra
    if type(extra) is dict and "composer_tier" in extra:
        declared = extra["composer_tier"]
        if declared in ("essential", "common", "advanced"):
            tier = cast(FieldTier, declared)
    field["tier"] = tier
```

The discriminated-plugin path builds its discriminator field by hand (`knob_schema.py:506-515`, affects `transform__llm` and `source__llm`) — add `"tier": "common",` to that literal so the "every wire field carries a tier" claim is actually true (`_base_field` is not on that path).

Remove `composer_tier_default` everywhere: the `_lower_field` parameter and its `del`, the `_lower_nested_field` call (`_lower_field(name, info, plugin_kind="", plugin_name="")`), and the `lower_model_to_knob_schema` signature + any caller passing it (grep `composer_tier_default` across `src/` and `tests/`; there must be zero hits when done). Keep `plugin_kind`/`plugin_name` parameters as they are (they are part of the public signature even if unused inside `_lower_field`).

- [ ] **Step 4: Annotate the first advanced knobs**

`src/elspeth/plugins/transforms/llm/base.py` — add `json_schema_extra={"composer_tier": "advanced"}` to these `Field(...)` calls: `temperature`, `max_tokens`, `response_format`, `output_fields`, `image_inputs`, `max_image_bytes`, `max_images_per_call`, `lookup`, `pool_size`, `min_dispatch_delay_ms`, `max_dispatch_delay_ms`, `backoff_multiplier`, `recovery_step_ms`, `max_capacity_retry_seconds`. Leave `provider`, `model`, `prompt_template`, `system_prompt`, `response_field`, `queries` untiered (common). Do not touch fields already carrying `composer_hidden`.

`src/elspeth/plugins/sources/csv_source.py:43-45` — add `json_schema_extra={"composer_tier": "advanced"}` to `delimiter`, `encoding`, `skip_rows`.

Example of the exact edit shape — always break the call across lines: the ruff limit is 140 (`pyproject.toml:283`) and the one-line csv_source fields would reach ~150 chars, and ruff's reflow would otherwise shift line citations under the 8 pending tier-model allowlist entries for that file:

```python
    temperature: float = Field(
        0.0,
        ge=0.0,
        le=2.0,
        description="Sampling temperature",
        json_schema_extra={"composer_tier": "advanced"},
    )
```

**Ruling recorded here (W1 of the plan review):** knobs default to `"common"`; plugins annotate only `advanced`/`essential`. Ticket `elspeth-9cca900d41`'s original acceptance ("a lint pins that every knob has an explicit tier") is superseded — the test above pins the default instead. Wave 2 `elspeth-ca456d9d8d` builds on the default-to-common contract.

- [ ] **Step 5: Regenerate the golden snapshots deliberately**

Run from repo root:

```bash
python - <<'EOF'
import json
from pathlib import Path
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.catalog.service import CatalogServiceImpl
out = Path("tests/golden/web/catalog/knob_schema")
svc = CatalogServiceImpl(get_shared_plugin_manager())
for (kind, name), info in sorted(svc._schema_cache.items()):
    payload = {"plugin_kind": kind, "plugin_name": name, "knob_schema": info.knob_schema}
    (out / f"{kind}__{name}.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print("regenerated", len(svc._schema_cache))
EOF
git diff --stat tests/golden/web/catalog/knob_schema | tail -1
```

Expected: "regenerated 55"; the diff touches all 55 files and consists ONLY of added `"tier": "common"` / `"tier": "advanced"` lines. Review `git diff tests/golden/web/catalog/knob_schema/transform__llm.json` and confirm exactly the 14 fields from Step 4 read `"advanced"`.

- [ ] **Step 6: Run the catalog tests**

Run: `pytest tests/unit/web/catalog -q`
Expected: PASS (golden, properties, composer-help, eager lowering).

- [ ] **Step 7: Commit**

```bash
git add src/elspeth/web/catalog/knob_schema.py src/elspeth/plugins/transforms/llm/base.py src/elspeth/plugins/sources/csv_source.py tests/unit/web/catalog/test_knob_schema_properties.py tests/unit/web/composer/test_knob_schema_from_model.py tests/golden/web/catalog/knob_schema
git commit -m "feat(catalog): every knob carries a composer tier; first advanced annotations (elspeth-9cca900d41, elspeth-0bfd019f68)"
```

---

### Task 5: SchemaFormTurn groups advanced knobs

**Files:**
- Modify: `src/elspeth/web/frontend/src/components/chat/guided/SchemaFormTurn.tsx` (`visibleFields` ~L75, summary view ~L159-193, edit view ~L195-210)
- Modify: `src/elspeth/web/frontend/src/components/chat/guided/guided.css` (near `.guided-schema-summary-defaults` ~L1719)
- Test: `src/elspeth/web/frontend/src/components/chat/guided/SchemaFormTurn.test.tsx`

**Interfaces:**
- Consumes: `KnobField.tier` (Task 4), `useShowAdvanced()` (Task 2).

- [ ] **Step 1: Write the failing tests**

Append to `SchemaFormTurn.test.tsx` (add `import { usePreferencesStore } from "@/stores/preferencesStore";` and `import { resetStore } from "@/test/store-helpers";`):

```ts
describe("SchemaFormTurn advanced tier", () => {
  beforeEach(() => resetStore(usePreferencesStore));

  const payload = pluginPayload([
    field({ name: "prompt_template", label: "Prompt", kind: "text", required: true, tier: "common" }),
    field({ name: "temperature", label: "Temperature", kind: "number-float", tier: "advanced", default: 0 }),
  ]);

  it("keeps advanced knobs behind a closed Advanced settings disclosure by default", async () => {
    const user = userEvent.setup();
    render(<SchemaFormTurn payload={payload} onSubmit={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Edit" }));
    const details = screen.getByText("Advanced settings (1)").closest("details");
    expect(details).not.toBeNull();
    expect(details).not.toHaveAttribute("open");
    expect(screen.getByRole("textbox", { name: "Prompt" })).toBeInTheDocument();
    expect(screen.getByRole("spinbutton", { name: "Temperature" })).toBeInTheDocument();
  });

  it("opens the disclosure when show_advanced is on", async () => {
    usePreferencesStore.setState({ showAdvanced: true });
    const user = userEvent.setup();
    render(<SchemaFormTurn payload={payload} onSubmit={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByText("Advanced settings (1)").closest("details")).toHaveAttribute("open");
  });

  it("opens the disclosure when the flag flips on an already-mounted form", async () => {
    const user = userEvent.setup();
    render(<SchemaFormTurn payload={payload} onSubmit={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByText("Advanced settings (1)").closest("details")).not.toHaveAttribute("open");
    act(() => usePreferencesStore.setState({ showAdvanced: true }));
    expect(screen.getByText("Advanced settings (1)").closest("details")).toHaveAttribute("open");
  });

  it("treats a field with no tier as common", async () => {
    const user = userEvent.setup();
    render(<SchemaFormTurn payload={pluginPayload([field({ name: "path", label: "Path", kind: "text" })])} onSubmit={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.queryByText(/Advanced settings/)).not.toBeInTheDocument();
  });
});
```

Add `beforeEach` to the vitest import and `act` to the `@testing-library/react` import.

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run src/components/chat/guided/SchemaFormTurn.test.tsx -t "advanced tier"`
Expected: FAIL — "Advanced settings (1)" not found.

- [ ] **Step 3: Implement**

At the top of `SchemaFormTurn.tsx` add `import { useShowAdvanced } from "@/stores/preferencesStore";` and, at module scope:

```ts
function isAdvanced(field: KnobField): boolean {
  return field.tier === "advanced";
}
```

Inside the component, after `const hasPathKnob = ...`: `const showAdvanced = useShowAdvanced();`.

Replace the summary rows block and the edit-view `visibleFields().map(...)` with a partition. Summary view — replace the `summaryRows` computation with:

```tsx
          const rows = visibleFields().filter(
            (f) => isRequiredNow(f, values) || !isEmptyValue(f, values[f.name]),
          );
          const primaryRows = rows.filter((f) => !isAdvanced(f));
          const advancedRows = rows.filter(isAdvanced);
          if (rows.length === 0) {
            return (
              <p className="guided-schema-summary-defaults">
                No settings need review for this step.
              </p>
            );
          }
          const renderRow = (f: KnobField) => (
            <div className="guided-schema-summary-row" key={f.name}>
              <dt className="guided-schema-summary-label">{f.label}</dt>
              <dd className="guided-schema-summary-value">
                {summaryValueNode(f, values[f.name])}
                {showValidationFailureTeaching && f.name === "on_validation_failure" && (
                  <p className="guided-schema-summary-caveat" role="note">
                    {TUTORIAL_VALIDATION_FAILURE_CAVEAT}
                  </p>
                )}
              </dd>
            </div>
          );
          return (
            <>
              {primaryRows.length > 0 && (
                <dl className="guided-schema-summary">{primaryRows.map(renderRow)}</dl>
              )}
              {advancedRows.length > 0 && (
                <details className="guided-schema-advanced" open={showAdvanced}>
                  <summary>Advanced settings ({advancedRows.length})</summary>
                  <dl className="guided-schema-summary">{advancedRows.map(renderRow)}</dl>
                </details>
              )}
            </>
          );
```

Edit view — replace `{visibleFields().map((field) => (<KnobFieldRenderer .../>))}` with:

```tsx
          {visibleFields().filter((f) => !isAdvanced(f)).map((field) => (
            <KnobFieldRenderer
              key={field.name}
              field={field}
              required={isRequiredField(field, values)}
              value={values[field.name]}
              onChange={(value) => onChange(field.name, value)}
              idPrefix={reactId}
              disabled={disabled}
              isTutorial={isTutorial}
            />
          ))}
          {visibleFields().some(isAdvanced) && (
            <details className="guided-schema-advanced" open={showAdvanced}>
              <summary>Advanced settings ({visibleFields().filter(isAdvanced).length})</summary>
              {visibleFields().filter(isAdvanced).map((field) => (
                <KnobFieldRenderer
                  key={field.name}
                  field={field}
                  required={isRequiredField(field, values)}
                  value={values[field.name]}
                  onChange={(value) => onChange(field.name, value)}
                  idPrefix={reactId}
                  disabled={disabled}
                  isTutorial={isTutorial}
                />
              ))}
            </details>
          )}
```

The prop list (`idPrefix={reactId}`, `disabled`, `isTutorial`) is copied verbatim from the existing call at L196-206. Validation, `canSubmit`, `fieldsNeedingAttention` and `handleContinue` keep iterating `visibleFields()` unchanged, so an advanced required field still blocks Continue and is named in the needs-attention banner.

`guided.css` — after `.guided-schema-summary-defaults`:

```css
.guided-schema-advanced {
  margin-top: 0.75rem;
  border-top: 1px solid var(--color-border);
  padding-top: 0.5rem;
}
.guided-schema-advanced > summary {
  cursor: pointer;
  font-weight: 600;
  color: var(--color-text-secondary);
}
```

(Confirm `--color-border` exists in `styles/tokens.css`; if the token is named differently, use the one `.guided-schema-summary-row` already uses.)

- [ ] **Step 4: Run the guided tests and the tutorial canary**

Run: `npx vitest run src/components/chat/guided src/components/tutorial`
Expected: PASS. The tutorial suite proves ADR-031: no tutorial-specific branch was added.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/web/frontend/src/components/chat/guided/SchemaFormTurn.tsx src/elspeth/web/frontend/src/components/chat/guided/SchemaFormTurn.test.tsx src/elspeth/web/frontend/src/components/chat/guided/guided.css
git commit -m "feat(guided): advanced-tier knobs behind a disclosure, open with show_advanced (elspeth-9cca900d41)"
```

---

### Task 6: Shared `OptionRows` + node inspector reorder

**Files:**
- Create: `src/elspeth/web/frontend/src/components/inspector/OptionRows.tsx`
- Create: `src/elspeth/web/frontend/src/components/inspector/OptionRows.test.tsx`
- Modify: `src/elspeth/web/frontend/src/components/inspector/GraphView.tsx` (`NodeConfigPanel` ~L738-800; `ConfigValue`/`ConfigRows` stay and are exported for reuse)
- Modify: `src/elspeth/web/frontend/src/components/inspector/GraphView.test.tsx:330-365`

**Interfaces:**
- Consumes: `useShowAdvanced()` (Task 2); `ConfigValue`, `ConfigRows` from `GraphView.tsx` (export them).
- Produces: `export function OptionRows({ options, ariaLabel }: { options: Record<string, unknown>; ariaLabel: string }): JSX.Element` — renders essential rows, an "Advanced settings (N)" `<details>`, and (only with `show_advanced`) a "Raw options (JSON)" `<details>`. `export const INTERNAL_OPTION_KEYS`, `export const ESSENTIAL_OPTION_KEYS`.

Wave 1 orders by a static allowlist; tier-driven ordering from the catalog schema is Wave 2 (`elspeth-a6ea581e8a` step 1 note).

- [ ] **Step 1: Write the failing test**

`OptionRows.test.tsx`:

```tsx
import { act, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { usePreferencesStore } from "@/stores/preferencesStore";
import { resetStore } from "@/test/store-helpers";
import { OptionRows } from "./OptionRows";

const OPTIONS = {
  profile: "sonnet",
  prompt_template: "Rate {{ row['case_study1'] }}",
  temperature: 0.2,
  schema: { mode: "observed", guaranteed_fields: ["id"] },
  interpretation_requirements: [{ id: "x", accepted_artifact_hash: "3876" + "a".repeat(60) }],
  blob_ref: "f976fd8b-4432-4f8f-bbc3-2d8a9f2114e0",
};

describe("OptionRows", () => {
  beforeEach(() => resetStore(usePreferencesStore));

  it("shows essential rows first, advanced behind a closed disclosure, and no raw JSON by default", () => {
    render(<OptionRows options={OPTIONS} ariaLabel="assess options" />);
    const region = screen.getByRole("region", { name: "assess options" });
    const terms = within(region).getAllByRole("term").map((t) => t.textContent);
    expect(terms.slice(0, 3)).toEqual(["Prompt", "Model profile", "Row schema"]);
    expect(region.textContent).not.toMatch(/prompt_template|schema_mode/);
    const advanced = within(region).getByText("Advanced settings (1)").closest("details");
    expect(advanced).not.toHaveAttribute("open");
    expect(within(region).queryByText(/Raw options/)).not.toBeInTheDocument();
    expect(region.textContent).not.toMatch(/f976fd8b-4432/);
    expect(region.textContent).not.toMatch(/a{60}/);
  });

  it("with show_advanced on, opens the disclosure and offers the raw JSON", () => {
    usePreferencesStore.setState({ showAdvanced: true });
    render(<OptionRows options={OPTIONS} ariaLabel="assess options" />);
    const region = screen.getByRole("region", { name: "assess options" });
    expect(within(region).getByText("Advanced settings (1)").closest("details")).toHaveAttribute("open");
    expect(within(region).getByText("Raw options (JSON)")).toBeInTheDocument();
  });

  it("reacts when the preference flips on an already-mounted panel (the real user flow)", () => {
    render(<OptionRows options={OPTIONS} ariaLabel="assess options" />);
    expect(screen.getByText("Advanced settings (1)").closest("details")).not.toHaveAttribute("open");
    act(() => usePreferencesStore.setState({ showAdvanced: true }));
    expect(screen.getByText("Advanced settings (1)").closest("details")).toHaveAttribute("open");
    expect(screen.getByText("Raw options (JSON)")).toBeInTheDocument();
  });

  it("renders a plain sentence for empty options", () => {
    render(<OptionRows options={{}} ariaLabel="gate options" />);
    expect(screen.getByText("No settings for this step.")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run src/components/inspector/OptionRows.test.tsx`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `OptionRows.tsx`**

```tsx
// ============================================================================
// OptionRows — the ONE renderer for a component's plugin options, shared by
// the graph node inspector and the Spec tab so the two views cannot drift
// (elspeth-a6ea581e8a, elspeth-b9ebdf9011).
//
// Three tiers, decided by key (Wave 1; catalog-tier-driven ordering is Wave 2):
//   1. ESSENTIAL_OPTION_KEYS — what the reader authored; always visible, in
//      this order.
//   2. everything else — "Advanced settings (N)" <details>, open only when the
//      show_advanced preference is on.
//   3. INTERNAL_OPTION_KEYS — wire bookkeeping (review event ids, blob refs,
//      content hashes). Never rendered as rows; reachable only through the
//      "Raw options (JSON)" block, which itself renders only with show_advanced.
// ============================================================================

import { CodeBlock } from "@/components/chat/CodeBlock";
import { titleCaseLabel } from "@/components/catalog/pluginDisplayName";
import { useShowAdvanced } from "@/stores/preferencesStore";

import { ConfigRows } from "./ConfigRows";

// Visible labels for the authored keys (copy-register rule: no snake_case in
// visible text). Anything not listed falls back to titleCaseLabel(key), the
// frontend's single title-casing implementation (elspeth-d2de348437).
export const OPTION_LABELS: Readonly<Record<string, string>> = {
  prompt_template: "Prompt",
  system_prompt: "System prompt",
  profile: "Model profile",
  model: "Model",
  response_field: "Answer written to",
  path: "File",
  schema: "Row schema",
  mode: "Mode",
  fields: "Fields",
  field_mapping: "Field mapping",
  select_only: "Keep only",
  columns: "Columns",
  url: "URL",
  query: "Query",
};

export function optionLabel(key: string): string {
  return OPTION_LABELS[key] ?? titleCaseLabel(key);
}

export const ESSENTIAL_OPTION_KEYS: readonly string[] = Object.keys(OPTION_LABELS);

export const INTERNAL_OPTION_KEYS: ReadonlySet<string> = new Set([
  "interpretation_requirements",
  "blob_ref",
  "source_authoring",
  "resolved_prompt_template_hash",
  "prompt_template_source",
  "lookup_source",
  "system_prompt_source",
]);

// Re-key by visible label. (The raw key is recoverable from the Raw options
// block; ConfigRows has no per-row title plumbing and none is added here.)
function pick(options: Record<string, unknown>, keys: readonly string[]): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const key of keys) {
    if (Object.prototype.hasOwnProperty.call(options, key)) out[optionLabel(key)] = options[key];
  }
  return out;
}

export function OptionRows({
  options,
  ariaLabel,
}: {
  options: Record<string, unknown>;
  ariaLabel: string;
}): JSX.Element {
  const showAdvanced = useShowAdvanced();
  const essential = pick(options, ESSENTIAL_OPTION_KEYS);
  const advancedKeys = Object.keys(options).filter(
    (key) => !ESSENTIAL_OPTION_KEYS.includes(key) && !INTERNAL_OPTION_KEYS.has(key),
  );
  const advanced = pick(options, advancedKeys);
  const isEmpty = Object.keys(essential).length === 0 && advancedKeys.length === 0;

  return (
    <div className="option-rows" role="region" aria-label={ariaLabel}>
      {isEmpty ? (
        <p className="graph-config-empty-value">No settings for this step.</p>
      ) : (
        <>
          {Object.keys(essential).length > 0 && (
            <ConfigRows values={essential} emptyText="" />
          )}
          {advancedKeys.length > 0 && (
            <details className="option-rows-advanced" open={showAdvanced}>
              <summary>Advanced settings ({advancedKeys.length})</summary>
              <ConfigRows values={advanced} emptyText="" />
            </details>
          )}
        </>
      )}
      {showAdvanced && (
        <details className="option-rows-raw">
          <summary>Raw options (JSON)</summary>
          <CodeBlock
            code={JSON.stringify(options, null, 2)}
            language="json"
            prettyJson
            showCopy={false}
          />
        </details>
      )}
    </div>
  );
}
```

Create `src/components/inspector/ConfigRows.tsx` by **moving** `isRecord`, `ConfigValue`, and `ConfigRows` out of `GraphView.tsx` verbatim (exported), then `import { ConfigRows } from "./ConfigRows";` in `GraphView.tsx`. `OptionRows.tsx` imports from `./ConfigRows` (as in the code above) — never from `./GraphView`, which would be an import cycle. Keep the CSS class names unchanged. `url` stays an essential (always-visible) key: it is authored content; the credential-egress gate for URL fields lives in the Run confirm dialog, not here. `schema` is essential because the llm plugin requires it.

- [ ] **Step 4: Reorder `NodeConfigPanel`**

Replace the two `<section>`s at the end of `NodeConfigPanel` with:

```tsx
      <section className="graph-config-section">
        <h4>Settings</h4>
        <OptionRows options={config.options} ariaLabel={`${config.id} settings`} />
      </section>

      <details className="graph-config-section graph-config-connections">
        <summary>Connections &amp; schema</summary>
        <ConfigRows
          values={config.connections}
          emptyText="No explicit connections configured."
        />
      </details>
```

Add `import { OptionRows } from "./OptionRows";` and `import { ConfigRows } from "./ConfigRows";`.

- [ ] **Step 5: Update the GraphView config-panel test**

In `GraphView.test.tsx` ~L351-365 the existing assertions (`prompt`, `Find colours`, `output_schema`, `fields`, `url`) still hold: `prompt` is not in `ESSENTIAL_OPTION_KEYS` so it lands under "Advanced settings" — closed `<details>` content is still in the DOM for `getByText`. Add after `expect(within(panel).getByText("url")).toBeInTheDocument();`:

```ts
    // Authored settings come first; wiring is collapsed (elspeth-a6ea581e8a).
    const settings = within(panel).getByRole("heading", { name: "Settings" });
    const connections = within(panel).getByText("Connections & schema");
    expect(
      settings.compareDocumentPosition(connections) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).not.toBe(0);
    expect(connections.closest("details")).not.toHaveAttribute("open");
```

Search the file for `"Plugin options"` / `"Connections"` heading assertions and update them to the new labels.

- [ ] **Step 6: Run**

Run: `npx vitest run src/components/inspector`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/elspeth/web/frontend/src/components/inspector/OptionRows.tsx src/elspeth/web/frontend/src/components/inspector/OptionRows.test.tsx src/elspeth/web/frontend/src/components/inspector/ConfigRows.tsx src/elspeth/web/frontend/src/components/inspector/GraphView.tsx src/elspeth/web/frontend/src/components/inspector/GraphView.test.tsx
git commit -m "feat(inspector): shared OptionRows; node panel shows authored settings first, wiring collapsed (elspeth-a6ea581e8a)"
```

---

### Task 7: Spec tab uses `OptionRows`; humanise the routing rows

**Files:**
- Modify: `src/elspeth/web/frontend/src/components/workspace/PipelineSpecView.tsx`
- Test: `src/elspeth/web/frontend/src/components/workspace/PipelineSpecView.test.tsx`

**Interfaces:**
- Consumes: `OptionRows` (Task 6).

- [ ] **Step 1: Write the failing test**

Append to `PipelineSpecView.test.tsx`:

```tsx
  it("renders options through OptionRows and never shows hashes or blob refs by default", () => {
    useSessionStore.setState({
      compositionState: makeComposition(7, {
        sources: {
          source: {
            plugin: "csv",
            on_success: "raw_rows",
            on_validation_failure: "discard",
            options: {
              path: "blob:f976fd8b-4432-4f8f-bbc3-2d8a9f2114e0",
              blob_ref: "f976fd8b-4432-4f8f-bbc3-2d8a9f2114e0",
              interpretation_requirements: [{ accepted_artifact_hash: "3".repeat(64) }],
              schema: { mode: "observed" },
            },
          },
        },
      }),
    });
    render(<PipelineSpecView />);
    const card = screen.getByRole("article", { name: "Source source" });
    expect(within(card).getByRole("region", { name: "Source source settings" })).toBeInTheDocument();
    expect(card.textContent).not.toMatch(/f976fd8b-4432/);
    expect(card.textContent).not.toMatch(/3{64}/);
    expect(within(card).queryByText("None")).not.toBeInTheDocument();
    expect(within(card).getByText("Rows failing validation")).toBeInTheDocument();
    expect(within(card).getByText("dropped (recorded in the audit trail)")).toBeInTheDocument();
  });
```

Check `makeComposition`'s source shape in `src/test/composerFixtures.ts` and match the field names it expects for a source (`on_success`, `on_validation_failure`, `options`, `plugin`).

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run src/components/workspace/PipelineSpecView.test.tsx`
Expected: FAIL — no region named "Source source settings"; blob ref present.

- [ ] **Step 3: Implement**

In `PipelineSpecView.tsx` replace the `CodeBlock` import with `import { OptionRows } from "@/components/inspector/OptionRows";` and add a routing humaniser at module scope:

```ts
const ROUTING_LABELS: Record<string, string> = {
  input: "Reads from",
  on_success: "Then",
  on_error: "On error",
  on_validation_failure: "Rows failing validation",
  on_write_failure: "If writing fails",
  fork_to: "Forks every row to",
  routes: "Routes",
  branches: "Merges branches",
  policy: "Merge policy",
  merge: "Merge",
  scope_name: "Scope",
  scope_opener: "Scope opened by",
  scope_policy: "Scope policy",
  output_mode: "Output mode",
  timeout_seconds: "Waits up to (seconds)",
};

function routingLabel(field: string): string {
  return ROUTING_LABELS[field] ?? field;
}

function routingValue(field: string, value: unknown): string {
  if (value === "discard") return "dropped (recorded in the audit trail)";
  if (Array.isArray(value)) return value.map(String).join(", ");
  if (field === "routes" && typeof value === "object" && value !== null) {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.every(([, target]) => target === "fork")) return "every row continues to all branches";
    return entries.map(([when, target]) => `${when} → ${String(target)}`).join("; ");
  }
  return displayValue(value);
}
```

In `SpecSection`, change the `<dl>`:

```tsx
                <dl>
                  <div>
                    <dt>Kind</dt>
                    <dd>{row.kind}</dd>
                  </div>
                  {row.plugin !== null && (
                    <div>
                      <dt>Plugin</dt>
                      <dd>{row.plugin}</dd>
                    </div>
                  )}
                  {routingEntries.map(([field, value]) => (
                    <div key={field}>
                      <dt>{routingLabel(field)}</dt>
                      <dd>{routingValue(field, value)}</dd>
                    </div>
                  ))}
                </dl>
                <OptionRows options={row.options} ariaLabel={`${singular} ${row.id} settings`} />
```

Remove the `id` row (the `<h4>` already shows it) and the old `pipeline-spec-options` div. The `Copy`/download of YAML is unaffected (YAML tab). Check `PipelineSpecView.test.tsx`'s existing tests for assertions on the literal `dt` strings `id`/`kind`/`plugin`/`on_success` and update them to the new labels; the "shows only non-null authoritative routing fields" test keeps its `condition`-absent assertion unchanged.

- [ ] **Step 4: Run**

Run: `npx vitest run src/components/workspace`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/web/frontend/src/components/workspace/PipelineSpecView.tsx src/elspeth/web/frontend/src/components/workspace/PipelineSpecView.test.tsx
git commit -m "feat(spec): humanised routing rows and shared OptionRows; raw JSON only with show_advanced (elspeth-b9ebdf9011)"
```

---

### Task 8: ValidationResult goes through the humaniser; stage list gated

**Files:**
- Modify: `src/elspeth/web/frontend/src/components/execution/ValidationResult.tsx`
- Modify: `src/elspeth/web/frontend/src/components/sidebar/SideRailValidationBanner.tsx:85-95` (`SuggestionList` row text)
- Test: `src/elspeth/web/frontend/src/components/execution/ValidationResult.test.tsx`
- Test: `src/elspeth/web/frontend/src/components/sidebar/SideRailValidationBanner.test.tsx`

**Interfaces:**
- Consumes: `humaniseValidationMessage(message, phraseFor, stepLabelFor)`, `makePhraseFor(compositionState)` from `@/lib/validationHumaniser`; `stepLabelForNodeId(state, id)` from `@/components/chat/interpretationStepLabel`; `useShowAdvanced()`; `useSessionStore((s) => s.compositionState)`.

- [ ] **Step 1: Write the failing tests**

Append to `ValidationResult.test.tsx` (add `import { usePreferencesStore } from "@/stores/preferencesStore";`, `import { useSessionStore } from "@/stores/sessionStore";`, `import { resetStore } from "@/test/store-helpers";`, `act, within` to the `@testing-library/react` import, and `beforeEach` to the vitest import):

```tsx
describe("ValidationResultBanner detail level (elspeth-27efd1e801)", () => {
  beforeEach(() => {
    resetStore(usePreferencesStore);
    useSessionStore.setState({ compositionState: null });
  });

  it("keeps the per-stage check list out of the expanded pass view by default", async () => {
    const user = userEvent.setup();
    render(<ValidationResultBanner result={makePassResult()} />);
    await user.click(screen.getByRole("button", { name: "Validation passed. Show details." }));
    expect(screen.queryByText(/plugin_enablement/)).not.toBeInTheDocument();
    expect(screen.getByText("All 2 checks passed.")).toBeInTheDocument();
  });

  it("shows the check list, without the check-name prefix, once show_advanced flips on a mounted banner", async () => {
    const user = userEvent.setup();
    render(<ValidationResultBanner result={makePassResult()} />);
    await user.click(screen.getByRole("button", { name: "Validation passed. Show details." }));
    expect(screen.queryByText("Graph structure is valid")).not.toBeInTheDocument();
    act(() => usePreferencesStore.setState({ showAdvanced: true }));
    const item = screen.getByText("Graph structure is valid");
    expect(item).toBeInTheDocument();
    expect(item.closest("li")).toHaveAttribute("title", "graph_structure");
    expect(screen.queryByText(/^graph_structure:/)).not.toBeInTheDocument();
  });

  it("humanises a contract-violation dump into a headline and keeps the raw text behind Technical details", () => {
    render(
      <ValidationResultBanner
        result={makePassResult({
          is_valid: false,
          errors: [
            {
              component_id: "assess",
              component_type: "transform",
              message:
                "Schema contract violation: 'source' -> 'assess': required field 'case_study1' is not guaranteed by the producer",
              suggestion: null,
            },
          ],
        })}
      />,
    );
    // role="alert" (ValidationResult.tsx:236) wraps the whole error list, so
    // the raw text IS inside the alert — the contract is that it sits only
    // inside a closed <details>, never in the headline the AT announces first.
    const alert = screen.getByRole("alert");
    const raw = within(alert).getByText(/Schema contract violation/);
    const details = raw.closest("details");
    expect(details).not.toBeNull();
    expect(details).not.toHaveAttribute("open");
    expect(within(details as HTMLElement).getByText("Technical details")).toBeInTheDocument();
    const item = alert.querySelector("li.validation-banner-error-item") as HTMLElement;
    const headline = Array.from(item.childNodes)
      .filter((node) => (node as HTMLElement).tagName !== "DETAILS")
      .map((node) => node.textContent)
      .join("");
    expect(headline).not.toMatch(/Schema contract violation/);
    expect(headline).toMatch(/assess/i);
  });
});
```

Confirm the error object shape against `ValidationError` in `types/index.ts` (`component_id`, `component_type`, `message`, `suggestion`) and adjust.

Append to `SideRailValidationBanner.test.tsx` inside its top-level `describe` (the file already imports `makeComposition`, `useExecutionStore`, `useSessionStore`, `resetStore`; add `act` to the `@testing-library/react` import and `import { usePreferencesStore } from "@/stores/preferencesStore";`):

```tsx
  it("names the suggestion's component by its plain phrase, not the raw id, and reacts to a mounted flag flip", () => {
    resetStore(usePreferencesStore);
    useSessionStore.setState({ compositionState: makeComposition(1) } as never);
    useExecutionStore.setState({
      validationResult: {
        is_valid: true,
        summary: "Validation passed",
        checks: [
          { name: "graph_structure", passed: true, detail: "Graph structure is valid", affected_nodes: [], outcome_code: null },
        ],
        errors: [],
        warnings: [],
        readiness: { authoring_valid: true, execution_ready: true, completion_ready: true, blockers: [] },
        suggestions: [
          { component: "select_columns", message: "Schema contract violation: 'source' -> 'select_columns': required field 'id' is not guaranteed", severity: "info" },
        ],
      } as never,
    });

    render(<SideRailValidationBanner />);

    const list = screen.getByRole("list", { name: /suggestions/i });
    expect(list.textContent).not.toMatch(/select_columns:/);
    expect(list.textContent).not.toMatch(/Schema contract violation/);
    expect(screen.getByText(/Schema contract violation/).closest("details")).not.toBeNull();

    // The check list under the pass banner appears only once the flag flips ON A MOUNTED tree.
    expect(screen.queryByText("Graph structure is valid")).not.toBeInTheDocument();
    act(() => usePreferencesStore.setState({ showAdvanced: true }));
    expect(screen.getByText("Graph structure is valid")).toBeInTheDocument();
  });
```

Check how the file's existing tests put `suggestions` on the execution store (the `SUGGESTION` constant at the top of the file shows the `{ component, message, severity }` shape) and match the exact store key they use; give the suggestion `<ul>` an `aria-label="Suggestions"` if it does not already have an accessible name.

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run src/components/execution/ValidationResult.test.tsx -t "detail level"`
Expected: 3 failures.

- [ ] **Step 3: Implement**

Add imports:

```ts
import { useMemo } from "react";
import { stepLabelForNodeId } from "@/components/chat/interpretationStepLabel";
import { humaniseValidationMessage, makePhraseFor } from "@/lib/validationHumaniser";
import { useShowAdvanced } from "@/stores/preferencesStore";
import { useSessionStore } from "@/stores/sessionStore";
```

Inside `ValidationResultBanner`, before the `if (result.is_valid)` branch:

```ts
  const showAdvanced = useShowAdvanced();
  const compositionState = useSessionStore((s) => s.compositionState);
  const phraseFor = useMemo(() => makePhraseFor(compositionState), [compositionState]);
  const stepLabelFor = (componentId: string): string | null =>
    stepLabelForNodeId(compositionState, componentId);
```

Pass-state expanded view — replace the `result.checks.length > 0 && (<ul className="validation-banner-checks">…)` block with:

```tsx
        {result.checks.length > 0 && !showAdvanced && (
          <p className="validation-banner-checks-summary">
            All {result.checks.length} checks passed.
          </p>
        )}
        {result.checks.length > 0 && showAdvanced && (
          <ul className="validation-banner-checks">
            {result.checks.map((check, i) => (
              <li key={i} className="validation-banner-check-item" title={check.name}>
                <Icon name={check.passed ? "check" : "cross"} />{" "}
                {check.name === "advisor_signoff"
                  ? `${validationCheckDisplayName(check.name)}: ${check.detail}`
                  : check.detail}
              </li>
            ))}
          </ul>
        )}
```

Keep the existing forced-expansion rule for advisory checks: when `hasForcedGuidance` is true because of `advisoryChecks`/`failedAdvisorChecks`, render those specific checks (not the whole list) regardless of `showAdvanced`:

```tsx
        {!showAdvanced && (advisoryChecks.length > 0 || failedAdvisorChecks.length > 0) && (
          <ul className="validation-banner-checks">
            {[...advisoryChecks, ...failedAdvisorChecks].map((check, i) => (
              <li key={i} className="validation-banner-check-item" title={check.name}>
                <Icon name={check.passed ? "check" : "cross"} />{" "}
                {validationCheckDisplayName(check.name)}: {check.detail}
              </li>
            ))}
          </ul>
        )}
```

Failure branch — humanise each error. Replace the `errorText` construction:

```tsx
          const finding = humaniseValidationMessage(err.message, phraseFor, stepLabelFor);
          const errorText = (
            <>
              <strong>{resolveComponentName(err.component_id, nodes, componentNames)}:</strong>{" "}
              {finding.headline}
            </>
          );
```

and after the `{err.suggestion && ...}` block inside the `<li>` add:

```tsx
              {finding.raw !== null && (
                <details className="validation-banner-technical">
                  <summary>Technical details</summary>
                  <pre>{finding.raw}</pre>
                </details>
              )}
```

Apply the same humanise + details treatment to the warnings list (`warn.message`). Drop the `[{component_type}]` bracket prefix from both (the resolved name already carries the type when it falls back). Give `resolveComponentName` a fourth parameter `phraseFor: (componentId: string | null) => string` and change its two final fallbacks (`return componentId;` and `` node ? `${node.node_type}:${node.id}` : componentId ``) to `return phraseFor(componentId);` so a raw id never renders; pass `phraseFor` at both call sites (warnings and errors).

- [ ] **Step 4: Run the execution tests**

Run: `npx vitest run src/components/execution src/components/sidebar`
Expected: two pre-existing tests fail first — `"expands to check details on click and collapses again via Collapse"` (ValidationResult.test.tsx:54-73) and `"auto-expands when the pass carries warnings and offers no Collapse"` (:75-96) both assert `/plugin_enablement passed/` with the flag off. Edit each: add `usePreferencesStore.setState({ showAdvanced: true });` as the first line of the test body (the fixture's `detail` text is `"plugin_enablement passed."`, so the regex still matches the now-prefix-free row). Re-run; expected PASS. Also update `SideRailValidationBanner.tsx:91` in this task: `<strong>{s.component}:</strong> {s.message}` renders the raw component id and raw message — pass them through the same `phraseFor` / `humaniseValidationMessage(...).headline` pair (the component already has access to `compositionState` via the store).

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/web/frontend/src/components/execution/ValidationResult.tsx src/elspeth/web/frontend/src/components/execution/ValidationResult.test.tsx src/elspeth/web/frontend/src/components/sidebar/SideRailValidationBanner.tsx src/elspeth/web/frontend/src/components/sidebar/SideRailValidationBanner.test.tsx
git commit -m "fix(validation): execution banner and side-rail suggestions use the shared humaniser; check list only with show_advanced (elspeth-27efd1e801)"
```

---

### Task 9: Whole-tree verification and closeout

**Files:** none new.

- [ ] **Step 1: Frontend full run**

Run (from `src/elspeth/web/frontend`): `npx vitest run` then `npm run lint` (if defined) and `npx tsc --noEmit -p .`
Expected: all green.

- [ ] **Step 2: Lint corpus diff**

```bash
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check --rules all --root src/elspeth > /tmp/claude-1000/-home-john-elspeth/lints-after.txt; wc -l /tmp/claude-1000/-home-john-elspeth/lints-after.txt
```

Compare the finding count against the pre-change capture taken before Task 1 (capture it then with the same command to `lints-before.txt`). Expected: identical counts; `diff` shows no added findings. `knob_schema.py`'s `_attach_tier` uses `type(extra) is dict` + membership + index exactly as before (no `getattr`), so the attribute-contracts and masquerade gates are untouched.

- [ ] **Step 3: Backend full suite as a background job**

Run: `pytest tests/ -n 12 -q 2>&1 | tail -30` in the background (worktree preferred for long runs per AGENTS.md). Expected: green, including `tests/unit/web/test_sessions_composer_attribute_contracts.py`, `tests/unit/elspeth_lints/test_masquerade_gate.py`, and `tests/unit/web/catalog/test_knob_schema_golden.py`. Read the summary line, not `tail` alone (three void-run variants exist — check the collected count is non-zero).

- [ ] **Step 4: Live check on the local deployment**

Executor: the lane that ran Task 8 (it has the branch built). Procedure: build the frontend (`npm run build` in `src/elspeth/web/frontend`), then `sudo systemctl restart elspeth-web` and confirm `/api/system/status` reports the new `frontend_build` value before trusting anything (`is-active` alone lies after a restart — poll the build id). Sign in as the staging operator account (credentials live only in the runnable staging surfaces, never in docs); open session `39578c6f`. Confirm: Spec tab shows no hash/UUID with the default preference; toggle "Show technical detail" in Composer preferences; Spec tab now offers "Raw options (JSON)"; Validation inspector expanded shows the check list only with the flag on; node inspector shows Settings above a collapsed "Connections & schema". Report: one `filigree add-comment elspeth-cd8abcba3f` listing each check as pass/fail with the frontend build id; a failed check reopens the task's ticket rather than being noted in prose.

- [ ] **Step 5: Close the tickets**

Ticket workflow facts: `elspeth-9c11df65f8` is a *feature* — start it with `filigree start-work elspeth-9c11df65f8 --assignee <lane> --advance` (walks proposed → approved → building); `elspeth-9cca900d41` and `elspeth-27efd1e801` are *bugs* — `--advance` walks triage → confirmed → fixing, and closing requires fixing → verifying first (`filigree update <id> --status verifying` then `filigree close`). For each of `elspeth-9c11df65f8`, `elspeth-9cca900d41`, `elspeth-b9ebdf9011`, `elspeth-27efd1e801`: `filigree add-comment <id> "<what landed, commit sha, what verified>"` then close. **Do not close `elspeth-a6ea581e8a`** — comment that the reorder + collapse landed and leave it open for its Wave 2 follow-up (catalog-tier-driven ordering, roadmap row 8). `elspeth-0bfd019f68`: comment that the `composer_tier_default` half landed here; the chip deletion remains (Wave 3).

---

## Roadmap: Waves 2 and 3 (separate plans)

Each wave gets its own plan file once Wave 1 is merged; the tickets already carry file:line detail.

**Wave 2 — the flag reaches the remaining surfaces** (all blocked on `elspeth-9c11df65f8`, now landed):

| Order | Ticket | Scope | Depends on |
|---|---|---|---|
| 1 | `elspeth-af559a0bab` | Tool-call cards: sentence primary, identifier secondary; map the 15 unmapped web-registry tools (verified by importing `_dispatch._REGISTERED_TOOLS`, 40 tools); registry-parity test | — |
| 2 | `elspeth-34e810312c` | Run history & diagnostics behind the flag; keep corruption badge + Explain | Wave 1 |
| 3 | `elspeth-aa39cffb16` | Import YAML behind the flag; YAML Download stays | Wave 1 |
| 4 | `elspeth-05a240b82a` | Accounting grid glossary + collapse; recent-errors count | Wave 1, `elspeth-27efd1e801` (phrase-map reuse) |
| 5 | `elspeth-ca456d9d8d` | Wire-stage detail under "Technical details"; `node_options_summary` through tiers; humanise `plugin.id` subtitles | Wave 1 Task 4 |
| 6 | `elspeth-8555a6a9e0` | Catalog: Capability chips, characteristic strip tiering, Schema view | Wave 1 |
| 7 | `elspeth-c8a402a9a4` | Version history grouping that keeps every version revertable (expand-in-place); humanised applied labels; edit-source labels need a backend wire field — out of scope | #1 |
| 8 | `elspeth-a6ea581e8a` (follow-up) | Replace `ESSENTIAL_OPTION_KEYS` with catalog-tier-driven ordering once `pluginCatalogStore` exposes knob schemas to the inspector | Wave 1 Task 6 |

**Wave 3 — register, bugs, cleanup** (independent of the flag; can run in parallel with Wave 2 by a second lane):

| Ticket | Scope |
|---|---|
| `elspeth-93f5621f18` | `humaniseStepLabel` raw-id fallback → "a removed step" (design reversal; the :120-124 doctrine and two pinning tests change with it) |
| `elspeth-d74ab492dd` | Register batch (ModelChip, scope badge, byte count, failure enums, ExplainDialog, egress plugin names, provenance enum, "reviewed" word) |
| `elspeth-4bf65fe149` | Planner brief: reader's terms; corpus case that fails on `is_valid:`/`options.` tokens |
| `elspeth-d1feee1e67` | e2e keyboard path through the graph a11y list (the 1px clip + `:focus-within` reveal is deliberate; 57c6fba409 closed not_a_bug) |
| `elspeth-f1394307e3` | Minor gated surfaces: recovery transcript, blob structural disclosure, audit Refresh (Show archived dropped — it is the only archive-restore path) |
| `elspeth-0bfd019f68` (remainder) | Delete the unknown-audit-characteristic chip (the vocabulary-parity test already exists: test_audit_characteristic_vocabulary_parity.py:38) |

**Sequencing rule for both waves:** one PR per ticket; each PR's default-DOM regression pin (no 32+-hex hash, no UUID, no `[a-z]+_[a-z_]+:` stage prefix outside `<details>`/`<code>` with the flag off) is the acceptance test the reviewer runs first.
