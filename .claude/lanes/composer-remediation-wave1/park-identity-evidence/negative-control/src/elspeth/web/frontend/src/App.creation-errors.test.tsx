import type { ReactNode } from "react";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import * as api from "./api/client";
import { useSessionStore } from "./stores/sessionStore";
import { usePreferencesStore } from "./stores/preferencesStore";
import type { Session } from "./types/api";

// Keep App's real mount branches and the real session-creation action. The
// surrounding workspaces and their unrelated API effects are outside this test.
vi.mock("./components/common/AuthGuard", () => ({
  AuthGuard: ({ children }: { children: ReactNode }) => children,
}));
vi.mock("./components/common/AppHeader", () => ({ AppHeader: () => null }));
vi.mock("./components/sidebar/GraphModal", () => ({ GraphModal: () => null }));
vi.mock("./components/sidebar/ImportYamlModal", () => ({ ImportYamlModalHost: () => null }));
vi.mock("./components/common/CommandPalette", () => ({ CommandPalette: () => null }));
vi.mock("./components/execution/RunOutcomeNotice", () => ({ RunOutcomeNotice: () => null }));
vi.mock("./components/chat/ChatPanel", () => ({
  ChatPanel: () => <div data-testid="chat-panel" />,
}));
vi.mock("./components/catalog/CatalogDrawer", () => ({ CatalogDrawer: () => null }));
vi.mock("./components/recovery/RecoveryPanel", () => ({ RecoveryPanel: () => null }));
vi.mock("./components/composer/SaveForReviewDialog", () => ({ SaveForReviewDialog: () => null }));
vi.mock("./components/workspace/ComposerWorkspace", () => ({
  ComposerWorkspace: ({ authoring }: { authoring: ReactNode }) => authoring,
}));
vi.mock("./components/workspace/WorkspacePaneContext", () => ({
  useWorkspacePaneController: () => ({ state: { authoringCollapsed: false } }),
}));
vi.mock("./components/tutorial", () => ({
  HelloWorldTutorial: () => <div data-testid="tutorial" />,
}));
vi.mock("./hooks/useHashRouter", () => ({ useHashRouter: () => ({ redirectToast: null }) }));
vi.mock("./hooks/useSharedRoute", () => ({ useSharedRoute: () => ({ kind: "none" }) }));
vi.mock("./hooks/useAuth", () => ({ useAuth: () => ({ isAuthenticated: true }) }));
vi.mock("./hooks/useAutoResumeSession", () => ({ useAutoResumeSession: () => undefined }));
vi.mock("./hooks/useSession", () => ({ useSessionLifecycle: () => undefined }));
vi.mock("./stores/subscriptions", () => ({ initStoreSubscriptions: vi.fn(), requestValidate: vi.fn() }));
vi.mock("./api/client", async (importOriginal) => ({
  ...await importOriginal<typeof import("./api/client")>(),
  createSession: vi.fn(),
  fetchSystemStatus: vi.fn().mockResolvedValue({
    composer_available: true,
    composer_model: "test-model",
    composer_advisor_model: "test-advisor",
  }),
}));

const denialDetail = "This action requires the user role.";
const recovery = "Ask your administrator to grant that role before retrying.";
const created: Session = {
  id: "created-session",
  title: "New session",
  created_at: "2026-10-17T00:00:00Z",
  updated_at: "2026-10-17T00:00:00Z",
};

beforeEach(() => {
  vi.clearAllMocks();
  useSessionStore.getState().reset();
  usePreferencesStore.setState({
    loaded: true,
    tutorialCompleted: true,
    bootstrapError: null,
    writeError: null,
    bootstrap: vi.fn().mockResolvedValue(undefined),
  });
  useSessionStore.setState({ sessionsLoaded: true });
});

afterEach(() => {
  cleanup();
  useSessionStore.getState().reset();
});

describe("session creation failures", () => {
  it("retains public API denial and recovery text without stale error details", async () => {
    const denial = api.apiErrorFromBody(403, {
      detail: denialDetail,
      guidance: recovery,
      recovery_text: recovery,
    });
    vi.mocked(api.createSession).mockRejectedValueOnce(denial);
    useSessionStore.setState({ errorDetails: ["An older validation failure"] });

    await useSessionStore.getState().createSession();

    expect(useSessionStore.getState().error).toBe(`${denialDetail} ${recovery}`);
    expect(useSessionStore.getState().errorDetails).toBeNull();
    expect(useSessionStore.getState().activeSessionId).toBeNull();
    expect(useSessionStore.getState().sessions).toEqual([]);
  });

  it.each([
    new Error("Private transport diagnostics must not be shown"),
    null,
    { detail: "Not an API error" },
    { status: 503, detail: "   " },
  ])("uses a useful fallback for non-API or empty failures: %j", async (error) => {
    vi.mocked(api.createSession).mockRejectedValueOnce(error);

    await useSessionStore.getState().createSession();

    expect(useSessionStore.getState().error).toBe("Failed to create session. Please try again.");
  });

  it.each(["landing", "tutorial", "empty workspace"])(
    "announces and dismisses a failure on the no-session %s surface",
    async (surface) => {
      useSessionStore.setState({ error: denialDetail, sessionsLoaded: surface !== "empty workspace" });
      usePreferencesStore.setState({ tutorialCompleted: surface !== "tutorial" });
      render(<App />);

      expect(screen.getByRole("alert")).toHaveTextContent(denialDetail);
      if (surface === "landing") expect(screen.getByText("No sessions yet")).toBeInTheDocument();
      if (surface === "tutorial") expect(screen.getByTestId("tutorial")).toBeInTheDocument();
      if (surface === "empty workspace") expect(screen.getByTestId("chat-panel")).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "Dismiss error" }));
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(useSessionStore.getState().error).toBeNull();
    },
  );

  it("shows a rejected landing action and removes the failure after a successful retry", async () => {
    vi.mocked(api.createSession)
      .mockRejectedValueOnce(api.apiErrorFromBody(403, { detail: denialDetail, recovery_text: recovery }))
      .mockResolvedValueOnce(created);
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: /new session/i }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(denialDetail);
    expect(alert).toHaveTextContent(recovery);
    expect(useSessionStore.getState().activeSessionId).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /new session/i }));
    expect(await screen.findByTestId("chat-panel")).toBeInTheDocument();
    expect(useSessionStore.getState().activeSessionId).toBe(created.id);
    expect(useSessionStore.getState().error).toBeNull();
    expect(useSessionStore.getState().errorDetails).toBeNull();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(api.createSession).toHaveBeenCalledTimes(2);
  });

  it("renders public error text as text, without creating markup", async () => {
    useSessionStore.setState({ error: "Denied <img src=x onerror=alert(1)>" });
    render(<App />);

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Denied <img src=x onerror=alert(1)>");
    expect(within(alert).queryByRole("img")).not.toBeInTheDocument();
    await act(async () => undefined);
  });

  it("leaves active-session errors with their existing chat owner", async () => {
    useSessionStore.setState({ activeSessionId: created.id, sessions: [created], error: denialDetail });
    render(<App />);

    expect(screen.getByTestId("chat-panel")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await act(async () => undefined);
  });
});
