// motion.ts — the single authority for JS-driven scroll animation choice.
//
// CSS transitions in this frontend already honour the user's OS-level
// reduced-motion setting behind `@media (prefers-reduced-motion: reduce)`
// blocks (workspace.css, sidebar.css, header.css, blobs.css, tutorial.css).
// The imperative scroll API gets NO such downgrade from the browser:
// `scrollTo({ behavior: "smooth" })` animates regardless of the preference,
// so every JS scroll site must consult it explicitly — through this helper,
// never with its own matchMedia read (elspeth-5b42a9ae1e).

/**
 * The `behavior` to pass to `scrollTo` / `scrollIntoView`: `"smooth"`
 * normally, `"auto"` (jump) when the user asks for reduced motion.
 *
 * Reads the media query at call time, so an OS preference toggled
 * mid-session takes effect on the very next scroll — no listener needed.
 * Falls back to `"smooth"` where matchMedia is unavailable (same guard
 * shape as useTheme.ts).
 */
export function preferredScrollBehavior(): ScrollBehavior {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return "smooth";
  }
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches
    ? "auto"
    : "smooth";
}
