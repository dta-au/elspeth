import { chmodSync, lstatSync, mkdirSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import {
  OidcEvidenceError,
  buildOidcEvidence,
  validateAuthConfig,
  validateAuthorizationRequest,
  validateCallbackLanding,
  validateSessionToken,
  writeOidcBearerHandoff,
  writeOidcEvidence,
} from "./oidc-evidence";

const directories: string[] = [];

afterEach(() => {
  for (const directory of directories.splice(0)) {
    try {
      chmodSync(directory, 0o700);
    } catch {
      // The test may already have removed or replaced the directory.
    }
    rmSync(directory, { force: true, recursive: true });
  }
});

function base64Url(value: object): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function token(claims: Record<string, unknown>): string {
  return `${base64Url({ alg: "HS256", typ: "JWT" })}.${base64Url(claims)}.signature`;
}

const expected = {
  audience: "client-id",
  authorizationOrigin: "https://acceptance.auth.ap-southeast-2.amazoncognito.com",
  stagingOrigin: "https://elspeth.acceptance.example",
} as const;

const CHALLENGE = "c".repeat(43);
const HANDOFF = "h".repeat(43);

function sessionClaims(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    iss: "elspeth",
    aud: expected.stagingOrigin,
    provider: "oidc",
    sub: "identity-0001",
    username: "person",
    jti: "token-0001",
    iat: 1_900_000_000,
    exp: 2_000_000_000,
    ...overrides,
  };
}

function authorizationUrl(overrides: Record<string, string | null> = {}, origin: string = expected.authorizationOrigin): string {
  const params: Record<string, string | null> = {
    response_type: "code",
    client_id: expected.audience,
    redirect_uri: `${expected.stagingOrigin}/api/auth/sso/callback`,
    scope: "openid profile email",
    state: "s".repeat(43),
    nonce: "n".repeat(43),
    code_challenge: CHALLENGE,
    code_challenge_method: "S256",
    ...overrides,
  };
  const url = new URL(`${origin}/oauth2/authorize`);
  for (const [key, value] of Object.entries(params)) {
    if (value !== null) url.searchParams.set(key, value);
  }
  return url.toString();
}

describe("auth config", () => {
  it("accepts a closed SSO deployment whose start URL is this deployment's own", () => {
    expect(
      validateAuthConfig(
        { provider: "oidc", registration_mode: "closed", sso_start_url: `${expected.stagingOrigin}/api/auth/sso/start` },
        expected,
      ),
    ).toEqual({ ssoStartUrl: `${expected.stagingOrigin}/api/auth/sso/start` });
  });

  it.each([
    ["a local provider", { provider: "local", registration_mode: "closed", sso_start_url: null }],
    ["open registration", { provider: "oidc", registration_mode: "open", sso_start_url: `${expected.stagingOrigin}/api/auth/sso/start` }],
    ["an unwired deployment", { provider: "oidc", registration_mode: "closed", sso_start_url: null }],
    ["a start URL on another origin", { provider: "oidc", registration_mode: "closed", sso_start_url: "https://evil.invalid/api/auth/sso/start" }],
    [
      "the old browser-client fields",
      {
        provider: "oidc",
        registration_mode: "closed",
        sso_start_url: `${expected.stagingOrigin}/api/auth/sso/start`,
        oidc_client_id: expected.audience,
        authorization_endpoint: `${expected.authorizationOrigin}/oauth2/authorize`,
      },
    ],
  ])("refuses %s", (_label, config) => {
    expect(() => validateAuthConfig(config, expected)).toThrow("oidc_auth_config");
  });
});

describe("authorization request", () => {
  it("accepts the backend's confidential-client request with S256, state, nonce and the backend callback", () => {
    expect(() => validateAuthorizationRequest(authorizationUrl(), expected)).not.toThrow();
  });

  it.each([
    ["the wrong origin", authorizationUrl({}, "https://evil.invalid")],
    ["an implicit flow", authorizationUrl({ response_type: "token" })],
    ["another client", authorizationUrl({ client_id: "someone-else" })],
    ["the SPA as redirect target", authorizationUrl({ redirect_uri: `${expected.stagingOrigin}/` })],
    ["a redirect to another origin", authorizationUrl({ redirect_uri: "https://evil.invalid/api/auth/sso/callback" })],
    ["plain PKCE", authorizationUrl({ code_challenge_method: "plain" })],
    ["a malformed challenge", authorizationUrl({ code_challenge: "short" })],
    ["no state", authorizationUrl({ state: null })],
    ["no nonce", authorizationUrl({ nonce: null })],
    ["no openid scope", authorizationUrl({ scope: "profile email" })],
    ["a client secret on the query", authorizationUrl({ client_secret: "leaked" })],
    ["a verifier on the query", authorizationUrl({ code_verifier: "leaked" })],
    ["a duplicated client_id", `${authorizationUrl()}&client_id=${expected.audience}`],
  ])("refuses %s with a static error", (_label, url) => {
    expect(() => validateAuthorizationRequest(url, expected)).toThrow("oidc_authorization_request");
    try {
      validateAuthorizationRequest(url, expected);
    } catch (error) {
      expect(String(error)).not.toMatch(/leaked|someone-else|evil/);
    }
  });
});

describe("callback landing", () => {
  it("accepts exactly one handoff code in the fragment on this deployment's SPA", () => {
    expect(() => validateCallbackLanding(`${expected.stagingOrigin}/#/auth/callback?code=${HANDOFF}`, expected)).not.toThrow();
  });

  it.each([
    ["a code in the query", `${expected.stagingOrigin}/?code=${HANDOFF}#/auth/callback?code=${HANDOFF}`],
    ["a token in the fragment", `${expected.stagingOrigin}/#/auth/callback?access_token=leaked`],
    ["a code and a state", `${expected.stagingOrigin}/#/auth/callback?code=${HANDOFF}&state=x`],
    ["two codes", `${expected.stagingOrigin}/#/auth/callback?code=${HANDOFF}&code=${HANDOFF}`],
    ["an error category", `${expected.stagingOrigin}/#/auth/callback?error=sso_idp_error`],
    ["a code outside the alphabet", `${expected.stagingOrigin}/#/auth/callback?code=${HANDOFF}%2F`],
    ["a code beyond the bound", `${expected.stagingOrigin}/#/auth/callback?code=${"h".repeat(129)}`],
    ["another origin", `https://evil.invalid/#/auth/callback?code=${HANDOFF}`],
    ["another path", `${expected.stagingOrigin}/app#/auth/callback?code=${HANDOFF}`],
    ["the old query callback", `${expected.stagingOrigin}/?code=${HANDOFF}&state=x`],
  ])("refuses %s", (_label, url) => {
    expect(() => validateCallbackLanding(url, expected)).toThrow("oidc_callback_landing");
  });
});

describe("session token", () => {
  it("accepts ELSPETH's session token for this deployment issued to an SSO identity, hashing the subject", () => {
    const claims = validateSessionToken(token(sessionClaims()), expected, 1_950_000_000);
    expect(claims.subjectSha256).toMatch(/^[0-9a-f]{64}$/);
    expect(JSON.stringify(claims)).not.toContain("identity-0001");
  });

  it.each([
    ["an IdP-issued token", sessionClaims({ iss: "https://cognito-idp.ap-southeast-2.amazonaws.com/pool" })],
    ["another deployment's audience", sessionClaims({ aud: "https://other.example" })],
    ["the local-only audience", sessionClaims({ aud: "elspeth-local" })],
    ["a local-provider token", sessionClaims({ provider: "local" })],
    ["no provider", sessionClaims({ provider: undefined })],
    ["no subject", sessionClaims({ sub: undefined })],
    ["a blank subject", sessionClaims({ sub: " " })],
    ["no token id", sessionClaims({ jti: undefined })],
    ["an expired token", sessionClaims({ exp: 1_800_000_000 })],
  ])("rejects %s with a static error", (_label, claims) => {
    const sentinel = "credential-sentinel";
    expect(() => validateSessionToken(token({ ...claims, secret: sentinel }), expected, 1_950_000_000)).toThrowError(
      OidcEvidenceError,
    );
    try {
      validateSessionToken(token({ ...claims, secret: sentinel }), expected, 1_950_000_000);
    } catch (error) {
      expect(String(error)).not.toContain(sentinel);
    }
  });

  it.each(["not-a-jwt", `${"a".repeat(20_000)}.e30.signature`, "a.!!!!.c"])(
    "rejects malformed or oversized JWT material",
    (rawToken) => {
      expect(() => validateSessionToken(rawToken, expected, 1_950_000_000)).toThrowError(OidcEvidenceError);
    },
  );
});

describe("OIDC evidence", () => {
  it("builds the exact closed schema for one of four phases", () => {
    const evidence = buildOidcEvidence({
      phase: "candidate-initial",
      timestamp: "2026-07-14T01:02:03Z",
      ...expected,
      subjectSha256: "a".repeat(64),
      authMeStatus: 200,
      sessionCreateStatus: 201,
      sessionReadStatus: 200,
      sessionDeleteStatus: 204,
    });
    expect(Object.keys(evidence).sort()).toEqual(
      [
        "audience",
        "auth_me_status",
        "authorization_origin",
        "phase",
        "session_create_status",
        "session_delete_status",
        "session_read_status",
        "session_round_trip",
        "subject_sha256",
        "timestamp",
        "token_audience",
      ].sort(),
    );
    expect(evidence.session_round_trip).toBe(true);
    expect(evidence.token_audience).toBe(expected.stagingOrigin);
    expect(() => buildOidcEvidence({ ...evidence, phase: "unreviewed" } as never)).toThrow("oidc_evidence_schema");
    expect(() => buildOidcEvidence({ ...evidence, ...expected, phase: "candidate-initial", subjectSha256: "a".repeat(64), authMeStatus: 200, sessionCreateStatus: 201, sessionReadStatus: 200, sessionDeleteStatus: 204, stagingOrigin: "http://plain.invalid" })).toThrow("oidc_evidence_schema");
  });

  it("writes one bounded owner-only file through an owner-only directory", () => {
    const directory = join(tmpdir(), `elspeth-oidc-${process.pid}-${directories.length}`);
    directories.push(directory);
    mkdirSync(directory, { mode: 0o700 });
    const destination = join(directory, "candidate-initial.json");
    const evidence = buildOidcEvidence({
      phase: "candidate-initial",
      timestamp: "2026-07-14T01:02:03Z",
      ...expected,
      subjectSha256: "a".repeat(64),
      authMeStatus: 200,
      sessionCreateStatus: 201,
      sessionReadStatus: 200,
      sessionDeleteStatus: 204,
    });

    writeOidcEvidence(destination, evidence);

    expect(lstatSync(destination).mode & 0o777).toBe(0o600);
    expect(JSON.parse(readFileSync(destination, "utf8"))).toEqual(evidence);
    expect(() => writeOidcEvidence(join(directory, "invalid.json"), { ...evidence, audience: "" })).toThrow(
      "oidc_evidence_schema",
    );
  });

  it("writes a bounded bearer handoff only to a new owner-only file", () => {
    const directory = join(tmpdir(), `elspeth-oidc-handoff-${process.pid}-${directories.length}`);
    directories.push(directory);
    mkdirSync(directory, { mode: 0o700 });
    const destination = join(directory, "access-token");
    const accessToken = token(sessionClaims());

    writeOidcBearerHandoff(destination, accessToken);

    expect(lstatSync(destination).mode & 0o777).toBe(0o600);
    expect(readFileSync(destination, "utf8")).toBe(accessToken);
    expect(() => writeOidcBearerHandoff(destination, accessToken)).toThrow("oidc_bearer_handoff");
  });

  it("rejects symlink, permissive parent, and pre-existing destination attacks", () => {
    const directory = join(tmpdir(), `elspeth-oidc-attacks-${process.pid}-${directories.length}`);
    directories.push(directory);
    mkdirSync(directory, { mode: 0o700 });
    const evidence = buildOidcEvidence({
      phase: "candidate-initial",
      timestamp: "2026-07-14T01:02:03Z",
      ...expected,
      subjectSha256: "a".repeat(64),
      authMeStatus: 200,
      sessionCreateStatus: 201,
      sessionReadStatus: 200,
      sessionDeleteStatus: 204,
    });
    const target = join(directory, "target.json");
    const link = join(directory, "link.json");
    writeFileSync(target, "{}", { mode: 0o600 });
    symlinkSync(target, link);
    expect(() => writeOidcEvidence(link, evidence)).toThrow("oidc_evidence_destination");
    expect(() => writeOidcEvidence(target, evidence)).toThrow("oidc_evidence_destination");
    chmodSync(directory, 0o755);
    expect(() => writeOidcEvidence(join(directory, "new.json"), evidence)).toThrow("oidc_evidence_parent");
  });
});
