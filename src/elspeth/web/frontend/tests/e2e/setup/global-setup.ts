import {
  closeSync, constants, fchmodSync, fsyncSync, lstatSync, mkdirSync,
  openSync, realpathSync, renameSync, rmSync, writeFileSync,
} from "node:fs";
import { randomUUID } from "node:crypto";
import { dirname, isAbsolute, relative, resolve, sep } from "node:path";

import { request, type FullConfig } from "@playwright/test";

// Placeholder credential consumed only by the local playwright webServer,
// which spins up a fresh ELSPETH_WEB__data_dir per test session. Not a
// real password; the hook's credential scanner flags any high-entropy
// quoted string here, so keep this short and obviously synthetic.
const TEST_USER = {
  username: "e2e-tester",
  password: "pw-placeholder", // secret-scan: allow-this-line
  display_name: "E2E Tester",
};

const FRONTEND_PORT = process.env.PLAYWRIGHT_FRONTEND_PORT ?? "5173";
const BACKEND_PORT = process.env.PLAYWRIGHT_BACKEND_PORT ?? "8451";
const FRONTEND_ORIGIN =
  process.env.PLAYWRIGHT_FRONTEND_BASE_URL ?? `http://127.0.0.1:${FRONTEND_PORT}`;
const BACKEND_BASE_URL =
  process.env.PLAYWRIGHT_BACKEND_BASE_URL ?? `http://127.0.0.1:${BACKEND_PORT}`;
const TOKEN_KEY = "auth_token";
const ROLE_ADMIN_USERNAME = process.env.PLAYWRIGHT_ROLE_ADMIN_USERNAME;
const ROLE_ADMIN_PASSWORD = process.env.PLAYWRIGHT_ROLE_ADMIN_PASSWORD;

async function obtainToken(apiBaseURL: string): Promise<string> {
  const ctx = await request.newContext({ baseURL: apiBaseURL });
  try {
    const registerResp = await ctx.post("/api/auth/register", {
      data: TEST_USER,
    });

    if (registerResp.ok()) {
      const body = (await registerResp.json()) as { access_token: string };
      return body.access_token;
    }

    if (registerResp.status() !== 409) {
      const detail = await registerResp.text();
      throw new Error(
        `register failed (${registerResp.status()}): ${detail.slice(0, 500)}`,
      );
    }

    const loginResp = await ctx.post("/api/auth/login", {
      data: { username: TEST_USER.username, password: TEST_USER.password },
    });
    if (!loginResp.ok()) {
      const detail = await loginResp.text();
      throw new Error(
        `login fallback failed (${loginResp.status()}): ${detail.slice(0, 500)}`,
      );
    }
    const body = (await loginResp.json()) as { access_token: string };
    return body.access_token;
  } finally {
    await ctx.dispose();
  }
}

async function login(apiBaseURL: string, username: string, password: string): Promise<string> {
  const ctx = await request.newContext({ baseURL: apiBaseURL });
  try {
    const resp = await ctx.post("/api/auth/login", {
      data: { username, password },
    });
    if (!resp.ok()) {
      const detail = await resp.text();
      throw new Error(
        `role-admin login failed (${resp.status()}): ${detail.slice(0, 500)}`,
      );
    }
    const body = (await resp.json()) as { access_token: string };
    return body.access_token;
  } finally {
    await ctx.dispose();
  }
}

async function grantPipelineUserRole(
  apiBaseURL: string,
  userToken: string,
): Promise<void> {
  if (!ROLE_ADMIN_USERNAME || !ROLE_ADMIN_PASSWORD) {
    throw new Error("Playwright role-admin credentials were not configured");
  }

  const userCtx = await request.newContext({
    baseURL: apiBaseURL,
    extraHTTPHeaders: { Authorization: `Bearer ${userToken}` },
  });
  let identityId: string;
  try {
    const resp = await userCtx.get("/api/auth/me");
    if (!resp.ok()) {
      const detail = await resp.text();
      throw new Error(
        `read E2E identity failed (${resp.status()}): ${detail.slice(0, 500)}`,
      );
    }
    const body = (await resp.json()) as { user_id: string };
    identityId = body.user_id;
  } finally {
    await userCtx.dispose();
  }

  const adminToken = await login(
    apiBaseURL,
    ROLE_ADMIN_USERNAME,
    ROLE_ADMIN_PASSWORD,
  );
  const adminCtx = await request.newContext({
    baseURL: apiBaseURL,
    extraHTTPHeaders: { Authorization: `Bearer ${adminToken}` },
  });
  try {
    const resp = await adminCtx.post("/api/auth/admin/roles", {
      data: {
        identity_id: identityId,
        role: "user",
        note: "Playwright shared workload account",
      },
    });
    if (!resp.ok()) {
      const detail = await resp.text();
      throw new Error(
        `grant E2E user role failed (${resp.status()}): ${detail.slice(0, 500)}`,
      );
    }
  } finally {
    await adminCtx.dispose();
  }
}

async function markTutorialCompleted(apiBaseURL: string, token: string): Promise<void> {
  const ctx = await request.newContext({
    baseURL: apiBaseURL,
    extraHTTPHeaders: { Authorization: `Bearer ${token}` },
  });
  try {
    const resp = await ctx.patch("/api/composer-preferences", {
      data: {
        tutorial_completed_at: new Date().toISOString(),
        tutorial_completed_via: "skip",
      },
    });
    if (!resp.ok()) {
      const detail = await resp.text();
      throw new Error(
        `mark tutorial completed failed (${resp.status()}): ${detail.slice(0, 500)}`,
      );
    }
  } finally {
    await ctx.dispose();
  }
}

function preparePrivateStoragePath(path: string, root: string): void {
  const child = relative(root, path);
  if (child === "" || child === ".." || child.startsWith(`..${sep}`) || isAbsolute(child) || realpathSync(root) !== root) {
    throw new Error("Private Playwright storageState must remain under the real private run directory");
  }
  let directory = root;
  for (const component of relative(root, dirname(path)).split(sep).filter(Boolean)) {
    directory = resolve(directory, component);
    try {
      const stat = lstatSync(directory);
      if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error("Private auth path requires real directories");
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
      mkdirSync(directory, { mode: 0o700 });
    }
  }
  try {
    const stat = lstatSync(path);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1) throw new Error("Private auth storage must be a singly linked regular file");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }
  const fd = openSync(dirname(path), constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
  try {
    fchmodSync(fd, 0o700);
  } finally {
    closeSync(fd);
  }
}

function writeStorageState(path: string, token: string, privateRoot: string | undefined): void {
  const state = {
    cookies: [],
    origins: [
      {
        origin: FRONTEND_ORIGIN,
        localStorage: [{ name: TOKEN_KEY, value: token }],
      },
    ],
  };
  if (privateRoot === undefined) {
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, JSON.stringify(state, null, 2), { encoding: "utf-8" });
    return;
  }
  preparePrivateStoragePath(path, privateRoot);
  const temporary = resolve(dirname(path), `.playwright-${randomUUID()}.tmp`);
  const fd = openSync(temporary, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
  try {
    try {
      fchmodSync(fd, 0o600);
      writeFileSync(fd, JSON.stringify(state, null, 2), { encoding: "utf-8" });
      fsyncSync(fd);
    } finally {
      closeSync(fd);
    }
    renameSync(temporary, path);
  } finally {
    rmSync(temporary, { force: true });
  }
}

export default async function globalSetup(config: FullConfig): Promise<void> {
  const configuredPath = config.projects[0].use.storageState;
  if (typeof configuredPath !== "string") throw new Error("Playwright storageState must be a configured file path");
  const storagePath = resolve(configuredPath);
  const privateValue = process.env.PLAYWRIGHT_PRIVATE_RUN_DIR;
  const privateRoot = privateValue === undefined ? undefined : resolve(privateValue);
  if (privateRoot !== undefined) preparePrivateStoragePath(storagePath, privateRoot);
  const token = await obtainToken(BACKEND_BASE_URL);
  // Registration authenticates and activates the local identity, but the
  // workload boundary separately requires a live deployment-wide user role.
  // Exercise the same audited admin API an operator uses rather than weakening
  // that production boundary for the test deployment.
  await grantPipelineUserRole(BACKEND_BASE_URL, token);
  // Most E2E specs predate the first-run tutorial and assert on the normal
  // composer shell. Keep the baseline tester out of tutorial mode; specs that
  // exercise the tutorial explicitly reset the preference in their own setup.
  await markTutorialCompleted(BACKEND_BASE_URL, token);
  writeStorageState(storagePath, token, privateRoot);
  // Confirm to the operator that globalSetup completed — otherwise a
  // mis-pathed write looks identical to "globalSetup never ran" in the
  // failure output.
  console.log(`[globalSetup] wrote storageState to ${storagePath}`);
}
