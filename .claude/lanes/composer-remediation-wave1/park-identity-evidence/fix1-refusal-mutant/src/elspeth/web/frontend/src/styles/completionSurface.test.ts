import { existsSync, readFileSync } from "node:fs";

describe("public assets", () => {

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
