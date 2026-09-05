import { StrictMode } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LoginPage } from "./LoginPage";
import { AuthGuard } from "../common/AuthGuard";
import * as api from "../../api/client";
import { useAuthStore } from "../../stores/authStore";
import { resetStore } from "../../test/store-helpers";
import type { AuthConfig } from "../../types/index";

// The real authStore + useAuth drive these tests (the field-wipe bug lived
// in the store/AuthGuard seam, so mocking the hook would test nothing);
// only the HTTP layer is mocked.
vi.mock("../../api/client", () => ({
  fetchAuthConfig: vi.fn(),
  login: vi.fn(),
  register: vi.fn(),
  verifyEmail: vi.fn(),
  completeSsoLogin: vi.fn(),
  fetchCurrentUser: vi.fn(),
  fetchUserComposerPreferences: vi.fn(),
  updateUserComposerPreferences: vi.fn(),
}));

const SSO_START_URL = "https://elspeth.example.gov.au/api/auth/sso/start";
const HANDOFF_CODE = "H".repeat(43);

function localConfig(
  mode: AuthConfig["registration_mode"] = "open",
): AuthConfig {
  return {
    provider: "local",
    registration_mode: mode,
    sso_start_url: null,
  };
}

function ssoConfig(
  provider: Exclude<AuthConfig["provider"], "local"> = "oidc",
  ssoStartUrl: string | null = SSO_START_URL,
): AuthConfig {
  return {
    provider,
    registration_mode: "closed",
    sso_start_url: ssoStartUrl,
  };
}

function signedInUser() {
  return {
    user_id: "sso-user",
    username: "sso-user",
    display_name: null,
    email: null,
    groups: [],
    dev_admin: false,
  };
}

async function failOneSignIn(user: ReturnType<typeof userEvent.setup>) {
  vi.mocked(api.login).mockRejectedValue({
    status: 401,
    detail: "Invalid credentials",
  });
  await user.type(await screen.findByLabelText("Username"), "alice");
  await user.type(screen.getByLabelText("Password"), "wrong-password");
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  return screen.findByRole("alert");
}

describe("LoginPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.history.replaceState(null, "", "/");
    sessionStorage.clear();
    localStorage.clear();
    resetStore(useAuthStore);
    // Simulate a completed boot (loadFromStorage resolved, no stored token).
    useAuthStore.setState({ isLoading: false });
    vi.mocked(api.fetchAuthConfig).mockReturnValue(new Promise(() => {}));
    vi.stubGlobal("fetch", vi.fn());
  });

  it("preserves a shared inspection route before the login page inspects callback data", async () => {
    window.location.hash = "#/shared/tk-abc";
    vi.mocked(api.fetchAuthConfig).mockResolvedValue(localConfig());

    render(
      <AuthGuard>
        <div>protected</div>
      </AuthGuard>,
    );

    await screen.findByLabelText("Username");
    expect(window.location.hash).toBe("#/shared/tk-abc");
    expect(sessionStorage.getItem("elspeth_post_login_redirect")).toBe(
      "#/shared/tk-abc",
    );
  });

  describe("SSO handoff", () => {
    it.each(["oidc", "entra", "vanguard", "google"] as const)(
      "sends the browser to the backend's start URL for %s, with nothing of the IdP in the page",
      async (provider) => {
        vi.mocked(api.fetchAuthConfig).mockResolvedValue(ssoConfig(provider));
        let navigatedTo = "";
        const click = vi
          .spyOn(HTMLAnchorElement.prototype, "click")
          .mockImplementation(function (this: HTMLAnchorElement) {
            navigatedTo = this.href;
          });
        const user = userEvent.setup();
        render(<LoginPage />);

        await user.click(await screen.findByRole("button", { name: /single sign-on/i }));

        expect(click).toHaveBeenCalledTimes(1);
        expect(navigatedTo).toBe(SSO_START_URL);
        // No browser-side transaction: the backend holds it in a sealed cookie.
        expect(sessionStorage.length).toBe(0);
        expect(fetch).not.toHaveBeenCalled();
        expect(screen.queryByLabelText("Username")).toBeNull();
      },
    );

    it("shows that SSO is not configured instead of a button when the backend publishes no start URL", async () => {
      vi.mocked(api.fetchAuthConfig).mockResolvedValue(ssoConfig("oidc", null));
      render(<LoginPage />);
      expect(await screen.findByRole("alert")).toHaveTextContent("not configured");
      expect(screen.queryByRole("button", { name: /single sign-on/i })).toBeNull();
      expect(screen.queryByLabelText("Username")).toBeNull();
    });

    it("scrubs the fragment before any network call and exchanges the code exactly once in StrictMode", async () => {
      window.history.replaceState(null, "", `/#/auth/callback?code=${HANDOFF_CODE}`);
      vi.mocked(api.fetchAuthConfig).mockResolvedValue(ssoConfig());
      vi.mocked(api.fetchCurrentUser).mockResolvedValue(signedInUser());
      let hashWhenCompleteWasCalled: string | null = null;
      vi.mocked(api.completeSsoLogin).mockImplementation(async () => {
        hashWhenCompleteWasCalled = window.location.hash;
        return { access_token: "session-token", token_type: "bearer" };
      });

      render(
        <StrictMode>
          <LoginPage />
        </StrictMode>,
      );

      expect(window.location.hash).toBe("");
      await waitFor(() => expect(useAuthStore.getState().token).toBe("session-token"));
      expect(api.completeSsoLogin).toHaveBeenCalledTimes(1);
      expect(api.completeSsoLogin).toHaveBeenCalledWith(HANDOFF_CODE);
      expect(hashWhenCompleteWasCalled).toBe("");
      expect(localStorage.getItem("auth_token")).toBe("session-token");
      expect(fetch).not.toHaveBeenCalled();
    });

    it("does not wait for the auth config to exchange the code", async () => {
      window.history.replaceState(null, "", `/#/auth/callback?code=${HANDOFF_CODE}`);
      // fetchAuthConfig never resolves (the beforeEach default).
      vi.mocked(api.fetchCurrentUser).mockResolvedValue(signedInUser());
      vi.mocked(api.completeSsoLogin).mockResolvedValue({ access_token: "session-token" });
      render(<LoginPage />);
      await waitFor(() => expect(useAuthStore.getState().token).toBe("session-token"));
    });

    it.each([
      ["sso_access_pending", /awaiting approval/],
      ["sso_identity_disabled", /disabled/],
      ["provider_unavailable", /unavailable/],
      ["sso_state_mismatch", /Single sign-on failed/],
    ])("shows the sentence for a refused callback (%s) without exchanging anything", async (category, expected) => {
      window.history.replaceState(null, "", `/#/auth/callback?error=${category}`);
      vi.mocked(api.fetchAuthConfig).mockResolvedValue(ssoConfig());
      render(<LoginPage />);
      expect(window.location.hash).toBe("");
      expect(await screen.findByRole("alert")).toHaveTextContent(expected);
      expect(api.completeSsoLogin).not.toHaveBeenCalled();
      expect(useAuthStore.getState().token).toBeNull();
    });

    it.each([
      ["an unknown category", "/#/auth/callback?error=access_denied&error_description=secret"],
      ["a token in the fragment", "/#/auth/callback?access_token=fragment-secret"],
      ["a duplicated code", `/#/auth/callback?code=${HANDOFF_CODE}&code=other`],
      ["a code with a state riding along", `/#/auth/callback?code=${HANDOFF_CODE}&state=x`],
    ])("fails closed on %s after scrubbing, echoing nothing", async (_name, url) => {
      window.history.replaceState(null, "", url);
      vi.mocked(api.fetchAuthConfig).mockResolvedValue(ssoConfig());
      render(<LoginPage />);
      expect(window.location.hash).toBe("");
      expect(await screen.findByRole("alert")).toHaveTextContent("Single sign-on failed");
      expect(screen.getByRole("alert")).not.toHaveTextContent(/secret|access_denied|other/);
      expect(api.completeSsoLogin).not.toHaveBeenCalled();
      expect(useAuthStore.getState().token).toBeNull();
    });

    it("fails closed when the backend refuses the handoff, keeping the page signed out", async () => {
      window.history.replaceState(null, "", `/#/auth/callback?code=${HANDOFF_CODE}`);
      vi.mocked(api.fetchAuthConfig).mockResolvedValue(ssoConfig());
      vi.mocked(api.completeSsoLogin).mockRejectedValue({ status: 401, detail: "handoff refused" });
      render(<LoginPage />);
      expect(await screen.findByRole("alert")).toHaveTextContent("Single sign-on failed");
      expect(screen.getByRole("alert")).not.toHaveTextContent(/handoff refused|H{10}/);
      expect(useAuthStore.getState().token).toBeNull();
      expect(localStorage.getItem("auth_token")).toBeNull();
    });

    it("leaves a legacy query-string callback alone: nothing on the query is a credential here", async () => {
      window.history.replaceState(null, "", "/?code=legacy&state=legacy&token=legacy-secret");
      vi.mocked(api.fetchAuthConfig).mockResolvedValue(ssoConfig());
      render(<LoginPage />);
      await screen.findByRole("button", { name: /single sign-on/i });
      expect(api.completeSsoLogin).not.toHaveBeenCalled();
      expect(fetch).not.toHaveBeenCalled();
      expect(useAuthStore.getState().token).toBeNull();
    });

    it("keeps verify_token on the separate email-verification path", async () => {
      window.history.replaceState(null, "", "/?verify_token=email-token");
      vi.mocked(api.fetchAuthConfig).mockResolvedValue(localConfig("email_verified"));
      vi.mocked(api.verifyEmail).mockResolvedValue({ access_token: "verified-token" });
      vi.mocked(api.fetchCurrentUser).mockResolvedValue({
        user_id: "verified",
        username: "verified",
        display_name: null,
        email: "verified@example.com",
        groups: [],
        dev_admin: false,
      });
      render(<LoginPage />);
      expect(window.location.search).toBe("");
      await waitFor(() => expect(api.verifyEmail).toHaveBeenCalledWith("email-token"));
      expect(api.completeSsoLogin).not.toHaveBeenCalled();
    });
  });

  it("exposes a single status region while auth configuration is loading", () => {
    const { container } = render(<LoginPage />);

    const statuses = screen.getAllByRole("status");
    expect(statuses).toHaveLength(1);
    expect(statuses[0]).toHaveAccessibleName(
      "Loading authentication configuration",
    );

    const spinner = container.querySelector(".spinner");
    expect(spinner).toHaveAttribute("aria-hidden", "true");
    expect(spinner).not.toHaveAttribute("role");
    expect(spinner).not.toHaveAttribute("aria-label");
    // Boot-frame parity (elspeth-b2a677d661): this full-viewport frame and
    // AuthGuard's must show the SAME page-scale affordance — the bare
    // button-scale .spinner is 14px and cannot anchor a viewport.
    expect(spinner).toHaveClass("spinner-page");
  });

  it("renders the local-auth form with labelled inputs and a sign-in button", async () => {
    vi.mocked(api.fetchAuthConfig).mockResolvedValue(localConfig());
    render(<LoginPage />);
    expect(await screen.findByLabelText("Username")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /sign in/i })).toBeInTheDocument();
  });

  it("keeps the username and clears only the password after a failed sign-in", async () => {
    // WCAG 3.3.7 Redundant Entry (elspeth-d49f8ad511): the app cleared
    // BOTH fields via an AuthGuard remount; only the rejected password
    // may be discarded.
    vi.mocked(api.fetchAuthConfig).mockResolvedValue(localConfig());
    const user = userEvent.setup();
    render(<LoginPage />);

    const alert = await failOneSignIn(user);

    expect(alert).toHaveTextContent("Invalid username or password.");
    expect(screen.getByLabelText("Username")).toHaveValue("alice");
    expect(screen.getByLabelText("Password")).toHaveValue("");
  });

  it("associates the sign-in error with both credential fields via aria", async () => {
    // The error copy is deliberately generic (never says which field was
    // wrong), so both fields are flagged and described by the banner.
    vi.mocked(api.fetchAuthConfig).mockResolvedValue(localConfig());
    const user = userEvent.setup();
    render(<LoginPage />);

    const alert = await failOneSignIn(user);
    expect(alert).toHaveAttribute("id", "login-error");

    for (const label of ["Username", "Password"]) {
      const input = screen.getByLabelText(label);
      expect(input).toHaveAttribute("aria-invalid", "true");
      expect(input).toHaveAttribute("aria-describedby", "login-error");
      expect(input).toHaveAccessibleDescription(
        "Invalid username or password.",
      );
    }
  });

  it("stays mounted through a failed sign-in when rendered inside AuthGuard", async () => {
    // Regression for the actual wipe mechanism: authStore.login() used to
    // flip the global isLoading flag, so AuthGuard swapped LoginPage for
    // its boot spinner mid-attempt and remounted a blank form on failure.
    vi.mocked(api.fetchAuthConfig).mockResolvedValue(localConfig());
    const user = userEvent.setup();
    render(
      <AuthGuard>
        <div data-testid="app-shell" />
      </AuthGuard>,
    );

    await failOneSignIn(user);

    expect(screen.getByLabelText("Username")).toHaveValue("alice");
    expect(screen.queryByTestId("app-shell")).not.toBeInTheDocument();
  });

  describe("registration", () => {
    it("offers Create an account when registration is open", async () => {
      vi.mocked(api.fetchAuthConfig).mockResolvedValue(localConfig("open"));
      render(<LoginPage />);
      expect(
        await screen.findByRole("button", { name: "Create an account" }),
      ).toBeInTheDocument();
    });

    it("offers Create an account when email verification is required", async () => {
      vi.mocked(api.fetchAuthConfig).mockResolvedValue(localConfig("email_verified"));
      render(<LoginPage />);
      expect(
        await screen.findByRole("button", { name: "Create an account" }),
      ).toBeInTheDocument();
    });

    it.each(["closed"] as const)(
      "renders no registration affordance when registration_mode is %s",
      async (mode) => {
        vi.mocked(api.fetchAuthConfig).mockResolvedValue(localConfig(mode));
        render(<LoginPage />);
        await screen.findByLabelText("Username");
        expect(
          screen.queryByRole("button", { name: "Create an account" }),
        ).not.toBeInTheDocument();
      },
    );

    it("registers a new account and signs it in with the returned token", async () => {
      vi.mocked(api.fetchAuthConfig).mockResolvedValue(localConfig("open"));
      vi.mocked(api.register).mockResolvedValue({ access_token: "tok-new" });
      vi.mocked(api.fetchCurrentUser).mockResolvedValue({
        user_id: "u-new",
        username: "newuser",
        display_name: "newuser",
        email: null,
        groups: [],
        dev_admin: false,
      });
      const user = userEvent.setup();
      render(<LoginPage />);

      await user.click(
        await screen.findByRole("button", { name: "Create an account" }),
      );
      await user.type(screen.getByLabelText("Username"), "newuser");
      await user.type(screen.getByLabelText("Password"), "correct-horse");
      await user.type(
        screen.getByLabelText("Confirm password"),
        "correct-horse",
      );
      await user.click(screen.getByRole("button", { name: "Create account" }));

      await waitFor(() => {
        expect(useAuthStore.getState().token).toBe("tok-new");
      });
      expect(api.register).toHaveBeenCalledWith("newuser", "correct-horse");
      expect(useAuthStore.getState().user).toMatchObject({
        username: "newuser",
      });
      expect(localStorage.getItem("auth_token")).toBe("tok-new");
    });

    it("registers an email-verified account and waits for verification", async () => {
      vi.mocked(api.fetchAuthConfig).mockResolvedValue(localConfig("email_verified"));
      vi.mocked(api.register).mockResolvedValue({
        status: "verification_required",
        email: "new@example.com",
      });
      const user = userEvent.setup();
      render(<LoginPage />);

      await user.click(
        await screen.findByRole("button", { name: "Create an account" }),
      );
      await user.type(screen.getByLabelText("Username"), "newuser");
      await user.type(screen.getByLabelText("Email"), "new@example.com");
      await user.type(screen.getByLabelText("Password"), "correct-horse");
      await user.type(
        screen.getByLabelText("Confirm password"),
        "correct-horse",
      );
      await user.click(screen.getByRole("button", { name: "Create account" }));

      expect(api.register).toHaveBeenCalledWith(
        "newuser",
        "correct-horse",
        "new@example.com",
      );
      expect(
        await screen.findByText(/check new@example.com/i),
      ).toBeInTheDocument();
      expect(useAuthStore.getState().token).toBeNull();
      expect(localStorage.getItem("auth_token")).toBeNull();
    });

    it("rejects mismatched passwords locally with aria-wired feedback", async () => {
      vi.mocked(api.fetchAuthConfig).mockResolvedValue(localConfig("open"));
      const user = userEvent.setup();
      render(<LoginPage />);

      await user.click(
        await screen.findByRole("button", { name: "Create an account" }),
      );
      await user.type(screen.getByLabelText("Username"), "newuser");
      await user.type(screen.getByLabelText("Password"), "one-password");
      await user.type(
        screen.getByLabelText("Confirm password"),
        "another-password",
      );
      await user.click(screen.getByRole("button", { name: "Create account" }));

      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent("Passwords do not match.");
      expect(alert).toHaveAttribute("id", "register-error");
      expect(api.register).not.toHaveBeenCalled();
      for (const label of ["Password", "Confirm password"]) {
        const input = screen.getByLabelText(label);
        expect(input).toHaveAttribute("aria-invalid", "true");
        expect(input).toHaveAttribute("aria-describedby", "register-error");
      }
      expect(screen.getByLabelText("Username")).not.toHaveAttribute(
        "aria-invalid",
      );
    });

    it("surfaces a username conflict without discarding the attempt", async () => {
      vi.mocked(api.fetchAuthConfig).mockResolvedValue(localConfig("open"));
      vi.mocked(api.register).mockRejectedValue({
        status: 409,
        detail: "User already exists: taken",
      });
      const user = userEvent.setup();
      render(<LoginPage />);

      await user.click(
        await screen.findByRole("button", { name: "Create an account" }),
      );
      await user.type(screen.getByLabelText("Username"), "taken");
      await user.type(screen.getByLabelText("Password"), "correct-horse");
      await user.type(
        screen.getByLabelText("Confirm password"),
        "correct-horse",
      );
      await user.click(screen.getByRole("button", { name: "Create account" }));

      const alert = await screen.findByRole("alert");
      expect(alert).toHaveTextContent("That username is not available.");
      const username = screen.getByLabelText("Username");
      expect(username).toHaveValue("taken");
      expect(username).toHaveAttribute("aria-invalid", "true");
      expect(username).toHaveAttribute("aria-describedby", "register-error");
    });

    it("returns to the sign-in view from the registration form", async () => {
      vi.mocked(api.fetchAuthConfig).mockResolvedValue(localConfig("open"));
      const user = userEvent.setup();
      render(<LoginPage />);

      await user.click(
        await screen.findByRole("button", { name: "Create an account" }),
      );
      expect(screen.getByLabelText("Confirm password")).toBeInTheDocument();

      await user.click(screen.getByRole("button", { name: "Sign in" }));
      expect(
        screen.queryByLabelText("Confirm password"),
      ).not.toBeInTheDocument();
      expect(screen.getByLabelText("Username")).toBeInTheDocument();
    });
  });
});

// ── Login frame + commitment state (elspeth-340f5d104c, elspeth-dcb29d06ba) ──
//
// The login screen was built entirely from inline style objects, and two of
// those declarations were shipping defects rather than idiom complaints:
//
//   * `height: 100vh` on the page wrapper with NO overflow. On a short
//     viewport the centred card was clipped at BOTH ends with no scroll path,
//     so the submit button was unreachable and the user could not sign in.
//   * the in-flight primary adopted `.btn:disabled` (which outranks
//     `.btn-primary` at (0,2,0) vs (0,1,0)) with no progress cue, so "working"
//     and "unavailable" shared one appearance.
//
// These read the resolved style off the element rather than asserting on
// source text: what is being pinned is what reaches the box.
describe("login page frame and commitment state", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.history.replaceState(null, "", "/");
    sessionStorage.clear();
    localStorage.clear();
    resetStore(useAuthStore);
    useAuthStore.setState({ isLoading: false });
    vi.mocked(api.fetchAuthConfig).mockResolvedValue(localConfig());
    vi.stubGlobal("fetch", vi.fn());
  });

  it("gives the page wrapper a bounded height AND an overflow rule, so it owns a scroll", async () => {
    render(<LoginPage />);
    const page = await screen.findByTestId("login-page");
    const style = getComputedStyle(page);

    // Both halves are load-bearing. `overflow-y: auto` on a content-sized box
    // never produces a scrollbar, and a bounded box without it is simply
    // clipped by `body { overflow: hidden }` — either alone leaves the submit
    // button unreachable on a short viewport.
    expect(style.overflowY).toBe("auto");
    // Dynamic viewport unit, NOT the static 100vh the box shipped with: 100vh
    // is the LARGE viewport, which mobile browser chrome overlays.
    expect(style.height).toBe("100dvh");
  });

  it("top-anchors the card when it outgrows the viewport and centres it when it fits", async () => {
    render(<LoginPage />);
    const page = await screen.findByTestId("login-page");
    const card = await screen.findByTestId("login-card");

    // A centred flex item that outgrows its line overflows symmetrically, so
    // scrollTop: 0 lands BELOW the card's top edge. flex-start + auto margins
    // centre while there is free space and top-anchor once there is not.
    expect(getComputedStyle(page).alignItems).toBe("flex-start");
    expect(getComputedStyle(card).margin).toBe("auto");
  });

  it("draws the card edge with a border and a theme-paired shadow token", async () => {
    render(<LoginPage />);
    const cardEl = await screen.findByTestId("login-card");
    const card = getComputedStyle(cardEl);

    // The bespoke `0 2px 8px rgba(10, 40, 50, 0.4)` was a fourth shadow recipe
    // outside the sanctioned three, teal-tinted, and — being inline — could not
    // be overridden by [data-theme="light"], so the light card wore a dark
    // halo. Borders do the heavy lifting; the shadow is a token that flips.
    // Read off the element's own declaration for `border`: jsdom's computed
    // view neither expands nor preserves a shorthand whose value contains
    // var() (it reports the initial `medium none rgb(0,0,0)`), so the computed
    // view cannot answer this one. There is no cascade question to resolve
    // here — an inline declaration always reaches its own element.
    expect(cardEl.style.border).toBe("1px solid var(--color-border)");
    expect(card.boxShadow).toBe("var(--shadow-modal)");
    expect(card.boxShadow).not.toMatch(/rgba?\(/);
  });

  it.each([
    ["Sign in", "Signing in…", "Signing in"],
    ["Create account", "Creating account…", "Creating account"],
  ])(
    "renders a progress cue inside the in-flight %s button",
    async (idleName, busyText, busyName) => {
      // Never settles: the assertion is about the button's committed state.
      const inFlight = new Promise<never>(() => {});
      vi.mocked(api.login).mockReturnValue(inFlight as never);
      vi.mocked(api.register).mockReturnValue(inFlight as never);
      vi.mocked(api.fetchAuthConfig).mockResolvedValue(localConfig("open"));

      const user = userEvent.setup();
      render(<LoginPage />);

      if (idleName === "Create account") {
        await user.click(
          await screen.findByRole("button", { name: "Create an account" }),
        );
        await user.type(screen.getByLabelText("Username"), "alice");
        await user.type(screen.getByLabelText("Password"), "correct-horse");
        await user.type(
          screen.getByLabelText("Confirm password"),
          "correct-horse",
        );
      } else {
        await user.type(await screen.findByLabelText("Username"), "alice");
        await user.type(screen.getByLabelText("Password"), "correct-horse");
      }
      await user.click(screen.getByRole("button", { name: idleName }));

      const button = screen.getByRole("button", { name: busyName });
      expect(button).toHaveAttribute("aria-busy", "true");
      expect(button).toHaveTextContent(busyText);
      // The cue lives INSIDE the button, matching AcknowledgementCard and
      // ExecuteButton — not beside it, where the disabled wash still reads as
      // "unavailable".
      const cue = button.querySelector(".spinner");
      expect(cue).not.toBeNull();
      // aria-hidden, NOT a second live region: the loading branch owns the
      // only role="status" on this screen and the state is already carried
      // programmatically by aria-busy plus the aria-label flip.
      expect(cue).toHaveAttribute("aria-hidden", "true");
      expect(screen.queryAllByRole("status")).toHaveLength(0);
    },
  );
});
