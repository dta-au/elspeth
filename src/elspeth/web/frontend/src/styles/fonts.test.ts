import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const fontsCss = readFileSync("src/styles/fonts.css", "utf8");
const tokensCss = readFileSync("src/styles/tokens.css", "utf8");

function rootBlock(): string {
  const match = /:root\s*\{(?<body>[\s\S]*?)\n\}/.exec(tokensCss);
  if (match?.groups?.body === undefined) {
    throw new Error("tokens.css has no top-level :root block");
  }
  return match.groups.body;
}

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? sourceFiles(path) : [path];
  });
}

describe("ARGOnite fonts", () => {
  it.each([400, 500, 600, 700])("imports Instrument Sans %i", (weight) => {
    expect(fontsCss).toContain(`@import "@fontsource/instrument-sans/latin-${weight}.css";`);
  });

  it.each([600, 700, 800])("imports Bricolage Grotesque %i", (weight) => {
    expect(fontsCss).toContain(`@import "@fontsource/bricolage-grotesque/latin-${weight}.css";`);
  });

  it.each([
    "@fontsource/inter/latin-400.css",
    "@fontsource/inter/latin-500.css",
    "@fontsource/inter/latin-600.css",
    "@fontsource/inter/latin-700.css",
    "@fontsource/jetbrains-mono/latin-400.css",
    "@fontsource/jetbrains-mono/latin-600.css",
    "@fontsource/jetbrains-mono/latin-700.css",
  ])("keeps %s", (path) => {
    expect(fontsCss).toContain(`@import "${path}";`);
  });

  it("starts --font-sans with Instrument Sans and keeps Inter as fallback", () => {
    const match = /--font-sans:\s*(?<value>[^;]+);/.exec(rootBlock());
    expect(match?.groups?.value.trimStart().startsWith('"Instrument Sans"')).toBe(true);
    expect(match?.groups?.value).toContain('"Inter"');
  });

  it("defines --font-display starting with Bricolage Grotesque", () => {
    const match = /--font-display:\s*(?<value>[^;]+);/.exec(rootBlock());
    expect(match?.groups?.value.trimStart().startsWith('"Bricolage Grotesque"')).toBe(true);
    expect(match?.groups?.value).toContain('"Instrument Sans"');
  });

  it("does not reference Google Fonts anywhere under src/", () => {
    const offenders = sourceFiles("src")
      .filter((path) => !path.endsWith("fonts.test.ts"))
      .filter((path) => readFileSync(path, "utf8").includes("fonts.googleapis.com"));
    expect(offenders).toEqual([]);
  });
});
