import { test, type APIRequestContext, type APIResponse, type Locator, type Page } from "@playwright/test";

import {
  OIDC_EVIDENCE_PHASES,
  OidcEvidenceError,
  buildOidcEvidence,
  validateAuthConfig,
  validateAuthorizationRequest,
  validateCallbackLanding,
  validateSessionToken,
  writeOidcBearerHandoff,
  writeOidcEvidence,
  type OidcEvidencePhase,
} from "./harness/oidc-evidence";

/**
 * Real-browser evidence for the backend-for-frontend SSO walk (spec D2):
 *
 *   SPA button → GET /api/auth/sso/start (302) → IdP authorization request
 *   → IdP login → GET /api/auth/sso/callback (backend redeems the code as a
 *   confidential client) → `#/auth/callback?code=<handoff>` on the SPA →
 *   POST /api/auth/sso/complete → ELSPETH session token → API round trip.
 *
 * What is asserted is what the browser can observe: the authorization
 * request's shape, that the callback lands with a handoff code in the
 * FRAGMENT and nothing else, that the SPA scrubs the fragment BEFORE it
 * calls complete, that complete carries the code in a JSON body with no
 * bearer header, and that the token the SPA adopts is ELSPETH's for THIS
 * deployment. The IdP's tokens never reach the browser and are not looked
 * for. The IdP issuer is pinned by the backend, not observable here, and is
 * therefore not recorded as evidence.
 */

const MAX_API_RESPONSE_BYTES = 1024 * 1024;
const SESSION_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function requiredEnvironment(name: string): string {
  const value = process.env[name];
  if (value === undefined || value === "" || Buffer.byteLength(value, "utf8") > 16 * 1024) {
    throw new OidcEvidenceError("oidc_environment");
  }
  return value;
}

function requireOidc(condition: boolean, check: string): asserts condition {
  if (!condition) throw new OidcEvidenceError(check);
}

function exactStagingOrigin(raw: string): string {
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    throw new OidcEvidenceError("oidc_staging_origin");
  }
  requireOidc(
    parsed.protocol === "https:" &&
      parsed.username === "" &&
      parsed.password === "" &&
      parsed.pathname === "/" &&
      parsed.search === "" &&
      parsed.hash === "" &&
      parsed.origin === raw,
    "oidc_staging_origin",
  );
  return raw;
}

async function boundedJson(response: APIResponse, check: string): Promise<Record<string, unknown>> {
  const body = await response.body();
  requireOidc(body.length <= MAX_API_RESPONSE_BYTES, check);
  let payload: unknown;
  try {
    payload = JSON.parse(body.toString("utf8"));
  } catch {
    throw new OidcEvidenceError(check);
  }
  requireOidc(payload !== null && typeof payload === "object" && !Array.isArray(payload), check);
  return payload as Record<string, unknown>;
}

async function firstVisible(page: Page, selector: string, check: string): Promise<Locator> {
  const candidates = page.locator(selector);
  const count = await candidates.count();
  for (let index = 0; index < count; index += 1) {
    const candidate = candidates.nth(index);
    if (await candidate.isVisible()) return candidate;
  }
  throw new OidcEvidenceError(check);
}

async function apiCall(
  request: APIRequestContext,
  method: "get" | "post" | "delete",
  url: string,
  token: string,
  data?: object,
): Promise<APIResponse> {
  return request[method](url, {
    headers: { Authorization: `Bearer ${token}` },
    ...(data === undefined ? {} : { data }),
    failOnStatusCode: false,
    maxRedirects: 0,
    timeout: 15_000,
  });
}

test("completes confidential-client SSO through the handoff and a session round trip", async ({ page, request }) => {
  const stagingOrigin = exactStagingOrigin(requiredEnvironment("STAGING_BASE_URL"));
  const username = requiredEnvironment("OIDC_TEST_USERNAME");
  const password = requiredEnvironment("OIDC_TEST_PASSWORD");
  const audience = requiredEnvironment("OIDC_EXPECTED_AUDIENCE");
  const authorizationOrigin = requiredEnvironment("OIDC_EXPECTED_AUTHORIZATION_ORIGIN");
  const evidencePhase = requiredEnvironment("OIDC_EVIDENCE_PHASE") as OidcEvidencePhase;
  const evidenceFile = requiredEnvironment("OIDC_EVIDENCE_FILE");
  const bearerHandoffFile = process.env.OIDC_BEARER_HANDOFF_FILE;
  requireOidc(OIDC_EVIDENCE_PHASES.includes(evidencePhase), "oidc_evidence_phase");
  const expected = { audience, authorizationOrigin, stagingOrigin };

  const configResponse = await request.get(`${stagingOrigin}/api/auth/config`, {
    failOnStatusCode: false,
    maxRedirects: 0,
    timeout: 15_000,
  });
  requireOidc(configResponse.status() === 200, "oidc_auth_config_status");
  const authConfig = validateAuthConfig(await boundedJson(configResponse, "oidc_auth_config_body"), expected);

  // The callback landing: the backend's callback redirects the browser to
  // the SPA with the handoff code in the fragment. Observed on the frame,
  // validated for shape only — the code itself is never retained.
  let callbackLanding: string | null = null;
  let callbackLandingError: unknown = null;
  page.on("framenavigated", (frame) => {
    if (frame !== page.mainFrame() || callbackLanding !== null) return;
    const navigated = frame.url();
    let origin: string;
    try {
      origin = new URL(navigated).origin;
    } catch {
      return;
    }
    if (origin !== stagingOrigin) return;
    if (!navigated.includes("#/auth/callback")) return;
    callbackLanding = navigated;
    try {
      validateCallbackLanding(navigated, expected);
    } catch (error) {
      callbackLandingError = error;
    }
  });

  // The exchange: the SPA posts the handoff code to complete. By then the
  // fragment must already be gone from the address bar, the body must be
  // the code alone, and no bearer header may accompany it (there is no
  // token yet). The response is the ELSPETH token, checked below.
  let completeChecked = false;
  let resolveComplete!: () => void;
  const completeObserved = new Promise<void>((resolve) => {
    resolveComplete = resolve;
  });
  await page.route(`${stagingOrigin}/api/auth/sso/complete`, async (route) => {
    try {
      const completeRequest = route.request();
      const headers = await completeRequest.allHeaders();
      const current = new URL(page.url());
      let body: unknown = null;
      try {
        body = JSON.parse(completeRequest.postData() ?? "null");
      } catch {
        body = null;
      }
      requireOidc(completeRequest.method() === "POST", "oidc_complete_method");
      requireOidc(current.origin === stagingOrigin && current.search === "" && current.hash === "", "oidc_callback_scrub");
      requireOidc(!("authorization" in headers), "oidc_complete_authorization");
      requireOidc(
        body !== null &&
          typeof body === "object" &&
          !Array.isArray(body) &&
          Object.keys(body).join(",") === "code" &&
          typeof (body as { code: unknown }).code === "string",
        "oidc_complete_body",
      );
      completeChecked = true;
      await route.continue();
    } catch {
      await route.abort("blockedbyclient");
    } finally {
      resolveComplete();
    }
  });

  await page.goto(stagingOrigin, { waitUntil: "domcontentloaded" });
  const authorizationRequestPromise = page.waitForRequest(
    (candidate) => {
      try {
        return new URL(candidate.url()).origin === authorizationOrigin;
      } catch {
        return false;
      }
    },
    { timeout: 15_000 },
  );
  const startRequestPromise = page.waitForRequest(authConfig.ssoStartUrl, { timeout: 15_000 });
  await page.getByRole("button", { name: "Sign in with single sign-on", exact: true }).click();
  // The button is a navigation to the BACKEND's start route; the IdP request
  // is the backend's 302, not something the page built.
  await startRequestPromise;
  const authorizationRequest = await authorizationRequestPromise;
  validateAuthorizationRequest(authorizationRequest.url(), expected);

  await (await firstVisible(page, 'input[name="username"], input#signInFormUsername', "oidc_username_control")).fill(username);
  await (await firstVisible(page, 'input[name="password"], input#signInFormPassword', "oidc_password_control")).fill(password);
  await (
    await firstVisible(
      page,
      'input[name="signInSubmitButton"], button:has-text("Sign in")',
      "oidc_submit_control",
    )
  ).click();

  await completeObserved;
  requireOidc(completeChecked, "oidc_complete_exchange");
  requireOidc(callbackLanding !== null, "oidc_callback_observation");
  if (callbackLandingError !== null) throw callbackLandingError;
  await page.waitForFunction(() => typeof localStorage.getItem("auth_token") === "string", undefined, { timeout: 30_000 });
  const sessionToken = await page.evaluate(() => localStorage.getItem("auth_token"));
  requireOidc(typeof sessionToken === "string" && sessionToken.length > 0, "oidc_session_token");
  const claims = validateSessionToken(sessionToken, expected);

  const authMe = await apiCall(request, "get", `${stagingOrigin}/api/auth/me`, sessionToken);
  const authMeStatus = authMe.status();
  requireOidc(authMeStatus === 200, "oidc_auth_me");

  let sessionId: string | null = null;
  let sessionCreateStatus = 0;
  let sessionReadStatus = 0;
  let sessionDeleteStatus = 0;
  let sessionFailure: unknown = null;
  try {
    const created = await apiCall(request, "post", `${stagingOrigin}/api/sessions`, sessionToken, {});
    sessionCreateStatus = created.status();
    requireOidc(sessionCreateStatus === 201, "oidc_session_create");
    const createdPayload = await boundedJson(created, "oidc_session_create_body");
    requireOidc(typeof createdPayload.id === "string" && SESSION_ID.test(createdPayload.id), "oidc_session_identity");
    sessionId = createdPayload.id;
    const read = await apiCall(request, "get", `${stagingOrigin}/api/sessions/${sessionId}`, sessionToken);
    sessionReadStatus = read.status();
    requireOidc(sessionReadStatus === 200, "oidc_session_read");
    const readPayload = await boundedJson(read, "oidc_session_read_body");
    requireOidc(readPayload.id === sessionId, "oidc_session_binding");
  } catch (error) {
    sessionFailure = error;
  } finally {
    if (sessionId !== null) {
      const deleted = await apiCall(request, "delete", `${stagingOrigin}/api/sessions/${sessionId}`, sessionToken);
      sessionDeleteStatus = deleted.status();
    }
  }
  if (sessionFailure !== null) throw sessionFailure;
  requireOidc(sessionDeleteStatus === 204, "oidc_session_delete");

  if (bearerHandoffFile !== undefined && bearerHandoffFile !== "") {
    writeOidcBearerHandoff(bearerHandoffFile, sessionToken);
  }

  const evidence = buildOidcEvidence({
    phase: evidencePhase,
    timestamp: new Date().toISOString(),
    ...expected,
    subjectSha256: claims.subjectSha256,
    authMeStatus,
    sessionCreateStatus,
    sessionReadStatus,
    sessionDeleteStatus,
  });
  writeOidcEvidence(evidenceFile, evidence);
});
