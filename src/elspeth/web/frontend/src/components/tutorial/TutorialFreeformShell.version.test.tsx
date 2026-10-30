// A9-T: the tutorial negotiates the same Composer-Operation-Version as
// freeform. This drives the REAL store, observer and client with only
// `fetch` intercepted (the sibling TutorialFreeformShell tests mock the send
// path and so cannot see the header).
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { detachComposerObservers } from "@/api/composerOperationObserver";
import { setComposerAdmissionVersion } from "@/config/composerProtocol";
import { authenticateComposerCustody, purgeComposerCustody } from "@/stores/composerOperationCustody";
import { useSessionStore } from "@/stores/sessionStore";
import { resetStore } from "@/test/store-helpers";
import { TutorialFreeformShell } from "./TutorialFreeformShell";

vi.mock("@/api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/client")>()),
  getTutorialSample: vi.fn().mockResolvedValue({ sample_urls: ["https://example.gov.au/a.html", "https://example.gov.au/b.html", "https://example.gov.au/c.html"] }),
  getTutorialReadiness: vi.fn().mockResolvedValue({ state_id: null }),
}));
vi.mock("@/components/chat/ChatPanel", () => ({ ChatPanelContent: () => <div>Ordinary freeform chat</div> }));
vi.mock("./TutorialWorkspaceFrame", () => import("@/test/tutorialWorkspaceFrameStub"));

const SESSION = "11111111-1111-4111-8111-111111111111";
const scope = { principalId: "principal", authProvider: "local" };
const posts: { url: string; version: string | null }[] = [];

function stubFetch(): void {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (init?.method === "POST" && /\/(messages|recompose)$/.test(url)) {
      posts.push({ url, version: new Headers(init.headers).get("Composer-Operation-Version") });
      // Park the observer: the test needs only the admission request.
      return new Promise<Response>(() => undefined);
    }
    return new Promise<Response>(() => undefined);
  }));
}
function readySession(): void {
  useSessionStore.setState({
    activeSessionId: SESSION, compositionStateLoaded: true, isComposing: false, messages: [],
    selectSession: vi.fn(async (id: string) => { useSessionStore.setState({ activeSessionId: id, compositionStateLoaded: true }); }),
  });
}
beforeEach(() => {
  posts.length = 0; resetStore(useSessionStore); purgeComposerCustody(); sessionStorage.clear();
  localStorage.setItem("auth_token", "tutorial-version-token"); authenticateComposerCustody(scope); stubFetch(); readySession();
});
afterEach(() => { detachComposerObservers(); setComposerAdmissionVersion(1); vi.unstubAllGlobals(); localStorage.clear(); purgeComposerCustody(); });

describe("A9-T: the tutorial negotiates the same Composer-Operation-Version as freeform", () => {
  it.each([[1, null], [2, "2"]] as const)("admission version %s: tutorial and freeform send the same header", async (admission, header) => {
    setComposerAdmissionVersion(admission);
    const user = userEvent.setup();
    render(<TutorialFreeformShell sessionId={SESSION} onCompleted={vi.fn()} />);
    await user.click(await screen.findByRole("button", { name: "Send tutorial brief" }));
    await vi.waitFor(() => expect(posts).toHaveLength(1));
    const tutorial = posts[0];

    detachComposerObservers(); purgeComposerCustody(); authenticateComposerCustody(scope); readySession();
    void useSessionStore.getState().sendMessage("An ordinary freeform request");
    await vi.waitFor(() => expect(posts).toHaveLength(2));
    const freeform = posts[1];

    expect(tutorial.url).toBe(`/api/sessions/${SESSION}/messages`);
    expect(freeform.url).toBe(tutorial.url);
    expect(tutorial.version).toBe(header);
    expect(freeform.version).toBe(tutorial.version);
  });
});
