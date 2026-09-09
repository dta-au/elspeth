import {
  closeSync,
  constants,
  fsyncSync,
  linkSync,
  lstatSync,
  openSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { createHash, randomUUID } from "node:crypto";
import { basename, dirname, join } from "node:path";

const MAX_JWT_BYTES = 16 * 1024;
const MAX_JWT_PAYLOAD_BYTES = 8 * 1024;
const MAX_EVIDENCE_BYTES = 64 * 1024;
const MAX_URL_BYTES = 16 * 1024;
const SHA256 = /^[0-9a-f]{64}$/;
const BASE64URL = /^[A-Za-z0-9_-]+$/;
/** S256 code challenge: base64url of a 32-byte digest, unpadded. */
const PKCE_CHALLENGE = /^[A-Za-z0-9_-]{43}$/;
/** The handoff code the backend puts in the fragment: token_urlsafe, bounded like SsoCompleteRequest. */
const HANDOFF_CODE = /^[A-Za-z0-9_-]{1,128}$/;
/** The session token's `iss`: web/auth/session_token.py `_ISSUER`. */
const SESSION_TOKEN_ISSUER = "elspeth";

/**
 * What this harness proves, and what it deliberately does not.
 *
 * The backend is the confidential client (spec D2). The browser therefore
 * never sees the IdP's token endpoint, the client secret, or an IdP-minted
 * token; it sees the authorization REQUEST the backend redirects it to, the
 * callback LANDING (`#/auth/callback?code=<handoff>` in the fragment), and
 * the ELSPETH session token that POST /api/auth/sso/complete returns. Every
 * validator here checks one of those three observable things. The IdP
 * issuer is NOT observable from the browser and is not recorded as if it
 * had been verified: the backend pins it, and the backend's own tests prove
 * that pin.
 */

export const OIDC_EVIDENCE_PHASES = [
  "previous-before-candidate",
  "candidate-initial",
  "previous-after-rollback",
  "candidate-after-redeploy",
] as const;

export type OidcEvidencePhase = (typeof OIDC_EVIDENCE_PHASES)[number];

export class OidcEvidenceError extends Error {
  constructor(readonly check: string) {
    super(check);
    this.name = "OidcEvidenceError";
  }
}

interface ExpectedOidc {
  /** The IdP client id, observed on the authorization request. */
  audience: string;
  /** The exact HTTPS origin the authorization request must go to. */
  authorizationOrigin: string;
  /** The deployment under test: the SPA's origin, the callback's origin, and the session token's `aud`. */
  stagingOrigin: string;
}

interface AuthConfigDocument {
  provider?: unknown;
  registration_mode?: unknown;
  sso_start_url?: unknown;
}

export interface ValidatedAuthConfig {
  ssoStartUrl: string;
}

export interface ValidatedSessionToken {
  subjectSha256: string;
}

export interface OidcEvidence {
  phase: OidcEvidencePhase;
  timestamp: string;
  audience: string;
  authorization_origin: string;
  token_audience: string;
  subject_sha256: string;
  auth_me_status: 200;
  session_create_status: 201;
  session_read_status: 200;
  session_delete_status: 204;
  session_round_trip: true;
}

interface BuildOidcEvidenceInput extends ExpectedOidc {
  phase: OidcEvidencePhase;
  timestamp: string;
  subjectSha256: string;
  authMeStatus: number;
  sessionCreateStatus: number;
  sessionReadStatus: number;
  sessionDeleteStatus: number;
}

function exactHttpsOrigin(value: unknown, check: string): string {
  if (typeof value !== "string" || value.length === 0 || value.length > 4096) {
    throw new OidcEvidenceError(check);
  }
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    throw new OidcEvidenceError(check);
  }
  const port = parsed.port === "443" ? "" : parsed.port;
  const canonical = `https://${parsed.hostname.toLowerCase()}${port ? `:${port}` : ""}`;
  if (
    parsed.protocol !== "https:" ||
    parsed.username !== "" ||
    parsed.password !== "" ||
    parsed.pathname !== "/" ||
    parsed.search !== "" ||
    parsed.hash !== "" ||
    value !== canonical
  ) {
    throw new OidcEvidenceError(check);
  }
  return canonical;
}

function parseBoundedUrl(value: unknown, check: string): URL {
  if (typeof value !== "string" || value.length === 0 || Buffer.byteLength(value, "utf8") > MAX_URL_BYTES) {
    throw new OidcEvidenceError(check);
  }
  try {
    return new URL(value);
  } catch {
    throw new OidcEvidenceError(check);
  }
}

/**
 * GET /api/auth/config must say: SSO deployment, closed registration, and a
 * start URL that is THIS deployment's /api/auth/sso/start — not an IdP URL,
 * not another origin. A config that still published IdP endpoints would be
 * the old browser-client path, which is exactly what this harness must not
 * accept as evidence.
 */
export function validateAuthConfig(config: AuthConfigDocument, expected: Pick<ExpectedOidc, "stagingOrigin">): ValidatedAuthConfig {
  const stagingOrigin = exactHttpsOrigin(expected.stagingOrigin, "oidc_staging_origin");
  if (
    config.provider !== "oidc" ||
    config.registration_mode !== "closed" ||
    Object.keys(config).sort().join(",") !== "provider,registration_mode,sso_start_url" ||
    config.sso_start_url !== `${stagingOrigin}/api/auth/sso/start`
  ) {
    throw new OidcEvidenceError("oidc_auth_config");
  }
  return { ssoStartUrl: config.sso_start_url };
}

/**
 * The authorization request the backend redirected the browser to. It must
 * carry the confidential client's id, name the BACKEND callback as the
 * redirect target, use S256 PKCE, bind a state and a nonce, and carry no
 * secret or verifier. The values of state, nonce and challenge are not
 * returned: the harness has no use for them and must not leak them.
 */
export function validateAuthorizationRequest(url: unknown, expected: ExpectedOidc): void {
  const authorizationOrigin = exactHttpsOrigin(expected.authorizationOrigin, "oidc_expected_origin");
  const stagingOrigin = exactHttpsOrigin(expected.stagingOrigin, "oidc_staging_origin");
  if (typeof expected.audience !== "string" || !expected.audience) {
    throw new OidcEvidenceError("oidc_expected_audience");
  }
  const parsed = parseBoundedUrl(url, "oidc_authorization_request");
  const params = parsed.searchParams;
  const single = (name: string): string | null => {
    const values = params.getAll(name);
    return values.length === 1 ? values[0] : null;
  };
  if (
    parsed.origin !== authorizationOrigin ||
    parsed.username !== "" ||
    parsed.password !== "" ||
    parsed.hash !== "" ||
    single("response_type") !== "code" ||
    single("client_id") !== expected.audience ||
    single("redirect_uri") !== `${stagingOrigin}/api/auth/sso/callback` ||
    single("code_challenge_method") !== "S256" ||
    !PKCE_CHALLENGE.test(single("code_challenge") ?? "") ||
    !(single("state") ?? "") ||
    !(single("nonce") ?? "") ||
    !(single("scope") ?? "").split(" ").includes("openid") ||
    params.has("client_secret") ||
    params.has("code_verifier") ||
    params.has("token") ||
    params.has("access_token") ||
    params.has("id_token")
  ) {
    throw new OidcEvidenceError("oidc_authorization_request");
  }
}

/**
 * Where the backend's callback sends the browser: this deployment's SPA,
 * with exactly one handoff code in the FRAGMENT and nothing in the query.
 * A token anywhere in the URL, a code in the query, or a second parameter
 * is a different (older, or broken) flow and is refused as evidence.
 */
export function validateCallbackLanding(url: unknown, expected: Pick<ExpectedOidc, "stagingOrigin">): void {
  const stagingOrigin = exactHttpsOrigin(expected.stagingOrigin, "oidc_staging_origin");
  const parsed = parseBoundedUrl(url, "oidc_callback_landing");
  const prefix = "#/auth/callback?";
  if (
    parsed.origin !== stagingOrigin ||
    parsed.pathname !== "/" ||
    parsed.search !== "" ||
    !parsed.hash.startsWith(prefix)
  ) {
    throw new OidcEvidenceError("oidc_callback_landing");
  }
  const fragment = new URLSearchParams(parsed.hash.slice(prefix.length));
  const keys = [...fragment.keys()];
  const codes = fragment.getAll("code");
  if (keys.length !== 1 || codes.length !== 1 || !HANDOFF_CODE.test(codes[0])) {
    throw new OidcEvidenceError("oidc_callback_landing");
  }
}

function decodeClaims(rawToken: string): Record<string, unknown> {
  if (Buffer.byteLength(rawToken, "utf8") > MAX_JWT_BYTES) {
    throw new OidcEvidenceError("oidc_token_format");
  }
  const segments = rawToken.split(".");
  if (segments.length !== 3 || segments.some((segment) => !BASE64URL.test(segment))) {
    throw new OidcEvidenceError("oidc_token_format");
  }
  let decoded: Buffer;
  try {
    decoded = Buffer.from(segments[1], "base64url");
  } catch {
    throw new OidcEvidenceError("oidc_token_format");
  }
  if (decoded.length === 0 || decoded.length > MAX_JWT_PAYLOAD_BYTES) {
    throw new OidcEvidenceError("oidc_token_format");
  }
  let claims: unknown;
  try {
    claims = JSON.parse(decoded.toString("utf8"));
  } catch {
    throw new OidcEvidenceError("oidc_token_format");
  }
  if (claims === null || typeof claims !== "object" || Array.isArray(claims)) {
    throw new OidcEvidenceError("oidc_token_format");
  }
  return claims as Record<string, unknown>;
}

/**
 * The token that lands in localStorage is ELSPETH's session token, minted by
 * POST /api/auth/sso/complete — never the IdP's. Its envelope is
 * web/auth/session_token.py's: iss "elspeth", aud = this deployment's
 * public_base_url, provider "oidc", sub = the identity id, plus jti and exp.
 * The signature is not checked here (the key is the backend's); what the
 * harness proves is that the browser holds an ELSPETH token for THIS
 * deployment, issued to an SSO identity, and that the API honours it.
 */
export function validateSessionToken(
  rawToken: string,
  expected: Pick<ExpectedOidc, "stagingOrigin">,
  nowEpochSeconds = Math.floor(Date.now() / 1000),
): ValidatedSessionToken {
  const stagingOrigin = exactHttpsOrigin(expected.stagingOrigin, "oidc_staging_origin");
  const claims = decodeClaims(rawToken);
  const subject = claims.sub;
  const expiration = claims.exp;
  const tokenId = claims.jti;
  if (
    claims.iss !== SESSION_TOKEN_ISSUER ||
    claims.aud !== stagingOrigin ||
    claims.provider !== "oidc" ||
    typeof subject !== "string" ||
    subject.trim() === "" ||
    Buffer.byteLength(subject, "utf8") > 1024 ||
    typeof tokenId !== "string" ||
    tokenId === "" ||
    typeof expiration !== "number" ||
    !Number.isSafeInteger(expiration) ||
    expiration <= nowEpochSeconds
  ) {
    throw new OidcEvidenceError("oidc_token_claims");
  }
  return { subjectSha256: createHash("sha256").update(subject).digest("hex") };
}

function isIsoUtc(value: string): boolean {
  return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/.test(value) && Number.isFinite(Date.parse(value));
}

export function buildOidcEvidence(input: BuildOidcEvidenceInput): OidcEvidence {
  if (
    !OIDC_EVIDENCE_PHASES.includes(input.phase) ||
    !isIsoUtc(input.timestamp) ||
    typeof input.audience !== "string" ||
    !input.audience ||
    !SHA256.test(input.subjectSha256) ||
    input.authMeStatus !== 200 ||
    input.sessionCreateStatus !== 201 ||
    input.sessionReadStatus !== 200 ||
    input.sessionDeleteStatus !== 204
  ) {
    throw new OidcEvidenceError("oidc_evidence_schema");
  }
  const authorizationOrigin = exactHttpsOrigin(input.authorizationOrigin, "oidc_evidence_schema");
  const tokenAudience = exactHttpsOrigin(input.stagingOrigin, "oidc_evidence_schema");
  return {
    phase: input.phase,
    timestamp: input.timestamp,
    audience: input.audience,
    authorization_origin: authorizationOrigin,
    token_audience: tokenAudience,
    subject_sha256: input.subjectSha256,
    auth_me_status: 200,
    session_create_status: 201,
    session_read_status: 200,
    session_delete_status: 204,
    session_round_trip: true,
  };
}

function ownerUid(): number {
  if (typeof process.getuid !== "function") {
    throw new OidcEvidenceError("oidc_evidence_owner");
  }
  return process.getuid();
}

export function writeOidcEvidence(destination: string, evidence: OidcEvidence): void {
  const validated = buildOidcEvidence({
    phase: evidence.phase,
    timestamp: evidence.timestamp,
    authorizationOrigin: evidence.authorization_origin,
    stagingOrigin: evidence.token_audience,
    audience: evidence.audience,
    subjectSha256: evidence.subject_sha256,
    authMeStatus: evidence.auth_me_status,
    sessionCreateStatus: evidence.session_create_status,
    sessionReadStatus: evidence.session_read_status,
    sessionDeleteStatus: evidence.session_delete_status,
  });
  if (Object.keys(evidence).sort().join(",") !== Object.keys(validated).sort().join(",") || evidence.session_round_trip !== true) {
    throw new OidcEvidenceError("oidc_evidence_schema");
  }
  const content = `${JSON.stringify(validated)}\n`;
  writeOwnerOnlyFile(destination, content, {
    maxBytes: MAX_EVIDENCE_BYTES,
    parentCheck: "oidc_evidence_parent",
    destinationCheck: "oidc_evidence_destination",
    sizeCheck: "oidc_evidence_size",
    writeCheck: "oidc_evidence_write",
  });
}

interface OwnerOnlyWriteChecks {
  maxBytes: number;
  parentCheck: string;
  destinationCheck: string;
  sizeCheck: string;
  writeCheck: string;
}

function writeOwnerOnlyFile(destination: string, content: string, checks: OwnerOnlyWriteChecks): void {
  const parent = dirname(destination);
  let parentStat;
  try {
    parentStat = lstatSync(parent);
  } catch {
    throw new OidcEvidenceError(checks.parentCheck);
  }
  if (!parentStat.isDirectory() || parentStat.isSymbolicLink() || parentStat.uid !== ownerUid() || (parentStat.mode & 0o077) !== 0) {
    throw new OidcEvidenceError(checks.parentCheck);
  }
  try {
    lstatSync(destination);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
      throw new OidcEvidenceError(checks.destinationCheck);
    }
  }
  try {
    lstatSync(destination);
    throw new OidcEvidenceError(checks.destinationCheck);
  } catch (error) {
    if (error instanceof OidcEvidenceError) throw error;
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
      throw new OidcEvidenceError(checks.destinationCheck);
    }
  }
  if (Buffer.byteLength(content, "utf8") > checks.maxBytes) {
    throw new OidcEvidenceError(checks.sizeCheck);
  }
  const temporary = join(parent, `.${basename(destination)}.${process.pid}.${randomUUID()}.tmp`);
  let fileDescriptor: number | null = null;
  try {
    fileDescriptor = openSync(
      temporary,
      constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW,
      0o600,
    );
    writeFileSync(fileDescriptor, content, { encoding: "utf8" });
    fsyncSync(fileDescriptor);
    closeSync(fileDescriptor);
    fileDescriptor = null;
    linkSync(temporary, destination);
    unlinkSync(temporary);
    const directoryDescriptor = openSync(parent, constants.O_RDONLY | constants.O_DIRECTORY);
    try {
      fsyncSync(directoryDescriptor);
    } finally {
      closeSync(directoryDescriptor);
    }
  } catch {
    if (fileDescriptor !== null) {
      try {
        closeSync(fileDescriptor);
      } catch {
        // Preserve the static outer failure.
      }
    }
    try {
      unlinkSync(temporary);
    } catch {
      // The file may already have been atomically renamed.
    }
    throw new OidcEvidenceError(checks.writeCheck);
  }
}

export function writeOidcBearerHandoff(destination: string, accessToken: string): void {
  const segments = accessToken.split(".");
  if (
    accessToken.length === 0 ||
    Buffer.byteLength(accessToken, "utf8") > MAX_JWT_BYTES ||
    segments.length !== 3 ||
    segments.some((segment) => !BASE64URL.test(segment))
  ) {
    throw new OidcEvidenceError("oidc_bearer_handoff");
  }
  writeOwnerOnlyFile(destination, accessToken, {
    maxBytes: MAX_JWT_BYTES,
    parentCheck: "oidc_bearer_handoff",
    destinationCheck: "oidc_bearer_handoff",
    sizeCheck: "oidc_bearer_handoff",
    writeCheck: "oidc_bearer_handoff",
  });
}
