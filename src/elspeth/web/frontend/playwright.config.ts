import { lstatSync, readdirSync, realpathSync } from "node:fs";
import { dirname, isAbsolute, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

import { defineConfig, devices } from "@playwright/test";

function portFromEnv(name: string, fallback: number): number {
  const value = process.env[name];
  if (value === undefined || value === "") {
    return fallback;
  }
  const port = Number(value);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(`${name} must be an integer TCP port, got '${value}'`);
  }
  return port;
}

const FRONTEND_PORT = portFromEnv("PLAYWRIGHT_FRONTEND_PORT", 5173);
const BACKEND_PORT = portFromEnv("PLAYWRIGHT_BACKEND_PORT", 8451);
const FRONTEND_URL = `http://127.0.0.1:${FRONTEND_PORT}`;
const BACKEND_BASE_URL = `http://127.0.0.1:${BACKEND_PORT}`;
const BACKEND_HEALTH_URL = `${BACKEND_BASE_URL}/api/health`;

const REPO_ROOT_FROM_FRONTEND = "../../../..";

// Anchor path resolution to this config file rather than process.cwd() —
// Playwright's runtime cwd varies (config-load vs test-run vs globalSetup)
// and a relative storageState path produced subtly different files in each
// phase. Keep it absolute here and share the same anchor with globalSetup.
const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, REPO_ROOT_FROM_FRONTEND);
const PRIVATE_RUN_BASE = resolve(REPO_ROOT, ".elspeth", "acceptance");

function shellArgument(value: string): string {
  return "'" + value.replaceAll("'", "'\"'\"'") + "'";
}

function within(parent: string, target: string): boolean {
  const child = relative(parent, target);
  return child === "" || (child !== ".." && !child.startsWith(`..${sep}`) && !isAbsolute(child));
}

function requirePrivateTree(path: string): void {
  let stat;
  try {
    stat = lstatSync(path);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return;
    throw error;
  }
  if (stat.isSymbolicLink() || (!stat.isDirectory() && (!stat.isFile() || stat.nlink !== 1))) {
    throw new Error("Private Playwright paths must contain only real directories and singly linked regular files");
  }
  if (stat.isDirectory()) {
    for (const name of readdirSync(path)) requirePrivateTree(resolve(path, name));
  }
}

function privateRunDirectory(): string | undefined {
  const value = process.env.PLAYWRIGHT_PRIVATE_RUN_DIR;
  if (value === undefined) return undefined;
  if (value === "" || value.trim() !== value || !isAbsolute(value) || /[\x00-\x1f\x7f?#]/.test(value)) {
    throw new Error("PLAYWRIGHT_PRIVATE_RUN_DIR must name an absolute private directory");
  }
  const directory = resolve(value);
  if (directory === PRIVATE_RUN_BASE || !within(PRIVATE_RUN_BASE, directory)) {
    throw new Error("PLAYWRIGHT_PRIVATE_RUN_DIR must name a run directory under the checkout's .elspeth/acceptance runtime area");
  }
  if (!lstatSync(directory).isDirectory() || realpathSync(directory) !== directory) {
    throw new Error("PLAYWRIGHT_PRIVATE_RUN_DIR must be an existing real directory without symlink ancestors");
  }
  requirePrivateTree(directory);
  for (const name of ["PLAYWRIGHT_FRONTEND_PORT", "PLAYWRIGHT_BACKEND_PORT"]) {
    if (!process.env[name]) throw new Error(`${name} must be explicit for a private Playwright run`);
  }
  if (FRONTEND_PORT === BACKEND_PORT) throw new Error("Private Playwright frontend/backend ports must differ");
  if (Object.keys(process.env).some((name) => name.startsWith("ELSPETH_WEB__"))) {
    throw new Error("Private Playwright runs require an environment without inherited ELSPETH_WEB__ settings");
  }
  if (process.env.PLAYWRIGHT_HTML_OUTPUT_DIR !== undefined || process.env.PLAYWRIGHT_HTML_REPORT !== undefined) {
    throw new Error("Private Playwright runs require no inherited HTML report path override");
  }
  const data = resolve(directory, "backend");
  if (process.env.PLAYWRIGHT_E2E_DATA_DIR !== undefined && resolve(process.env.PLAYWRIGHT_E2E_DATA_DIR) !== data) {
    throw new Error("PLAYWRIGHT_E2E_DATA_DIR conflicts with the private run directory");
  }
  const build = resolve(directory, "frontend-dist", "index.html");
  let built = false;
  try {
    built = lstatSync(build).isFile();
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }
  if (!built) {
    throw new Error("Private Playwright run requires its separately checked frontend-dist/index.html build");
  }
  return directory;
}

const PRIVATE_RUN_DIR = privateRunDirectory();
const E2E_RUN_ID = process.env.PLAYWRIGHT_E2E_RUN_ID ?? `run-${process.pid}`;
const E2E_DATA_DIR = PRIVATE_RUN_DIR !== undefined
  ? resolve(PRIVATE_RUN_DIR, "backend")
  : process.env.PLAYWRIGHT_E2E_DATA_DIR
  ? resolve(process.env.PLAYWRIGHT_E2E_DATA_DIR)
  : resolve(HERE, ".e2e-data", E2E_RUN_ID);
const STORAGE_STATE_PATH = PRIVATE_RUN_DIR === undefined
  ? resolve(HERE, "tests", "e2e", ".auth", "user.json")
  : resolve(PRIVATE_RUN_DIR, "auth", "user.json");
const ROLE_ADMIN_USERNAME = "e2e-role-admin";
const ROLE_ADMIN_PASSWORD = "pw-admin-placeholder"; // secret-scan: allow-this-line

process.env.PLAYWRIGHT_E2E_DATA_DIR = E2E_DATA_DIR;
process.env.PLAYWRIGHT_FRONTEND_BASE_URL = FRONTEND_URL;
process.env.PLAYWRIGHT_BACKEND_BASE_URL = BACKEND_BASE_URL;
process.env.PLAYWRIGHT_FRONTEND_PORT = String(FRONTEND_PORT);
process.env.PLAYWRIGHT_BACKEND_PORT = String(BACKEND_PORT);
process.env.PLAYWRIGHT_ROLE_ADMIN_USERNAME = ROLE_ADMIN_USERNAME;
process.env.PLAYWRIGHT_ROLE_ADMIN_PASSWORD = ROLE_ADMIN_PASSWORD;

const isCI = !!process.env.CI;

const composerSettingsEnv: Record<string, string> = {
  PLAYWRIGHT_E2E_DATA_DIR: E2E_DATA_DIR,
  PLAYWRIGHT_ROLE_ADMIN_USERNAME: ROLE_ADMIN_USERNAME,
  PLAYWRIGHT_ROLE_ADMIN_PASSWORD: ROLE_ADMIN_PASSWORD,
  ELSPETH_WEB__data_dir: E2E_DATA_DIR,
  ELSPETH_WEB__registration_mode: "open",
  ELSPETH_WEB__auth_provider: "local",
  ELSPETH_WEB__composer_max_composition_turns: "15",
  ELSPETH_WEB__composer_max_discovery_turns: "10",
  ELSPETH_WEB__composer_timeout_seconds: "180.0",
  ELSPETH_WEB__composer_rate_limit_per_minute: "60",
  ELSPETH_WEB__auth_rate_limit_per_minute: "120",
  ELSPETH_WEB__e2e_state_seed_enabled: "true",
  // Catalog acceptance exercises representative user-configurable plugins
  // outside the universal minimum. Authorize them only in this ephemeral
  // Playwright deployment; production REQUIRED_WEB_PLUGIN_IDS is unchanged.
  ELSPETH_WEB__plugin_allowlist: JSON.stringify([
    "transform:value_transform",
    "sink:database",
  ]),
  // Keep the operator-profiled LLM catalog surface available without a
  // credential or network call. The E2E schema test verifies the public
  // alias-only contract; it never executes this Bedrock profile.
  ELSPETH_WEB__llm_profiles: JSON.stringify({
    "e2e-bedrock": {
      provider: "bedrock",
      model: "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
    },
  }),
  // A deployment that configures llm_profiles must designate its standard
  // profile: WebSettings refuses to start otherwise (see the
  // _validate_default_llm_profile_alias model validator in
  // src/elspeth/web/config.py). Without this the Playwright-managed backend
  // raises before binding its port and every spec fails on webServer timeout.
  ELSPETH_WEB__default_llm_profile: "e2e-bedrock",
  // Placeholder JWT signing key for the local webServer instance. The
  // backend refuses startup if cors_origins contains a non-loopback host
  // and secret_key is left at its default; here we set it explicitly so
  // the ELSPETH_WEB__SECRET_KEY guard in src/elspeth/web/config.py is
  // satisfied even if a non-loopback CORS origin is configured later.
  ELSPETH_WEB__secret_key: "e2e-jwt-placeholder-key-000000000000", // secret-scan: allow-this-line
  // Local-only shareable-review HMAC key for the Playwright-managed backend.
  // WebSettings requires an operator-provided base64 string and decodes it to
  // bytes before constructing ShareTokenSigner.
  ELSPETH_WEB__shareable_link_signing_key: "ZWxzcGV0aC1lMmUtc2hhcmUta2V5LTAwMDAwMDAwMDA=", // secret-scan: allow-this-line

  ELSPETH_WEB__cors_origins: JSON.stringify([FRONTEND_URL]),
  // Hermetic composer availability (elspeth-e425a36805). litellm runs
  // python-dotenv's load_dotenv() at import unless LITELLM_MODE is
  // "PRODUCTION", and find_dotenv walks UP from the package, so on a
  // developer box the Playwright-managed backend picked up the checkout's
  // .env (a worktree sits beneath it too): the developer's provider key,
  // composer models and turn/timeout limits — the .env's upper-case keys
  // beat the lower-case pins above — and litellm logged completion() calls
  // against those models at boot. CI has no .env anywhere above its
  // checkout, so it booted without any of that. The tall-dialog full-page
  // baseline captured the split: green locally with an empty notice band,
  // red on CI where the band carried "Service unavailable: … missing
  // OPENAI_API_KEY." This pin is a no-op on CI and makes the local backend
  // boot the same way. It is litellm's only use of LITELLM_MODE.
  LITELLM_MODE: "PRODUCTION",
  // No ambient provider credential reaches the backend from the developer's
  // shell either: every key in PROVIDER_REQUIRED_ENV_KEYS
  // (src/elspeth/web/composer/provider_config.py) is blanked, and an empty
  // value counts as missing (_missing_required_env_keys in
  // src/elspeth/web/composer/availability.py). The composer is therefore
  // unavailable in every E2E run — the state CI has always tested — and the
  // model names are pinned so the notice copy the tall-dialog baseline
  // carries does not move with the product default.
  OPENAI_API_KEY: "",
  ANTHROPIC_API_KEY: "",
  AZURE_API_KEY: "",
  OPENROUTER_API_KEY: "",
  ELSPETH_WEB__composer_model: "gpt-5.5",
  ELSPETH_WEB__composer_advisor_model: "anthropic/claude-sonnet-4-6",
};

if (PRIVATE_RUN_DIR !== undefined) {
  // The existing local store factories derive their real SQLite paths from
  // data_dir. Explicit nulls plus --no-dotenv prevent an ambient DB redirect.
  composerSettingsEnv.ELSPETH_WEB__session_db_url = "null";
  composerSettingsEnv.ELSPETH_WEB__landscape_url = "null";
  composerSettingsEnv.ELSPETH_WEB__payload_store_path = resolve(E2E_DATA_DIR, "payloads");
}

const PRIVATE_BACKEND_COMMAND = [
  "uv run --frozen --no-sync elspeth --no-dotenv composer users add " + shellArgument(ROLE_ADMIN_USERNAME) +
    " --password " + shellArgument(ROLE_ADMIN_PASSWORD) + " --display-name " + shellArgument("E2E Role Administrator") +
    " --data-dir " + shellArgument(E2E_DATA_DIR),
  "uv run --frozen --no-sync elspeth --no-dotenv composer users bootstrap-admin local " + shellArgument(ROLE_ADMIN_USERNAME) +
    " --note " + shellArgument("Playwright workload-role bootstrap") + " --data-dir " + shellArgument(E2E_DATA_DIR),
  "uv run --frozen --no-sync --extra webui python -m uvicorn elspeth.web.app:create_app --factory --host 127.0.0.1 --port " +
    shellArgument(String(BACKEND_PORT)),
].join(" && ");

export default defineConfig({
  testDir: "./tests/e2e",
  // `**/*.test.ts` are vitest unit tests (e.g. harness/classify.test.ts) which
  // import `vitest` — that collides with Playwright's expect. Playwright owns
  // `*.spec.ts`; vitest owns `*.test.ts`.
  //
  // `**/*.staging.spec.ts` target a LIVE staging deployment with operator auth
  // (tests/e2e/.auth/staging-user.json) and have no local webServer. The CI
  // e2e lane boots an ephemeral local backend+frontend and has no staging
  // artifact, so collecting them here fails with ENOENT on staging-user.json.
  // They run only via `npm run test:e2e:staging`
  // (playwright.staging.config.ts), never in the default/CI run.
  testIgnore: [
    "**/setup/**",
    "**/page-objects/**",
    "**/helpers/**",
    "**/*.test.ts",
    "**/*.staging.spec.ts",
  ],
  // The suite uses one authenticated account and verifies account-scoped
  // composer preferences. Running specs in parallel lets tests race through the
  // same preference row and makes first-session preference assertions nondeterministic.
  fullyParallel: false,
  forbidOnly: isCI,
  retries: isCI ? 2 : 0,
  workers: 1,
  reporter: PRIVATE_RUN_DIR === undefined
    ? (isCI ? [["github"], ["html", { open: "never" }]] : [["list"], ["html", { open: "never" }]])
    : [[isCI ? "github" : "list"], ["html", { open: "never", outputFolder: resolve(PRIVATE_RUN_DIR, "playwright-report") }]],
  ...(PRIVATE_RUN_DIR === undefined ? {} : { outputDir: resolve(PRIVATE_RUN_DIR, "test-results") }),
  expect: {
    timeout: 5_000,
  },
  globalSetup: "./tests/e2e/setup/global-setup.ts",
  use: {
    baseURL: FRONTEND_URL,
    trace: "retain-on-failure",
    video: "retain-on-failure",
    screenshot: "only-on-failure",
    storageState: STORAGE_STATE_PATH,
    actionTimeout: 10_000,
    navigationTimeout: 15_000,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: [
    {
      command: PRIVATE_RUN_DIR !== undefined ? PRIVATE_BACKEND_COMMAND :
        'uv run elspeth composer users add "$PLAYWRIGHT_ROLE_ADMIN_USERNAME" ' +
        '--password "$PLAYWRIGHT_ROLE_ADMIN_PASSWORD" --display-name "E2E Role Administrator" ' +
        '--data-dir "$PLAYWRIGHT_E2E_DATA_DIR" && ' +
        'uv run elspeth composer users bootstrap-admin local "$PLAYWRIGHT_ROLE_ADMIN_USERNAME" ' +
        '--note "Playwright workload-role bootstrap" --data-dir "$PLAYWRIGHT_E2E_DATA_DIR" && ' +
        "npm --prefix src/elspeth/web/frontend run build && " +
        "uv run --extra webui python -m uvicorn elspeth.web.app:create_app --factory " +
        `--host 127.0.0.1 --port ${BACKEND_PORT}`,
      cwd: REPO_ROOT_FROM_FRONTEND,
      url: BACKEND_HEALTH_URL,
      // Default/CI builds before startup for production-document checks.
      // Explicit private tutorial runs use Vite and a separately checked private
      // build. FastAPI's product asset path remains unchanged and read-only.
      reuseExistingServer: false,
      // Admin bootstrap and the SPA build share this startup watchdog with
      // backend readiness; allow their combined cost on shared CI runners.
      timeout: 180_000,
      stdout: "pipe",
      stderr: "pipe",
      env: composerSettingsEnv,
    },
    {
      command: PRIVATE_RUN_DIR === undefined
        ? `npm run dev -- --host 127.0.0.1 --port ${FRONTEND_PORT}`
        : "npm run dev -- --host 127.0.0.1 --port " + shellArgument(String(FRONTEND_PORT)) + " --strictPort",
      url: FRONTEND_URL,
      reuseExistingServer: PRIVATE_RUN_DIR === undefined ? !isCI : false,
      timeout: 30_000,
      stdout: "pipe",
      stderr: "pipe",
    },
  ],
});
