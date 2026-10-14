# ELSPETH public website

Five static HTML pages; no generator, dependency install or compilation step.
The published routes remain `index.html`, `authoring.html`, `assurance.html`,
`use-cases.html` and `get-started.html`. Serve `website/` with a static server:

```bash
python3 -m http.server 8080 --bind 127.0.0.1 --directory website
node website/check-contrast.mjs
```

The Pages workflow uploads this directory directly on relevant website/workflow
pushes to the protected repository default branch. Manual dispatch from that
same branch is also supported. Pull requests and untrusted refs cannot deploy.
Deployment uses a trusted Nyx runner and the existing `github-pages` environment.
A local preview is not a deployment. Changes to the tutorial fixture under
`tutorial-site/` are separate from the public site.

## Content and visual sources

The site explains authoring, evidence review, use-case assessment and local
setup. Capability and readiness claims follow the repository README,
`docs/release/guarantees.md`, Composer guide, configuration reference and
operating runbooks. Illustrative scenarios are not deployed-service claims.
Release and database-upgrade paragraphs remain under release-coordinator
custody; reconcile them with the target release before publishing.

`tokens/colors.css` mirrors the Composer's semantic colour roles from
`src/elspeth/web/frontend/src/styles/tokens.css`: navy navigation, teal
workspace and warm paper surfaces. Website badge text retains its existing
4.5:1 text variants; the application's badge palette has a different non-text
contrast requirement. `site.css` supplies the reading layout. Fonts (Inter and
JetBrains Mono, SIL OFL-1.1) and Lucide are served locally; this site contains no
analytics or third-party asset requests. Outbound documentation links leave
the site when followed.

Navigation uses native `details` on narrow screens and works without JS.
`site.js` adds theme persistence, Escape/outside-click menu dismissal and
clipboard controls with a live status message. Theme and clipboard controls
are progressive enhancements.

## Verification

Run the existing website contract tests serially, using the intended source
roots in a worktree. Check internal assets and repository-document links,
including fragment identifiers. Inspect all five pages at desktop and narrow
widths in both themes; exercise keyboard navigation, theme persistence and
clipboard success/failure. Include a no-JavaScript check and a 320 CSS-pixel
reflow check. Automated accessibility checks supplement visual/keyboard review;
they do not establish complete accessibility conformance.

`check-contrast.mjs` checks the 44 opaque foreground/background combinations
used for body, secondary and muted text, links, and primary buttons in the two
themes. It includes known contrast and parser controls. Actual page composition
and interactive states still need browser checks.

## Composer screenshots

The images are captures of the actual local Composer at source commit
`40b0dfb66799e286c97166e08b4286fa26f3fc3f`, using an imported YAML pipeline and
three wholly synthetic community-facility records. The example routes requests
above 50,000 to an additional-review output; it does not determine funding
eligibility. It uses CSV sources/sinks and a configured gate, with no external
provider call and no pipeline execution. Model authoring was not demonstrated.

Capture used a loopback-only API and Vite instance, isolated SQLite state,
clean environment, disabled provider boot probes and no provider credentials.
The visible provider-unavailable notice is retained. No mocked graph, response
or DOM replacement is used. The original captures are 1440 × 960 CSS pixels;
WebP assets preserve that size. Links on images open the full-size assets.

The implementation handoff under `docs/reviews/public-site-085-20261011.md`
records the scenario, captured states, checks and remaining limits.
