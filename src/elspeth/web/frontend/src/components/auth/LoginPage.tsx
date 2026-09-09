import { useState, useEffect, type FormEvent } from "react";
import { useAuth } from "../../hooks/useAuth";
import * as api from "../../api/client";
import type { ApiError, AuthConfig } from "../../types/index";
import { Button, Input, AlertBanner, WordMark } from "../ui";
import { captureSsoCallback, ssoFailureMessage, type SsoCallbackOutcome } from "./ssoCallback";

/**
 * Login page that adapts to the configured auth provider.
 *
 * Fetches GET /api/auth/config on mount to determine provider type:
 * - "local": renders a username/password form; when the backend's
 *   registration_mode is "open" or "email_verified", also offers a
 *   "Create an account" view. Open registration auto-logs the account in;
 *   email-verified registration waits for the verification link.
 * - any SSO provider: renders a "Sign in with SSO" button that navigates
 *   to config.sso_start_url — the BACKEND's /api/auth/sso/start. The
 *   backend is the confidential client: it builds the authorization
 *   request, holds the transaction in a sealed cookie, exchanges the code
 *   itself, and sends the browser back to the `#/auth/callback` hash route
 *   with a single-use handoff code in the fragment (never a token, never
 *   in the query). This page exchanges that code for the session token
 *   with POST /api/auth/sso/complete. There is no browser-side PKCE, no
 *   token endpoint in the SPA, and nothing here ever holds a client id.
 *
 * The callback is captured and scrubbed from the address bar synchronously
 * during the first render, before any effect can start network IO. Email
 * verification remains a separate local-auth path on the query string.
 *
 * Failed sign-in attempts keep the username and clear only the
 * password (WCAG 3.3.7 Redundant Entry, elspeth-d49f8ad511); the
 * error banner is programmatically associated with the credential
 * fields via aria-invalid + aria-describedby.
 */

/** id linking the sign-in error banner to the credential fields. */
const LOGIN_ERROR_ID = "login-error";
/** id linking the registration error banner to its targeted fields. */
const REGISTER_ERROR_ID = "register-error";
const VERIFY_TOKEN_MAX_BYTES = 16 * 1024;

type CallbackCapture =
  | { kind: "verify"; token: string | null; started: boolean }
  | { kind: "sso"; outcome: SsoCallbackOutcome; started: boolean };

// React StrictMode constructs the component twice. The callback must be
// scrubbed during the first render, while the second render must receive the
// same bounded in-memory capture rather than rereading the URL.
let pendingCallbackCapture: CallbackCapture | null = null;

function captureCallback(): CallbackCapture | null {
  if (pendingCallbackCapture !== null && !pendingCallbackCapture.started) {
    return pendingCallbackCapture;
  }

  const sso = captureSsoCallback();
  if (sso !== null) {
    const capture: CallbackCapture = { kind: "sso", outcome: sso, started: false };
    pendingCallbackCapture = capture;
    return capture;
  }

  const params = new URLSearchParams(window.location.search);
  if (!params.has("verify_token")) return null;
  const verificationTokens = params.getAll("verify_token");
  const token =
    verificationTokens.length === 1 &&
    verificationTokens[0].length > 0 &&
    new TextEncoder().encode(verificationTokens[0]).byteLength <= VERIFY_TOKEN_MAX_BYTES
      ? verificationTokens[0]
      : null;
  window.history.replaceState(null, "", window.location.pathname + window.location.hash);
  const capture: CallbackCapture = { kind: "verify", token, started: false };
  pendingCallbackCapture = capture;
  return capture;
}

/** Which registration fields the current registration error is about. */
interface RegisterErrorTargets {
  username: boolean;
  email: boolean;
  password: boolean;
  confirm: boolean;
}

const NO_REGISTER_TARGETS: RegisterErrorTargets = {
  username: false,
  email: false,
  password: false,
  confirm: false,
};

export function LoginPage() {
  const [callbackCapture] = useState(captureCallback);
  const { login, loginWithToken, loginError } = useAuth();
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null);
  const [configLoading, setConfigLoading] = useState(true);
  const [view, setView] = useState<"signin" | "register">("signin");
  const [registerError, setRegisterError] = useState<string | null>(null);
  const [registerNotice, setRegisterNotice] = useState<string | null>(null);
  const [verificationError, setVerificationError] = useState<string | null>(
    null,
  );
  const [registerErrorTargets, setRegisterErrorTargets] =
    useState<RegisterErrorTargets>(NO_REGISTER_TARGETS);

  // Fetch auth config on mount to determine which login form to show
  useEffect(() => {
    api
      .fetchAuthConfig()
      .then((config) => {
        setAuthConfig(config);
        setConfigLoading(false);
      })
      .catch(() => {
        // If config fetch fails, fall back to local auth. Registration is
        // treated as closed — we don't know the effective mode, so we don't
        // advertise an affordance that may 404.
        setAuthConfig({
          provider: "local",
          registration_mode: "closed",
          sso_start_url: null,
        });
        setConfigLoading(false);
      });
  }, []);

  // Process the already-consumed callback. The capture happens synchronously
  // during render, before this or the config-fetch effect can start network IO.
  // Neither branch waits for the auth config: the handoff code is complete in
  // itself, and the backend refuses it on its own authority.
  useEffect(() => {
    if (callbackCapture === null || callbackCapture.started) return;
    callbackCapture.started = true;
    const finish = () => {
      pendingCallbackCapture = null;
    };

    if (callbackCapture.kind === "verify") {
      if (callbackCapture.token === null) {
        setVerificationError("Email verification failed. Please request a new link.");
        finish();
        return;
      }
      api
        .verifyEmail(callbackCapture.token)
        .then(({ access_token }) => loginWithToken(access_token))
        .catch(() => {
          setVerificationError("Email verification failed. Please request a new link.");
        })
        .finally(finish);
      return;
    }

    const { outcome } = callbackCapture;
    if (outcome.kind !== "code") {
      setVerificationError(ssoFailureMessage(outcome));
      finish();
      return;
    }
    api
      .completeSsoLogin(outcome.code)
      .then(({ access_token }) => loginWithToken(access_token))
      .catch(() => {
        setVerificationError("Single sign-on failed. Please try again.");
      })
      .finally(finish);
  }, [callbackCapture, loginWithToken]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!username || !password) return;

    setIsSubmitting(true);
    setVerificationError(null);
    const succeeded = await login(username, password);
    if (!succeeded) {
      // Keep the username (WCAG 3.3.7 Redundant Entry) — only the
      // rejected password is cleared, per convention.
      setPassword("");
    }
    setIsSubmitting(false);
  }

  async function handleRegister(e: FormEvent) {
    e.preventDefault();
    const emailVerificationRequired =
      authConfig?.registration_mode === "email_verified";
    const trimmedEmail = email.trim();
    if (
      !username ||
      !password ||
      !confirmPassword ||
      (emailVerificationRequired && !trimmedEmail)
    ) {
      return;
    }

    if (password !== confirmPassword) {
      setRegisterError("Passwords do not match.");
      setRegisterErrorTargets({
        username: false,
        email: false,
        password: true,
        confirm: true,
      });
      return;
    }

    setIsSubmitting(true);
    setRegisterError(null);
    setRegisterNotice(null);
    setVerificationError(null);
    setRegisterErrorTargets(NO_REGISTER_TARGETS);
    try {
      const result = emailVerificationRequired
        ? await api.register(username, password, trimmedEmail)
        : await api.register(username, password);
      if ("access_token" in result) {
        // The backend auto-logs open-registration accounts in; adopting the
        // returned token drops the user straight into the app.
        await loginWithToken(result.access_token);
      } else {
        setRegisterNotice(`Check ${result.email} for the verification link.`);
        setPassword("");
        setConfirmPassword("");
      }
    } catch (err) {
      const apiErr = err as ApiError;
      if (apiErr.status === 409) {
        setRegisterError("That username is not available.");
        setRegisterErrorTargets({
          username: true,
          email: false,
          password: false,
          confirm: false,
        });
      } else if (apiErr.status === 422 && emailVerificationRequired) {
        setRegisterError("Enter an email address to verify this account.");
        setRegisterErrorTargets({
          username: false,
          email: true,
          password: false,
          confirm: false,
        });
      } else {
        setRegisterError("Registration failed. Please try again.");
        setRegisterErrorTargets(NO_REGISTER_TARGETS);
      }
    } finally {
      setIsSubmitting(false);
    }
  }

  function switchView(next: "signin" | "register") {
    setView(next);
    // Keep the username across the switch (it's the common field);
    // passwords and stale errors don't carry over.
    setEmail("");
    setPassword("");
    setConfirmPassword("");
    setRegisterError(null);
    setRegisterNotice(null);
    setVerificationError(null);
    setRegisterErrorTargets(NO_REGISTER_TARGETS);
  }

  function handleSsoRedirect() {
    if (authConfig?.sso_start_url == null) return;
    // A real navigation, not fetch: the backend answers with a 302 to the
    // IdP and sets the transaction cookie on the way. rel=noreferrer keeps
    // this page's URL out of the IdP's logs.
    const anchor = document.createElement("a");
    anchor.href = authConfig.sso_start_url;
    anchor.rel = "noreferrer";
    anchor.click();
  }

  if (configLoading) {
    return (
      <div
        role="status"
        aria-label="Loading authentication configuration"
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          // Dynamic viewport unit, matching .app-root (header.css:12-17):
          // 100vh is the static large viewport, so on mobile the browser
          // chrome overlays the bottom of the box (elspeth-340f5d104c).
          height: "100dvh",
        }}
      >
        {/* Page-scale spinner (elspeth-b2a677d661): matches AuthGuard's boot
            frame — the bare .spinner is the 14px button-scale affordance and
            is far too small to anchor a full viewport. */}
        <span className="spinner spinner-page" aria-hidden="true" />
      </div>
    );
  }

  // Any provider that is not local signs in through the backend's SSO walk.
  const isSso = authConfig !== null && authConfig.provider !== "local";
  // Wired means the backend published a start URL; the same condition that
  // makes its /sso/* routes refuse hides the button, rather than a guess.
  const ssoWired = isSso && authConfig.sso_start_url !== null;
  // Registration is a local-auth capability; email_verified creates a
  // pending account and completes via the emailed verification link.
  const registrationAvailable =
    authConfig?.provider === "local" &&
    (authConfig?.registration_mode === "open" ||
      authConfig?.registration_mode === "email_verified");
  const emailVerificationRequired =
    authConfig?.registration_mode === "email_verified";
  const showRegister = registrationAvailable && view === "register";

  return (
    <div
      data-testid="login-page"
      style={{
        display: "flex",
        // flex-start, NOT center (elspeth-340f5d104c). A centred flex item
        // that outgrows its line overflows SYMMETRICALLY, so scrollTop: 0
        // lands below the card's top edge and the heading stays unreachable.
        // The card's own `margin: auto` still centres it whenever there IS
        // free space, and resolves to 0 when there is not — so the card is
        // centred on a tall viewport and top-anchored on a short one.
        alignItems: "flex-start",
        justifyContent: "center",
        // A BOUNDED height is what makes this element a scroll owner. The
        // filed remediation asked for `min-height: 100dvh; overflow-y: auto`,
        // but min-height alone leaves the box content-sized: it simply grows
        // past the viewport, never overflows itself, and `body { overflow:
        // hidden }` (base.css:14-19) then clips it with no scrollbar — the
        // exact shipping state where the submit button was unreachable. The
        // dynamic unit (not 100vh) keeps mobile browser chrome out of the box.
        height: "100dvh",
        overflowY: "auto",
        padding: "var(--space-lg)",
        backgroundColor: "var(--color-bg)",
      }}
    >
      <div
        data-testid="login-card"
        style={{
          width: "360px",
          maxWidth: "100%",
          margin: "auto",
          padding: "var(--space-2xl)",
          backgroundColor: "var(--color-surface)",
          // The card's edge is drawn by a BORDER, not by its shadow — the
          // house rule the bespoke `0 2px 8px rgba(10,40,50,0.4)` recipe
          // stood in for. That literal was also a fourth shadow outside the
          // sanctioned three, and being inline it could not be overridden by
          // [data-theme="light"], so the light card wore a dark teal halo.
          // --shadow-modal is theme-paired (tokens.css:241 / :464).
          border: "1px solid var(--color-border)",
          borderRadius: "var(--radius-lg)",
          boxShadow: "var(--shadow-modal)",
        }}
      >
        <div style={{ textAlign: "center", marginBottom: "var(--space-xl)" }}>
          {/* The brand mark is the canonical <WordMark> (mono/uppercase/
              tracked). The positioning line below states what ELSPETH is —
              derived from the product's own "auditable outputs" thesis, in the
              public-service register. Copy is operator/UX-tunable. */}
          <WordMark as="h1" size={22} style={{ margin: 0 }} />
          <p
            style={{
              margin: "var(--space-sm) 0 0",
              fontSize: "var(--font-size-sm)",
              color: "var(--color-text-secondary)",
            }}
          >
            Build and run auditable data pipelines.
          </p>
        </div>

        {isSso ? (
          <>
            {loginError && <AlertBanner tone="error">{loginError}</AlertBanner>}
            {verificationError && (
              <AlertBanner tone="error">{verificationError}</AlertBanner>
            )}
            {ssoWired ? (
              <Button
                variant="primary"
                type="button"
                onClick={handleSsoRedirect}
                aria-label="Sign in with single sign-on"
              >
                Sign in with SSO
              </Button>
            ) : (
              <AlertBanner tone="error">
                Single sign-on is not configured on this deployment.
              </AlertBanner>
            )}
          </>
        ) : showRegister ? (
          /* Local auth: registration form */
          <form
            onSubmit={handleRegister}
            aria-label="Create an account"
            style={{ display: "flex", flexDirection: "column", gap: "var(--space-md)" }}
          >
            <h2
              style={{
                margin: 0,
                fontSize: "var(--font-size-md)",
                fontWeight: 600,
              }}
            >
              Create an account
            </h2>

            {registerError && (
              <AlertBanner tone="error" id={REGISTER_ERROR_ID}>
                {registerError}
              </AlertBanner>
            )}
            {registerNotice && (
              <AlertBanner tone="info">{registerNotice}</AlertBanner>
            )}

            <Input
              label="Username"
              id="register-username"
              type="text"
              autoComplete="username"
              required
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              aria-invalid={registerErrorTargets.username ? true : undefined}
              aria-describedby={
                registerErrorTargets.username ? REGISTER_ERROR_ID : undefined
              }
            />

            {emailVerificationRequired && (
              <Input
                label="Email"
                id="register-email"
                type="email"
                autoComplete="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                aria-invalid={registerErrorTargets.email ? true : undefined}
                aria-describedby={
                  registerErrorTargets.email ? REGISTER_ERROR_ID : undefined
                }
              />
            )}

            <Input
              label="Password"
              id="register-password"
              type="password"
              autoComplete="new-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              aria-invalid={registerErrorTargets.password ? true : undefined}
              aria-describedby={
                registerErrorTargets.password ? REGISTER_ERROR_ID : undefined
              }
            />

            <Input
              label="Confirm password"
              id="register-confirm-password"
              type="password"
              autoComplete="new-password"
              required
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              aria-invalid={registerErrorTargets.confirm ? true : undefined}
              aria-describedby={
                registerErrorTargets.confirm ? REGISTER_ERROR_ID : undefined
              }
            />

            <Button
              variant="primary"
              type="submit"
              disabled={isSubmitting}
              aria-busy={isSubmitting}
              aria-label={isSubmitting ? "Creating account" : "Create account"}
            >
              {isSubmitting ? (
                <>
                  <span className="spinner" aria-hidden="true" />
                  Creating account…
                </>
              ) : (
                "Create account"
              )}
            </Button>

            <p
              style={{
                margin: 0,
                fontSize: "var(--font-size-sm)",
                color: "var(--color-text-secondary)",
                textAlign: "center",
              }}
            >
              Already have an account?{" "}
              {/* Inside a <form>: the explicit type="button" is the submit-
                  hazard decision — this link must never submit the form. */}
              <Button
                variant="bare"
                type="button"
                className="link-button"
                onClick={() => switchView("signin")}
              >
                Sign in
              </Button>
            </p>
          </form>
        ) : (
          /* Local auth: username/password form */
          <>
            {loginError && (
              <AlertBanner tone="error" id={LOGIN_ERROR_ID}>
                {loginError}
              </AlertBanner>
            )}
            {verificationError && (
              <AlertBanner tone="error">{verificationError}</AlertBanner>
            )}
            <form
              onSubmit={handleSubmit}
              style={{ display: "flex", flexDirection: "column", gap: "var(--space-md)" }}
            >
              {/* The sign-in error is deliberately generic (it never says which
                  field was wrong), so on failure BOTH credential fields are
                  flagged and described by the banner — same idiom as
                  SecretsPanel's form-error wiring. */}
              <Input
                label="Username"
                id="login-username"
                type="text"
                autoComplete="username"
                required
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                aria-invalid={loginError ? true : undefined}
                aria-describedby={loginError ? LOGIN_ERROR_ID : undefined}
              />

              <Input
                label="Password"
                id="login-password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                aria-invalid={loginError ? true : undefined}
                aria-describedby={loginError ? LOGIN_ERROR_ID : undefined}
              />

              {/* Progress cue INSIDE the button, matching
                  AcknowledgementCard.tsx:302-306 and ExecuteButton.tsx:610-618
                  (elspeth-dcb29d06ba). `.btn:disabled` (0,2,0) outranks
                  `.btn-primary` (0,1,0), so at the moment of commitment the
                  primary adopts the disabled wash; without a spinner that
                  reads as "unavailable" rather than "in flight". The spinner
                  is aria-hidden and the state is carried programmatically by
                  aria-busy + the aria-label flip — this screen must not mint a
                  second live region (the loading branch above owns the only
                  role="status" here; LoginPage.test.tsx:328-340 pins that). */}
              <Button
                variant="primary"
                type="submit"
                disabled={isSubmitting}
                aria-busy={isSubmitting}
                aria-label={isSubmitting ? "Signing in" : "Sign in"}
              >
                {isSubmitting ? (
                  <>
                    <span className="spinner" aria-hidden="true" />
                    Signing in…
                  </>
                ) : (
                  "Sign in"
                )}
              </Button>

              {registrationAvailable && (
                <p
                  style={{
                    margin: 0,
                    fontSize: "var(--font-size-sm)",
                    color: "var(--color-text-secondary)",
                    textAlign: "center",
                  }}
                >
                  New to ELSPETH?{" "}
                  {/* Inside a <form>: explicit type="button" — must never
                      submit the sign-in form. */}
                  <Button
                    variant="bare"
                    type="button"
                    className="link-button"
                    onClick={() => switchView("register")}
                  >
                    Create an account
                  </Button>
                </p>
              )}
            </form>
          </>
        )}
      </div>
    </div>
  );
}
