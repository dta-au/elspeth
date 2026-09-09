/**
 * Operator-declared protective-marking banner (PSPF markings up to PROTECTED,
 * plus the CABINET caveat). Declared server-side via
 * WebSettings.classification_banner and delivered on the /api/system/status
 * payload; deployments that declare nothing render nothing.
 *
 * The strip occupies the band .app-root already reserves for the fixed
 * overlay strips (header.css) — it adds no height of its own. The app-notice
 * and run-outcome strips share that band at a higher z, so an active notice
 * deliberately covers the marking until it is dismissed; the marking is
 * ambient identity, never urgent, so it always loses that contest.
 *
 * Marking text is the marking's standard PSPF form, emitted verbatim —
 * "OFFICIAL: Sensitive" keeps its mixed case and "PROTECTED//CABINET" its
 * double-slash caveat delimiter — so the CSS applies no text-transform.
 *
 * No role and no live-region semantics: the marking is static page identity,
 * read once in document order. An assertive/status role here would compete
 * with the notice regions the band already hosts (see the 2026-08-13
 * politeness convention in docs/agents/recent-code-hints.md).
 */
export type ClassificationBannerLevel =
  | "unofficial"
  | "official"
  | "official_sensitive"
  | "protected"
  | "protected_cabinet";

const MARKING: Record<
  ClassificationBannerLevel,
  { text: string; className: string }
> = {
  unofficial: {
    text: "UNOFFICIAL",
    className: "classification-banner--unofficial",
  },
  official: {
    text: "OFFICIAL",
    className: "classification-banner--official",
  },
  official_sensitive: {
    text: "OFFICIAL: Sensitive",
    className: "classification-banner--official-sensitive",
  },
  protected: {
    text: "PROTECTED",
    className: "classification-banner--protected",
  },
  protected_cabinet: {
    text: "PROTECTED//CABINET",
    className: "classification-banner--protected-cabinet",
  },
};

export function ClassificationBanner({
  level,
}: {
  level: ClassificationBannerLevel;
}): JSX.Element {
  const marking = MARKING[level];
  return (
    <div
      className={`classification-banner ${marking.className}`}
      data-testid="classification-banner"
    >
      {marking.text}
    </div>
  );
}
