# Composer Preferences OK Button

## Goal

Give users an explicit, conventional way to finish with the Composer preferences dialog while preserving its immediate-save behavior.

## Design

Add a footer action row below the existing preference controls.

- Place a primary **OK** button on the left, where **Reset tutorial** currently appears.
- Move **Reset tutorial** to the far right of the same row.
- Keep **Reset tutorial** visually neutral; resetting the tutorial is intentional state management, not an inherently dangerous action.
- Keep the existing top-right × button.

The **OK** button calls the panel's existing `onClose` callback. It does not perform another write: mode and detail preferences already persist when changed, theme changes take effect immediately, and tutorial reset retains its existing asynchronous action.

The × button, Escape key, backdrop click, and **OK** button therefore share the same close path and focus-restoration behavior.

## Implementation Boundaries

- Change only `ComposerPreferencesPanel` markup, its settings stylesheet, and focused component tests.
- Use the existing `Button` primitive and design tokens.
- Introduce a named footer class with a flex layout and `justify-content: space-between`; do not add inline styles.
- Do not change preference persistence, tutorial-reset behavior, API calls, modal structure, or focus-trap behavior.

## Verification

Component tests will prove that:

- both **OK** and **Reset tutorial** are present in the footer;
- **OK** uses the primary button treatment and invokes `onClose` once;
- **Reset tutorial** keeps its current neutral treatment and reset behavior;
- the footer structure provides the intended left/right placement without inline styling.

Run the focused Composer preferences component tests, frontend type checking, and the production frontend build.
