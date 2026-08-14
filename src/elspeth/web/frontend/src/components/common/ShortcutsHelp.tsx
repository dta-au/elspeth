import { useId, useRef } from "react";
import { useFocusTrap } from "@/hooks/useFocusTrap";

interface ShortcutsHelpProps {
  onClose: () => void;
}

interface ShortcutEntry {
  keys: string;
  action: string;
}

interface ShortcutGroup {
  name: string;
  items: ShortcutEntry[];
}

// Phase 8c-5 regroup: four semantic sections replace the two-section (Actions
// / Reference) layout from Phase 7B. Distribution rationale:
//
//   Actions    — things that change state (new session, run, validate).
//   Navigation — things that move focus or switch view (palette, chat, artifacts, catalog tabs).
//   Reference  — things that surface static information (catalog, this dialog).
//   Editing    — modal-management gestures (Escape).
//
// Ctrl+Shift+V (Validate) is kept because requestValidate has live consumers
// in CommandPalette and CompletionSummary (probe outcome: Phase 8c-5 Step 2).
//
// Alt+1-3 is drawer-scoped: the handler lives in CatalogDrawer and runs only
// while the catalog drawer is open. It is documented here for discoverability.
//
// SWITCH_TAB_EVENT (Alt+1-4 inspector tabs) was never added to App.tsx after
// Phase 3 removed the inspector tabs — no deletion needed (probe: no matches).
//
// COPY REGISTER (elspeth-3db2ae2f48, elspeth-93897c03d1): action labels are
// sentence case — first word capitalised, everything after it lower case
// unless it is an acronym (YAML) or a proper name of a product surface
// ("Sources / Transforms / Sinks" are catalog tab names). CommandPalette
// titles the same actions and must agree word-for-word for every chord both
// surfaces carry; `commandRegister.test.tsx` pins both halves.
const GROUPS: ShortcutGroup[] = [
  {
    name: "Actions",
    items: [
      { keys: "Ctrl+N", action: "New session" },
      { keys: "Ctrl+E", action: "Run pipeline" },
      { keys: "Ctrl+Shift+V", action: "Validate pipeline" },
    ],
  },
  {
    name: "Navigation",
    items: [
      { keys: "Ctrl+K", action: "Command palette" },
      { keys: "Ctrl+/", action: "Focus chat input" },
      { keys: "Ctrl/Cmd+Shift+G", action: "Show graph" },
      { keys: "Ctrl/Cmd+Shift+Y", action: "Show YAML" },
      { keys: "Alt+1-3", action: "Switch catalog tab (Sources / Transforms / Sinks)" },
    ],
  },
  {
    name: "Reference",
    items: [
      { keys: "Ctrl/Cmd+Shift+P", action: "Open plugin catalog" },
      { keys: "?", action: "Keyboard shortcuts" },
    ],
  },
  {
    name: "Editing",
    items: [{ keys: "Escape", action: "Close dialog or drawer" }],
  },
];

function ShortcutList({ items }: { items: ShortcutEntry[] }) {
  return (
    <dl className="shortcuts-list">
      {items.map(({ keys, action }) => (
        <div key={keys} className="shortcuts-list-item">
          <dt>
            <kbd className="command-palette-kbd">{keys}</kbd>
          </dt>
          <dd>{action}</dd>
        </div>
      ))}
    </dl>
  );
}

export function ShortcutsHelp({ onClose }: ShortcutsHelpProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const titleId = `${useId()}-shortcuts-title`;
  useFocusTrap(dialogRef);

  return (
    <>
      <div
        className="confirm-dialog-backdrop"
        onClick={onClose}
        role="presentation"
      />
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="confirm-dialog"
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            e.preventDefault();
            onClose();
          }
        }}
      >
        <header className="confirm-dialog-header">
          <h2 id={titleId} className="confirm-dialog-title">
            Keyboard shortcuts
          </h2>
        </header>
        <div className="confirm-dialog-body">
          {GROUPS.map((group) => (
            <section
              key={group.name}
              aria-label={group.name}
              className="shortcuts-group"
            >
              <h3 className="shortcuts-subheading">{group.name}</h3>
              <ShortcutList items={group.items} />
            </section>
          ))}
        </div>
        <footer className="confirm-dialog-actions">
          <button
            type="button"
            onClick={onClose}
            className="btn confirm-dialog-btn"
          >
            Close
          </button>
        </footer>
      </div>
    </>
  );
}
