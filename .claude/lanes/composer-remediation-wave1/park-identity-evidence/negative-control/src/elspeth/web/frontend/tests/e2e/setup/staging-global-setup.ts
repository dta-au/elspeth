// Staging-targeted globalSetup. Reads STAGING_BASE_URL, STAGING_USERNAME,
// and STAGING_PASSWORD from the environment, logs in against the deployed
// backend, and writes the same storageState shape that the local global
// setup writes — so every spec sees an auth_token in localStorage at the
// staging frontend origin.
//
// Differences from the local global-setup.ts:
//   - No register-then-fallback dance. Staging runs with
//     ELSPETH_WEB__REGISTRATION_MODE=closed; we expect the operator to
//     have created the user out of band. Bare login only.
//   - The storage state origin matches the staging URL (not localhost),
//     because the frontend reads auth_token via window.localStorage
//     scoped by origin.

import fs, {
  closeSync, constants, fchmodSync, fsyncSync, lstatSync, mkdirSync,
  openSync, rmSync, writeFileSync,
} from "node:fs";
import { randomUUID } from "node:crypto";
import { dirname, parse, resolve, sep } from "node:path";

import { request, type FullConfig } from "@playwright/test";

import { configureStagingOrigin } from "../../../scripts/staging-origin.mjs";

function required(name: string): string {
  const v = process.env[name];
  if (!v) {
    throw new Error(`${name} must be set for the staging Playwright run`);
  }
  return v;
}

async function obtainToken(
  apiBaseURL: string,
  username: string,
  password: string,
): Promise<string> {
  const ctx = await request.newContext({ baseURL: apiBaseURL, ignoreHTTPSErrors: false });
  try {
    let resp;
    try {
      resp = await ctx.post("/api/auth/login", {
        data: { username, password },
        maxRedirects: 0,
      });
    } catch {
      // Playwright transport errors can include request details.
      throw new Error("staging login request failed");
    }
    if (!resp.ok()) {
      throw new Error(`staging login failed (${resp.status()})`);
    }
    let body: unknown;
    try {
      body = await resp.json();
    } catch {
      throw new Error("staging login returned invalid JSON");
    }
    if (
      typeof body !== "object" || body === null || !("access_token" in body) ||
      typeof body.access_token !== "string" || body.access_token.length === 0
    ) {
      throw new Error("staging login returned no access token");
    }
    return body.access_token;
  } finally {
    await ctx.dispose();
  }
}

function prepareStoragePath(path: string): void {
  const directory = dirname(path);
  // The checkout is operator-owned. Reject pre-existing directory symlinks
  // before changing permissions or writing credentials beneath them.
  const root = parse(directory).root;
  let current = root;
  for (const component of directory.slice(root.length).split(sep)) {
    current = resolve(current, component);
    try {
      if (!lstatSync(current).isDirectory()) {
        throw new Error("staging auth storage requires real directories");
      }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT" || current !== directory) throw error;
      mkdirSync(directory, { mode: 0o700 });
    }
  }
  const directoryFd = openSync(directory, constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW);
  try {
    fchmodSync(directoryFd, 0o700);
  } finally {
    closeSync(directoryFd);
  }
  try {
    if (!lstatSync(path).isFile()) throw new Error("staging auth storage must be a regular file");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }
}

function writeStorageState(path: string, origin: string, token: string): void {
  const state = {
    cookies: [],
    origins: [
      {
        origin,
        localStorage: [{ name: "auth_token", value: token }],
      },
    ],
  };
  prepareStoragePath(path);
  const temporaryPath = resolve(dirname(path), `.staging-${randomUUID()}.tmp`);
  const fd = openSync(temporaryPath, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
  try {
    try {
      fchmodSync(fd, 0o600);
      writeFileSync(fd, JSON.stringify(state, null, 2), { encoding: "utf-8" });
      fsyncSync(fd);
    } finally {
      closeSync(fd);
    }
    // Rename replaces the entry; it never follows a target symlink.
    fs.renameSync(temporaryPath, path);
  } finally {
    rmSync(temporaryPath, { force: true });
  }
}

export default async function stagingGlobalSetup(config: FullConfig): Promise<void> {
  const baseURL = configureStagingOrigin();
  const username = required("STAGING_USERNAME");
  const password = required("STAGING_PASSWORD");
  const storagePath = config.projects[0].use.storageState;
  if (typeof storagePath !== "string") throw new Error("staging storageState must be a file path");
  const path = resolve(storagePath);
  prepareStoragePath(path);
  const token = await obtainToken(baseURL, username, password);
  writeStorageState(path, baseURL, token);
}
