import { dirname, resolve } from "node:path";
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
const E2E_RUN_ID = process.env.PLAYWRIGHT_E2E_RUN_ID ?? `run-${process.pid}`;
const E2E_DATA_DIR = process.env.PLAYWRIGHT_E2E_DATA_DIR
  ? resolve(process.env.PLAYWRIGHT_E2E_DATA_DIR)
  : resolve(HERE, ".e2e-data", E2E_RUN_ID);
const STORAGE_STATE_PATH = resolve(HERE, "tests", "e2e", ".auth", "user.json");

process.env.PLAYWRIGHT_E2E_DATA_DIR = E2E_DATA_DIR;
process.env.PLAYWRIGHT_FRONTEND_BASE_URL = FRONTEND_URL;
process.env.PLAYWRIGHT_BACKEND_BASE_URL = BACKEND_BASE_URL;
process.env.PLAYWRIGHT_FRONTEND_PORT = String(FRONTEND_PORT);
process.env.PLAYWRIGHT_BACKEND_PORT = String(BACKEND_PORT);

const isCI = !!process.env.CI;

const composerSettingsEnv: Record<string, string> = {
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
  // same preference row and makes first-session mode assertions nondeterministic.
  fullyParallel: false,
  forbidOnly: isCI,
  retries: isCI ? 2 : 0,
  workers: 1,
  reporter: isCI ? [["github"], ["html", { open: "never" }]] : [["list"], ["html", { open: "never" }]],
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
      command:
        "npm --prefix src/elspeth/web/frontend run build && " +
        "uv run --extra webui python -m uvicorn elspeth.web.app:create_app --factory " +
        `--host 127.0.0.1 --port ${BACKEND_PORT}`,
      cwd: REPO_ROOT_FROM_FRONTEND,
      url: BACKEND_HEALTH_URL,
      // Build before startup so this process serves the production SPA as
      // well as the API. Browser-security specs exercise that document path;
      // composerSettingsEnv still controls auth policy and .e2e-data isolation.
      reuseExistingServer: false,
      timeout: 60_000,
      stdout: "pipe",
      stderr: "pipe",
      env: composerSettingsEnv,
    },
    {
      command: `npm run dev -- --host 127.0.0.1 --port ${FRONTEND_PORT}`,
      url: FRONTEND_URL,
      reuseExistingServer: !isCI,
      timeout: 30_000,
      stdout: "pipe",
      stderr: "pipe",
    },
  ],
});
