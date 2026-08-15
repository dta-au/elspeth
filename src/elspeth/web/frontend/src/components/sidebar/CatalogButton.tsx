import { Button } from "@/components/ui";
import { OPEN_CATALOG_EVENT } from "@/lib/composer-events";

/**
 * Plugin-catalog trigger, mounted in the artifact-workspace toolbar beside
 * "Focus graph" (promoted out of the retired More-actions popover,
 * 2026-08-15 UX review). Plain compact species to match that row's 36px
 * register; the popover-era "Reference" meta chip and grid layout are gone —
 * beside a tablist and a view toggle, "read-only reference, not an action"
 * is no longer distinguishing information. The accessible name is the
 * visible label (the old aria-label="Catalog (reference)" failed WCAG 2.5.3
 * Label in Name against the visible "Plugin catalog").
 */
export function CatalogButton(): JSX.Element {
  return (
    <Button
      compact
      onClick={() => window.dispatchEvent(new CustomEvent(OPEN_CATALOG_EVENT))}
    >
      Plugin catalog
    </Button>
  );
}
