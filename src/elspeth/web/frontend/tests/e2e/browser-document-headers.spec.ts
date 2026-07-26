import { expect, test } from "@playwright/test";

const BACKEND_BASE_URL = process.env.PLAYWRIGHT_BACKEND_BASE_URL;
const FRONTEND_BASE_URL = process.env.PLAYWRIGHT_FRONTEND_BASE_URL;

if (BACKEND_BASE_URL === undefined || FRONTEND_BASE_URL === undefined) {
  throw new Error("Playwright server URLs were not configured");
}

function cspDirectives(value: string): Map<string, string[]> {
  const directives = new Map<string, string[]>();
  for (const rawDirective of value.split(";")) {
    const [name, ...sources] = rawDirective.trim().split(/\s+/);
    if (name === "") continue;
    if (directives.has(name)) throw new Error(`duplicate CSP directive: ${name}`);
    directives.set(name, sources);
  }
  return directives;
}

test.describe("production SPA document framing policy", () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test("served callback and hash document carries exact frame denial headers", async ({ request }) => {
    const response = await request.get(
      `${BACKEND_BASE_URL}/?code=browser-secret&state=browser-state#/session-id`,
    );

    expect(response.status()).toBe(200);
    expect(response.headers()["content-type"]).toContain("text/html");
    expect(cspDirectives(response.headers()["content-security-policy"] ?? "").get("frame-ancestors")).toEqual([
      "'none'",
    ]);
    expect(response.headers()["x-frame-options"]).toBe("DENY");
  });

  test("hostile parent cannot render the served SPA in an iframe", async ({ page }) => {
    const targetUrl = `${BACKEND_BASE_URL}/?code=browser-secret&state=browser-state#/session-id`;
    let framedDocumentResponse: { status: number; csp: string | undefined } | undefined;

    page.on("response", (response) => {
      if (response.url().startsWith(`${BACKEND_BASE_URL}/?code=browser-secret`)) {
        framedDocumentResponse = {
          status: response.status(),
          csp: response.headers()["content-security-policy"],
        };
      }
    });

    // Navigate to the real Vite origin before replacing its document. This
    // gives Chromium a resolved loopback address space, avoiding an unrelated
    // Local Network Access rejection of a synthetic top-level response.
    await page.goto(FRONTEND_BASE_URL);
    await page.setContent(
      `<!doctype html><title>Hostile parent</title><iframe title="victim" src="${targetUrl}"></iframe>`,
    );
    await expect(page.getByTitle("victim")).toBeVisible();
    await expect.poll(() => framedDocumentResponse?.status).toBe(200);

    const victimFrame = page.frames().find((frame) => frame.parentFrame() === page.mainFrame());
    expect(victimFrame?.url()).toBe("chrome-error://chromewebdata/");
    expect(cspDirectives(framedDocumentResponse?.csp ?? "").get("frame-ancestors")).toEqual(["'none'"]);
  });
});
