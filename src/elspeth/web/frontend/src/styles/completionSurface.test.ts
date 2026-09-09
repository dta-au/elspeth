import { existsSync, readFileSync } from "node:fs";

describe("guided completion surface", () => {
  it("aligns the completed stepper and content-height completion card", () => {
    const guidedCss = readFileSync(
      "src/components/chat/guided/guided.css",
      "utf8",
    );
    const stepperRule = guidedCss.match(
      /\.chat-panel--completed\s*>\s*\.guided-workflow\s*\{(?<body>[^}]*)\}/s,
    );
    const completionRule = guidedCss.match(
      /\.chat-panel--completed\s*>\s*\.guided-completion\s*\{(?<body>[^}]*)\}/s,
    );
    expect(stepperRule?.groups?.body).toContain(
      "padding-inline: var(--space-lg)",
    );
    expect(completionRule?.groups?.body).toContain(
      "margin-inline: var(--space-lg)",
    );
    expect(completionRule?.groups?.body).not.toMatch(/min-height|overflow/);
  });

  it("uses a small gap above the completion card", () => {
    const guidedCss = readFileSync(
      "src/components/chat/guided/guided.css",
      "utf8",
    );
    const completionRule = guidedCss.match(
      /\.chat-panel--completed\s*>\s*\.guided-completion\s*\{(?<body>[^}]*)\}/s,
    );

    expect(completionRule?.groups?.body).toContain(
      "margin-top: var(--space-xs)",
    );
  });

  it("keeps both completed surfaces aligned on narrow viewports", () => {
    const guidedCss = readFileSync(
      "src/components/chat/guided/guided.css",
      "utf8",
    );

    expect(guidedCss).toContain(`@media (max-width: 760px) {
  .chat-panel--completed > .guided-workflow {
    padding-inline: var(--space-sm);
  }

  .chat-panel--completed > .guided-completion {
    margin-inline: var(--space-sm);
  }
}`);
  });

  it("declares the ELSPETH SVG favicon as a same-origin public asset", () => {
    const indexHtml = readFileSync("index.html", "utf8");
    const faviconPath = "public/favicon.svg";

    expect(indexHtml).toContain(
      '<link rel="icon" href="/favicon.svg" type="image/svg+xml" />',
    );
    expect(existsSync(faviconPath)).toBe(true);
    if (!existsSync(faviconPath)) return;

    const favicon = readFileSync(faviconPath, "utf8");
    expect(favicon).toContain('viewBox="0 0 32 32"');
    expect(favicon).toContain('fill="#10272e"');
    expect(favicon).toContain('fill="#68d4df"');
  });
});
