import React from "react";
import { createRoot } from "react-dom/client";
import { ChatPanelContent } from "@/components/chat/ChatPanel";
import { useComposer } from "@/hooks/useComposer";
import { useAuthStore } from "@/stores/authStore";
import { useSessionStore } from "@/stores/sessionStore";
import * as custody from "@/stores/composerOperationCustody";

const requests: { method: string; path: string; operationId?: string; status?: number }[] = [];
let holdMode: "terminal" | "admission" | null = null;
let oldSession: string | null = null;
let held: { sessionId: string; operationId: string; messageId: string | null } | null = null;
let release: (() => void) | null = null;
let delivered: Promise<void> = Promise.resolve();
let markDelivered: (() => void) | null = null;
const actualFetch = window.fetch.bind(window);
window.fetch = async (input, init = {}) => {
  const url = new URL(String(input), location.href);
  if (url.origin !== location.origin) throw new Error("Offline React harness refused nonlocal fetch");
  const item = { method: init.method ?? "GET", path: url.pathname } as typeof requests[number];
  if (item.method === "POST" && url.pathname.endsWith("/messages")) item.operationId = JSON.parse(String(init.body)).operation_id;
  requests.push(item);
  const response = await actualFetch(input, init);
  let heldThisResponse = false;
  item.status = response.status;
  if (oldSession !== null && url.pathname.startsWith(`/api/sessions/${oldSession}/`)) {
    if (holdMode === "admission" && item.method === "POST" && url.pathname.endsWith("/messages") && response.status === 202) {
      const acknowledgement = await response.clone().json();
      heldThisResponse = true;
      held = { sessionId: oldSession, operationId: acknowledgement.operation_id, messageId: null };
      holdMode = null;
      delivered = new Promise<void>(resolve => { markDelivered = resolve; });
      await new Promise<void>(resolve => { release = resolve; });
    } else if (holdMode === "terminal" && item.method === "GET" && /\/operations\/[^/]+$/.test(url.pathname) && response.ok) {
      const snapshot = await response.clone().json();
      if (snapshot.status === "completed") {
        heldThisResponse = true;
      held = { sessionId: oldSession, operationId: snapshot.operation_id, messageId: snapshot.result.message.id };
        holdMode = null;
        delivered = new Promise<void>(resolve => { markDelivered = resolve; });
        await new Promise<void>(resolve => { release = resolve; });
      }
    }
  }
  if (heldThisResponse && markDelivered !== null) { markDelivered(); markDelivered = null; }
  return response;
};

const actions: Promise<void>[] = [];
function ActualComposerSurface() {
  const composer = useComposer();
  const sendMessage = (content: string) => {
    const action = composer.sendMessage(content);
    actions.push(action);
    return action;
  };
  // Observe the real hook's returned action without changing its result.
  return <ChatPanelContent allowFork={false} composer={{ ...composer, sendMessage }} />;
}

// Test-owned controls do not replace stores, observer or API.
const harness = {
  async mount(token: string, sessionId: string) {
    await useAuthStore.getState().loginWithToken(token);
    if (useAuthStore.getState().user === null) throw new Error("Real authentication failed");
    await useSessionStore.getState().selectSession(sessionId);
    const container = document.createElement("div");
    container.id = "actual-react-chat";
    document.body.replaceChildren(container);
    createRoot(container).render(<ActualComposerSurface />);
  },
  hold(mode: "terminal" | "admission", sessionId: string) { holdMode = mode; oldSession = sessionId; },
  release() { if (release === null) throw new Error("No held actual response"); release(); release = null; },
  async replace(token: string, sessionId: string) {
    await useAuthStore.getState().loginWithToken(token);
    await useSessionStore.getState().selectSession(sessionId);
  },
  async joinOriginalAction() { await delivered; await actions[0]; await new Promise<void>(resolve => requestAnimationFrame(() => resolve())); },
  state() {
    const state = useSessionStore.getState();
    return { requests, held, activeSessionId: state.activeSessionId, loaded: state.compositionStateLoaded,
      isComposing: state.isComposing, messages: state.messages.map(m => ({ id: m.id, sessionId: m.session_id, content: m.content })),
      principalId: useAuthStore.getState().user?.user_id ?? null,
      scope: custody.composerCustodyScope(), rawCustody: sessionStorage.getItem(custody.COMPOSER_CUSTODY_KEY) };
  },
};
Object.assign(window, { reactComposerAcceptance: harness });
