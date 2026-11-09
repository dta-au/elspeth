// Shared by Playwright setup and the directly executed Node tutorial harness.
export function parseStagingOrigin(value) {
  const message = "STAGING_BASE_URL must be an HTTPS origin without credentials, path, query or fragment";
  // Reject parser repairs, userinfo and empty ?/# before canonicalization.
  if (typeof value !== "string" || !/^https:\/\/[^\s\u0000-\u0020\u007f/?#@\\]+\/?$/i.test(value)) {
    throw new Error(message);
  }
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error(message);
  }
  if (
    parsed.protocol !== "https:" || parsed.hostname === "" ||
    parsed.username !== "" || parsed.password !== "" ||
    parsed.pathname !== "/" || parsed.search !== "" || parsed.hash !== ""
  ) {
    throw new Error(message);
  }
  return parsed.origin;
}

export function configureStagingOrigin() {
  const origin = parseStagingOrigin(process.env.STAGING_BASE_URL);
  const backend = process.env.PLAYWRIGHT_BACKEND_BASE_URL;
  if (backend !== undefined && parseStagingOrigin(backend) !== origin) {
    throw new Error("PLAYWRIGHT_BACKEND_BASE_URL must match STAGING_BASE_URL");
  }
  process.env.PLAYWRIGHT_BACKEND_BASE_URL = origin;
  return origin;
}

export async function loginToStaging(origin, username, password) {
  const destination = parseStagingOrigin(origin);
  let response;
  try {
    response = await fetch(`${destination}/api/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
      redirect: "error",
    });
  } catch {
    throw new Error("staging login request failed");
  }
  if (!response.ok) throw new Error(`staging login failed (${response.status})`);
  let body;
  try {
    body = await response.json();
  } catch {
    throw new Error("staging login returned invalid JSON");
  }
  if (typeof body !== "object" || body === null || typeof body.access_token !== "string" || body.access_token.length === 0) {
    throw new Error("staging login returned no access token");
  }
  return body.access_token;
}
