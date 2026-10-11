import fs, {
  chmodSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync,
  readdirSync, rmSync, statSync, symlinkSync, writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { spawnSync } from "node:child_process";

import type { FullConfig } from "@playwright/test";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import stagingGlobalSetup from "../../tests/e2e/setup/staging-global-setup";
import { configureStagingOrigin, loginToStaging, parseStagingOrigin } from "../../scripts/staging-origin.mjs";

const api = vi.hoisted(() => ({
  newContext: vi.fn(), post: vi.fn(), dispose: vi.fn(),
  ok: vi.fn(), status: vi.fn(), json: vi.fn(), text: vi.fn(),
}));
vi.mock("@playwright/test", () => ({
  request: { newContext: api.newContext },
  defineConfig: (config: unknown) => config,
  devices: { "Desktop Chrome": {} },
}));

let temporaryDirectory: string;
let storagePath: string;
let config: FullConfig;

beforeEach(() => {
  temporaryDirectory = mkdtempSync(join(process.env.STAGING_TEST_TMPDIR ?? tmpdir(), "staging-auth-"));
  storagePath = join(temporaryDirectory, ".auth", "staging-user.json");
  config = { projects: [{ use: { storageState: storagePath } }] } as FullConfig;
  vi.stubEnv("STAGING_BASE_URL", "https://staging.example.test/");
  vi.stubEnv("STAGING_USERNAME", "test-account");
  vi.stubEnv("STAGING_PASSWORD", "test-password");
  vi.stubEnv("PLAYWRIGHT_BACKEND_BASE_URL", undefined);
  api.newContext.mockResolvedValue({ post: api.post, dispose: api.dispose });
  api.post.mockResolvedValue({ ok: api.ok, status: api.status, json: api.json, text: api.text });
  api.ok.mockReturnValue(true);
  api.status.mockReturnValue(200);
  api.json.mockResolvedValue({ access_token: "test-bearer" });
  api.dispose.mockResolvedValue(undefined);
});

afterEach(() => {
  rmSync(temporaryDirectory, { recursive: true, force: true });
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

describe("staging credential origin", () => {
  it.each([
    undefined, "", "not-a-url", "http://staging.example.test", "//staging.example.test",
    "https://user:password@staging.example.test", "https://@staging.example.test",
    "https://staging.example.test/path", "https://staging.example.test/a/..",
    "https://staging.example.test?secret", "https://staging.example.test?",
    "https://staging.example.test#secret", "https://staging.example.test#",
    " https://staging.example.test", "https://staging.example.test\n",
    "https:\\staging.example.test", "https://staging.example.test:bad",
  ])("rejects %s before creating a credential request", async (origin) => {
    vi.stubEnv("STAGING_BASE_URL", origin);
    await expect(stagingGlobalSetup(config)).rejects.toThrow("HTTPS origin");
    expect(api.newContext).not.toHaveBeenCalled();
    expect(existsSync(storagePath)).toBe(false);
  });

  it.each([
    ["https://staging.example.test", "https://staging.example.test"],
    ["HTTPS://STAGING.EXAMPLE.TEST:443/", "https://staging.example.test"],
    ["https://deployment.example.test:8443/", "https://deployment.example.test:8443"],
    ["https://[::1]:8443", "https://[::1]:8443"],
  ])("preserves deployment origin %s", (input, expected) => {
    expect(parseStagingOrigin(input)).toBe(expected);
  });

  it("rejects a conflicting backend before login", async () => {
    vi.stubEnv("PLAYWRIGHT_BACKEND_BASE_URL", "https://unintended.example.test");
    await expect(stagingGlobalSetup(config)).rejects.toThrow("must match");
    expect(api.newContext).not.toHaveBeenCalled();
  });

  it("aligns the browser config and API helper target with the canonical origin", async () => {
    vi.stubEnv("STAGING_BASE_URL", "HTTPS://STAGING.EXAMPLE.TEST:443/");
    vi.stubEnv("PLAYWRIGHT_BACKEND_BASE_URL", "https://staging.example.test/");
    const { default: playwrightConfig } = await import("../../playwright.staging.config");
    expect(playwrightConfig.use?.baseURL).toBe("https://staging.example.test");
    expect(process.env.PLAYWRIGHT_BACKEND_BASE_URL).toBe("https://staging.example.test");
    expect(configureStagingOrigin()).toBe("https://staging.example.test");
  });
});

describe("staging login transport", () => {
  it("keeps TLS verification and disables credential-bearing redirects", async () => {
    await stagingGlobalSetup(config);
    expect(api.newContext).toHaveBeenCalledWith({ baseURL: "https://staging.example.test", ignoreHTTPSErrors: false });
    expect(api.post).toHaveBeenCalledExactlyOnceWith("/api/auth/login", {
      data: { username: "test-account", password: "test-password" }, maxRedirects: 0,
    });
    expect(api.dispose).toHaveBeenCalledOnce();
    const state = JSON.parse(readFileSync(storagePath, "utf8"));
    expect(state.origins).toEqual([{
      origin: "https://staging.example.test",
      localStorage: [{ name: "auth_token", value: "test-bearer" }],
    }]);
  });

  it.each([307, 308, 401, 500])("refuses status %s without reading an echoed credential body", async (status) => {
    api.ok.mockReturnValue(false);
    api.status.mockReturnValue(status);
    api.text.mockResolvedValue("test-account test-password test-bearer");
    await expect(stagingGlobalSetup(config)).rejects.toThrow(`staging login failed (${status})`);
    expect(api.post).toHaveBeenCalledOnce();
    expect(api.text).not.toHaveBeenCalled();
    expect(api.json).not.toHaveBeenCalled();
    expect(existsSync(storagePath)).toBe(false);
    expect(api.dispose).toHaveBeenCalledOnce();
  });

  it("removes request details from transport errors", async () => {
    api.post.mockRejectedValue(new Error("echo test-account test-password"));
    await expect(stagingGlobalSetup(config)).rejects.toThrow(/^staging login request failed$/);
    expect(existsSync(storagePath)).toBe(false);
  });

  it("removes response text from JSON parse failures", async () => {
    api.json.mockRejectedValue(new Error("echo test-password test-bearer"));
    await expect(stagingGlobalSetup(config)).rejects.toThrow(/^staging login returned invalid JSON$/);
  });

  it.each([null, {}, { access_token: "" }, { access_token: 42 }])("refuses malformed token response %s", async (body) => {
    api.json.mockResolvedValue(body);
    await expect(stagingGlobalSetup(config)).rejects.toThrow("no access token");
    expect(existsSync(storagePath)).toBe(false);
  });

  it("emits no account or token log", async () => {
    const log = vi.spyOn(console, "log");
    try {
      await stagingGlobalSetup(config);
      expect(log).not.toHaveBeenCalled();
    } finally {
      log.mockRestore();
    }
  });
});

describe("standalone staging harness login", () => {
  it("loads the shared helper in native Node without a TypeScript loader", () => {
    const source = `import { parseStagingOrigin } from './scripts/staging-origin.mjs'; if (parseStagingOrigin('https://deployment.example.test:8443/') !== 'https://deployment.example.test:8443') process.exit(37);`;
    const result = spawnSync(process.execPath, ["--input-type=module", "-e", source], { encoding: "utf8" });
    expect(result.status).toBe(0);
    const negative = spawnSync(process.execPath, ["-e", "process.exit(37)"], { encoding: "utf8" });
    expect(negative.status).toBe(37);
  });

  it("uses the canonical HTTPS destination with redirects disabled", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ access_token: "test-bearer" }) });
    vi.stubGlobal("fetch", fetch);
    await expect(loginToStaging("HTTPS://STAGING.EXAMPLE.TEST:443/", "test-account", "test-password")).resolves.toBe("test-bearer");
    expect(fetch).toHaveBeenCalledExactlyOnceWith("https://staging.example.test/api/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: "test-account", password: "test-password" }), redirect: "error",
    });
  });

  it("rejects plaintext before native fetch", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    await expect(loginToStaging("http://unintended.example.test", "test-account", "test-password")).rejects.toThrow("HTTPS origin");
    expect(fetch).not.toHaveBeenCalled();
  });

  it.each([307, 308, 401])("refuses native status %s without reading an echoed body", async (status) => {
    const json = vi.fn().mockResolvedValue({ detail: "test-account test-password test-bearer" });
    const fetch = vi.fn().mockResolvedValue({ ok: false, status, json });
    vi.stubGlobal("fetch", fetch);
    await expect(loginToStaging("https://staging.example.test", "test-account", "test-password")).rejects.toThrow(`staging login failed (${status})`);
    expect(fetch).toHaveBeenCalledOnce();
    expect(json).not.toHaveBeenCalled();
  });

  it("redacts native transport errors", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("echo test-password")));
    await expect(loginToStaging("https://staging.example.test", "test-account", "test-password")).rejects.toThrow(/^staging login request failed$/);
  });
});

describe("staging bearer file custody", () => {
  it("creates a 0700 directory and 0600 file under a permissive umask", async () => {
    const oldUmask = process.umask(0);
    try {
      await stagingGlobalSetup(config);
    } finally {
      process.umask(oldUmask);
    }
    expect(statSync(dirname(storagePath)).mode & 0o777).toBe(0o700);
    expect(statSync(storagePath).mode & 0o777).toBe(0o600);
    expect(readdirSync(dirname(storagePath))).toEqual(["staging-user.json"]);
  });

  it("atomically replaces an old permissive file with a private inode", async () => {
    mkdirSync(dirname(storagePath), { mode: 0o755 });
    writeFileSync(storagePath, "previous state");
    chmodSync(storagePath, 0o666);
    const inode = statSync(storagePath).ino;
    await stagingGlobalSetup(config);
    expect(statSync(storagePath).ino).not.toBe(inode);
    expect(statSync(storagePath).mode & 0o777).toBe(0o600);
    expect(statSync(dirname(storagePath)).mode & 0o777).toBe(0o700);
  });

  it("rejects an auth directory symlink before login", async () => {
    const victim = join(temporaryDirectory, "outside");
    mkdirSync(victim, { mode: 0o755 });
    chmodSync(victim, 0o755); // Establish the victim's mode independently of the process umask.
    symlinkSync(victim, dirname(storagePath));
    await expect(stagingGlobalSetup(config)).rejects.toThrow("real directories");
    expect(api.post).not.toHaveBeenCalled();
    expect(readdirSync(victim)).toEqual([]);
    expect(statSync(victim).mode & 0o777).toBe(0o755);
  });

  it("rejects a symlink in an ancestor directory", async () => {
    const actual = join(temporaryDirectory, "actual");
    mkdirSync(actual);
    const link = join(temporaryDirectory, "linked");
    symlinkSync(actual, link);
    config.projects[0].use.storageState = join(link, ".auth", "staging-user.json");
    await expect(stagingGlobalSetup(config)).rejects.toThrow("real directories");
    expect(api.post).not.toHaveBeenCalled();
  });

  it("rejects a pre-existing token-path symlink without touching its target", async () => {
    mkdirSync(dirname(storagePath));
    const victim = join(temporaryDirectory, "victim");
    writeFileSync(victim, "preserve me");
    symlinkSync(victim, storagePath);
    await expect(stagingGlobalSetup(config)).rejects.toThrow("regular file");
    expect(api.post).not.toHaveBeenCalled();
    expect(readFileSync(victim, "utf8")).toBe("preserve me");
  });

  it("replaces rather than follows a destination symlink swapped immediately before rename", async () => {
    const rename = fs.renameSync;
    const victim = join(temporaryDirectory, "victim");
    writeFileSync(victim, "preserve me");
    const publication = vi.spyOn(fs, "renameSync").mockImplementationOnce((source, target) => {
      symlinkSync(victim, target);
      rename(source, target);
    });
    await stagingGlobalSetup(config);
    expect(publication).toHaveBeenCalledOnce();
    expect(readFileSync(victim, "utf8")).toBe("preserve me");
    expect(lstatSync(storagePath).isSymbolicLink()).toBe(false);
    expect(statSync(storagePath).mode & 0o777).toBe(0o600);
  });

  it("preserves the previous state and removes the private temporary file when publication fails", async () => {
    mkdirSync(dirname(storagePath));
    writeFileSync(storagePath, "previous state");
    vi.spyOn(fs, "renameSync").mockImplementationOnce(() => { throw new Error("publication failed"); });
    await expect(stagingGlobalSetup(config)).rejects.toThrow("publication failed");
    expect(readFileSync(storagePath, "utf8")).toBe("previous state");
    expect(readdirSync(dirname(storagePath))).toEqual(["staging-user.json"]);
  });
});
