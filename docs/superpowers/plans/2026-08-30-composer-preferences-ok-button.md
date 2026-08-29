# Composer Preferences OK Button Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit OK button that closes the Composer preferences dialog while moving the existing neutral Reset tutorial action to the right side of the footer.

**Architecture:** Keep immediate persistence and modal dismissal unchanged. Extend `ComposerPreferencesForm` with an optional close callback so the panel can render both footer actions in one DOM row; use the existing `Button` primitive and a token-based flex layout, with focused component and stylesheet contract tests.

**Tech Stack:** React 18, TypeScript, Zustand, Testing Library, Vitest, CSS design tokens, Vite.

---

## File Structure

- Modify `src/elspeth/web/frontend/src/components/settings/ComposerPreferencesPanel.tsx`: accept the panel close callback in the form, render the OK and Reset tutorial footer actions, and pass `onClose` from the modal wrapper.
- Modify `src/elspeth/web/frontend/src/components/settings/settings.css`: replace the reset button's standalone top margin with a flex footer that places OK left and Reset tutorial right.
- Modify `src/elspeth/web/frontend/src/components/settings/ComposerPreferencesPanel.test.tsx`: pin button order, variants, no-op close behavior, and continued reset behavior.
- Modify `src/elspeth/web/frontend/src/components/settings/settingsSurface.test.ts`: pin the footer's token-based left/right layout contract.

### Task 1: Add and verify the Composer preferences footer actions

**Files:**
- Modify: `src/elspeth/web/frontend/src/components/settings/ComposerPreferencesPanel.test.tsx:243-335`
- Modify: `src/elspeth/web/frontend/src/components/settings/settingsSurface.test.ts:197-235`
- Modify: `src/elspeth/web/frontend/src/components/settings/ComposerPreferencesPanel.tsx:19-27,190-207,276-280`
- Modify: `src/elspeth/web/frontend/src/components/settings/settings.css:408-410`

- [ ] **Step 1: Write failing component tests for the footer and OK behavior**

Add these tests inside `describe("ComposerPreferencesPanel — modal chrome", ...)`:

~~~tsx
it("places primary OK left and neutral Reset tutorial right in a footer", () => {
  render(<ComposerPreferencesPanel onClose={vi.fn()} />);
  const dialog = screen.getByRole("dialog");
  const actions = dialog.querySelector<HTMLElement>(
    ".composer-preferences-actions",
  );

  expect(actions).not.toBeNull();
  expect(actions!.getAttribute("style")).toBeNull();
  const buttons = within(actions!).getAllByRole("button");
  expect(buttons).toHaveLength(2);
  expect(buttons[0]).toHaveTextContent("OK");
  expect(buttons[0]).toHaveClass("btn-primary");
  expect(buttons[1]).toHaveTextContent("Reset tutorial");
  expect(buttons[1]).not.toHaveClass("btn-primary", "btn-danger");
});

it("OK closes without writing another preference", async () => {
  const onClose = vi.fn();
  render(<ComposerPreferencesPanel onClose={onClose} />);

  await userEvent.click(screen.getByRole("button", { name: "OK" }));

  expect(onClose).toHaveBeenCalledTimes(1);
  expect(updateUserComposerPreferences).not.toHaveBeenCalled();
});
~~~

- [ ] **Step 2: Write a failing stylesheet contract test**

Add this test to the Composer preferences describe block in `settingsSurface.test.ts`:

~~~tsx
it("places the two footer actions at opposite sides on the spacing scale", () => {
  expect(declaredValue(".composer-preferences-actions", "display")).toBe(
    "flex",
  );
  expect(
    declaredValue(".composer-preferences-actions", "justify-content"),
  ).toBe("space-between");
  const gap = px(declaredValue(".composer-preferences-actions", "gap"));
  const topGap = px(
    declaredValue(".composer-preferences-actions", "margin-top"),
  );
  expect(spacingScale.has(gap)).toBe(true);
  expect(spacingScale.has(topGap)).toBe(true);
  expect(declaredValue(".composer-preferences-reset", "margin-left")).toBe(
    "auto",
  );
});
~~~

- [ ] **Step 3: Run the focused tests and verify the new expectations fail**

Run:

~~~bash
npm --prefix src/elspeth/web/frontend test -- ComposerPreferencesPanel.test.tsx settingsSurface.test.ts
~~~

Expected: FAIL because `.composer-preferences-actions` and the `OK` button do not exist, and the footer CSS declarations are absent.

- [ ] **Step 4: Implement the minimal footer markup**

Extend the form props and destructuring:

~~~tsx
interface ComposerPreferencesFormProps {
  onClose?: () => void;
  onResetTutorialComplete?: () => void;
}

export function ComposerPreferencesForm({
  onClose,
  onResetTutorialComplete,
}: ComposerPreferencesFormProps = {}): JSX.Element | null {
~~~

Replace the standalone Reset tutorial button with:

~~~tsx
<div className="composer-preferences-actions">
  {onClose !== undefined && (
    <Button compact variant="primary" onClick={onClose}>
      OK
    </Button>
  )}
  <Button
    compact
    className="composer-preferences-reset"
    disabled={writing}
    onClick={() => void onResetTutorial()}
  >
    Reset tutorial
  </Button>
</div>
~~~

Pass the callback from the panel wrapper:

~~~tsx
<ComposerPreferencesForm
  onClose={onClose}
  onResetTutorialComplete={onResetTutorialComplete ?? onClose}
/>
~~~

The callback is intentionally optional so standalone form tests and any future embedded form use retain the existing reset-only surface.

- [ ] **Step 5: Implement the token-based footer layout**

Replace the old `.composer-preferences-reset` top-margin rule with:

~~~css
.composer-preferences-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-sm);
  margin-top: var(--space-lg);
}

.composer-preferences-reset {
  margin-left: auto;
}
~~~

This keeps Reset tutorial neutral because `Button` defaults to the secondary variant, while `margin-left: auto` keeps it right-aligned even when the form is rendered without an OK callback.

- [ ] **Step 6: Run the focused tests and verify they pass**

Run:

~~~bash
npm --prefix src/elspeth/web/frontend test -- ComposerPreferencesPanel.test.tsx settingsSurface.test.ts
~~~

Expected: both test files pass with no failing tests.

- [ ] **Step 7: Run frontend static and production-build verification**

Run each command:

~~~bash
npm --prefix src/elspeth/web/frontend run typecheck
npm --prefix src/elspeth/web/frontend run lint
npm --prefix src/elspeth/web/frontend run lint:css
npm --prefix src/elspeth/web/frontend run build
~~~

Expected: all commands exit 0. Vite may report the existing dynamic-import and large-chunk warnings; those are warnings only when the build exits 0.

- [ ] **Step 8: Review the exact diff and commit only owned files**

Run:

~~~bash
git diff --check -- src/elspeth/web/frontend/src/components/settings/ComposerPreferencesPanel.tsx src/elspeth/web/frontend/src/components/settings/ComposerPreferencesPanel.test.tsx src/elspeth/web/frontend/src/components/settings/settings.css src/elspeth/web/frontend/src/components/settings/settingsSurface.test.ts
git diff -- src/elspeth/web/frontend/src/components/settings/ComposerPreferencesPanel.tsx src/elspeth/web/frontend/src/components/settings/ComposerPreferencesPanel.test.tsx src/elspeth/web/frontend/src/components/settings/settings.css src/elspeth/web/frontend/src/components/settings/settingsSurface.test.ts
git add docs/superpowers/plans/2026-08-30-composer-preferences-ok-button.md src/elspeth/web/frontend/src/components/settings/ComposerPreferencesPanel.tsx src/elspeth/web/frontend/src/components/settings/ComposerPreferencesPanel.test.tsx src/elspeth/web/frontend/src/components/settings/settings.css src/elspeth/web/frontend/src/components/settings/settingsSurface.test.ts
git commit --only docs/superpowers/plans/2026-08-30-composer-preferences-ok-button.md src/elspeth/web/frontend/src/components/settings/ComposerPreferencesPanel.tsx src/elspeth/web/frontend/src/components/settings/ComposerPreferencesPanel.test.tsx src/elspeth/web/frontend/src/components/settings/settings.css src/elspeth/web/frontend/src/components/settings/settingsSurface.test.ts -m "feat(web): add Composer preferences OK action"
~~~

Expected: one commit containing only the plan and the four owned frontend files; unrelated staged tier-burndown documents remain staged and unmodified.
