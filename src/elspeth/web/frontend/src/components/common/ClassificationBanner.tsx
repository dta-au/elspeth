/**
 * Operator-declared protective-marking banner (PSPF "UNOFFICIAL" /
 * "OFFICIAL"). Declared server-side via WebSettings.classification_banner and
 * delivered on the /api/system/status payload; deployments that declare
 * nothing render nothing.
 *
 * The strip occupies the band .app-root already reserves for the fixed
 * overlay strips (header.css) — it adds no height of its own. The app-notice
 * and run-outcome strips share that band at a higher z, so an active notice
 * deliberately covers the marking until it is dismissed; the marking is
 * ambient identity, never urgent, so it always loses that contest.
 *
 * No role and no live-region semantics: the marking is static page identity,
 * read once in document order. An assertive/status role here would compete
 * with the notice regions the band already hosts (see the 2026-08-13
 * politeness convention in docs/agents/recent-code-hints.md).
 */
export type ClassificationBannerLevel = "unofficial" | "official";

const MARKING_TEXT: Record<ClassificationBannerLevel, string> = {
  unofficial: "UNOFFICIAL",
  official: "OFFICIAL",
};

export function ClassificationBanner({
  level,
}: {
  level: ClassificationBannerLevel;
}): JSX.Element {
  return (
    <div
      className={`classification-banner classification-banner--${level}`}
      data-testid="classification-banner"
    >
      {MARKING_TEXT[level]}
    </div>
  );
}
