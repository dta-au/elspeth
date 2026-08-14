import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import { AuthGuard } from "./AuthGuard";
import { useAuthStore } from "@/stores/authStore";

// LoginPage and authenticated-children targets are heavy — stub them
// to deterministic data-testids so the test focuses on the
// sessionStorage round-trip semantics.
vi.mock("../auth/LoginPage", () => ({
  LoginPage: () => <div data-testid="login-page-stub">Login</div>,
}));

function _seedUnauthenticated() {
  useAuthStore.setState({
    token: null,
    user: null,
    isLoading: false,
    loginError: null,
  } as never);
}

function _seedAuthenticated() {
  useAuthStore.setState({
    token: "tok-abc",
    user: {
      user_id: "u-1",
      username: "alice",
      display_name: null,
      email: null,
      groups: [],
    } as never,
    isLoading: false,
    loginError: null,
  } as never);
}

const POST_LOGIN_REDIRECT_KEY = "elspeth_post_login_redirect";

describe("AuthGuard", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    // Reset to an unknown-hash starting point; tests set hash explicitly.
    window.location.hash = "";
  });

  it("renders the login page when unauthenticated", () => {
    _seedUnauthenticated();
    render(
      <AuthGuard>
        <div data-testid="children">protected</div>
      </AuthGuard>,
    );
    expect(screen.getByTestId("login-page-stub")).toBeInTheDocument();
    expect(screen.queryByTestId("children")).not.toBeInTheDocument();
  });

  it("shows the page-scale spinner while the auth check is in flight (elspeth-b2a677d661)", () => {
    // Both full-viewport boot frames — this one and LoginPage's config-load
    // frame — must compose the SAME `spinner spinner-page` pair, so the
    // affordance cannot shrink 32px → 14px between two consecutive frames.
    // The size itself lives on .spinner-page in animations.css; what this
    // pins is that AuthGuard no longer hand-rolls an inline size of its own.
    // Hold the auth check IN FLIGHT: useAuth calls loadFromStorage on mount,
    // whose no-token path resolves isLoading synchronously inside render's
    // act() — which would swap this frame for the LoginPage before the query
    // runs. The subject here is the in-flight frame, so the check must stay
    // in flight while it is inspected; the real action is restored after.
    const realLoadFromStorage = useAuthStore.getState().loadFromStorage;
    useAuthStore.setState({
      token: null,
      user: null,
      isLoading: true,
      loginError: null,
      loadFromStorage: async () => {},
    } as never);
    try {
      render(
        <AuthGuard>
          <div data-testid="children">protected</div>
        </AuthGuard>,
      );
      const status = screen.getByRole("status", {
        name: "Checking authentication",
      });
      const spinner = status.querySelector(".spinner");
      expect(spinner).not.toBeNull();
      expect(spinner).toHaveClass("spinner-page");
      expect(spinner).not.toHaveAttribute("style");
    } finally {
      useAuthStore.setState({ loadFromStorage: realLoadFromStorage } as never);
    }
  });

  it("renders children when authenticated", () => {
    _seedAuthenticated();
    render(
      <AuthGuard>
        <div data-testid="children">protected</div>
      </AuthGuard>,
    );
    expect(screen.getByTestId("children")).toBeInTheDocument();
  });

  it("preserves the shared-route hash through the login redirect (sessionStorage round-trip)", async () => {
    // Start unauthenticated on a #/shared/{token} route.
    window.location.hash = "#/shared/tk-abc";
    _seedUnauthenticated();

    const { rerender } = render(
      <AuthGuard>
        <div data-testid="children">protected</div>
      </AuthGuard>,
    );

    // Save effect must persist the hash to sessionStorage.
    await waitFor(() => {
      expect(window.sessionStorage.getItem(POST_LOGIN_REDIRECT_KEY)).toBe(
        "#/shared/tk-abc",
      );
    });

    // Simulate a login flow that drops the hash (e.g. a hard
    // redirect). On v1 we don't actually navigate — the restore path
    // is what matters when authentication flips to true.
    window.location.hash = "";

    // Flip to authenticated and re-render — the restore effect must
    // fire, write the saved hash back to window.location.hash, and
    // clear the sessionStorage key.
    act(() => {
      _seedAuthenticated();
    });
    rerender(
      <AuthGuard>
        <div data-testid="children">protected</div>
      </AuthGuard>,
    );

    await waitFor(() => {
      expect(window.location.hash).toBe("#/shared/tk-abc");
    });
    expect(window.sessionStorage.getItem(POST_LOGIN_REDIRECT_KEY)).toBeNull();
  });

  it("does not save non-shared hashes to sessionStorage", async () => {
    window.location.hash = "#/some-session-id";
    _seedUnauthenticated();
    render(
      <AuthGuard>
        <div data-testid="children">protected</div>
      </AuthGuard>,
    );
    // A short wait to give the save effect a chance to run.
    await new Promise((r) => setTimeout(r, 10));
    expect(window.sessionStorage.getItem(POST_LOGIN_REDIRECT_KEY)).toBeNull();
  });

  it("does not overwrite a pre-existing saved redirect on re-render", async () => {
    window.location.hash = "#/shared/tk-second";
    window.sessionStorage.setItem(POST_LOGIN_REDIRECT_KEY, "#/shared/tk-first");
    _seedUnauthenticated();
    render(
      <AuthGuard>
        <div data-testid="children">protected</div>
      </AuthGuard>,
    );
    await new Promise((r) => setTimeout(r, 10));
    // The earlier-saved value must be preserved — first-write wins.
    expect(window.sessionStorage.getItem(POST_LOGIN_REDIRECT_KEY)).toBe(
      "#/shared/tk-first",
    );
  });

  it("does not re-fire the restore effect when staying authenticated across renders", async () => {
    // No save key set, no shared hash. Authenticated from the start.
    _seedAuthenticated();
    window.location.hash = "";
    const { rerender } = render(
      <AuthGuard>
        <div data-testid="children">protected</div>
      </AuthGuard>,
    );
    // Re-render twice; window.location.hash should stay empty.
    rerender(
      <AuthGuard>
        <div data-testid="children">protected</div>
      </AuthGuard>,
    );
    rerender(
      <AuthGuard>
        <div data-testid="children">protected</div>
      </AuthGuard>,
    );
    expect(window.location.hash).toBe("");
  });
});
