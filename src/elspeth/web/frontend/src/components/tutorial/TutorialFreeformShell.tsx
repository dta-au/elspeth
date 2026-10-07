import { type JSX, useEffect, useRef, useState } from "react";
import { getTutorialReadiness, getTutorialSample } from "@/api/client";
import { ChatPanelContent } from "@/components/chat/ChatPanel";
import { Button } from "@/components/ui";
import { useComposer } from "@/hooks/useComposer";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { useSessionStore } from "@/stores/sessionStore";
import {
  TUTORIAL_SINK_PROMPT,
  TUTORIAL_SOURCE_PROMPT,
  TUTORIAL_TRANSFORMS_PROMPT,
} from "./tutorialMachine";
import { TutorialWorkspaceFrame } from "./TutorialWorkspaceFrame";

interface TutorialFreeformShellProps {
  sessionId: string;
  onCompleted: (sessionId: string) => void;
  onSessionMissing?: (sessionId: string) => void;
}

function tutorialBrief(sampleUrls: readonly string[]): string {
  return [
    "Build a pipeline to scrape and summarize these three synthetic project briefs.",
    `${TUTORIAL_SOURCE_PROMPT}\n${sampleUrls.join("\n")}`,
    TUTORIAL_SINK_PROMPT,
    TUTORIAL_TRANSFORMS_PROMPT,
    "Please show me the proposed pipeline for review before I run it.",
  ].join("\n\n");
}

function errorDetail(error: unknown): string {
  if (typeof error === "object" && error !== null && "detail" in error) {
    const detail = (error as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
  }
  if (error instanceof Error) return error.message;
  return "Could not check the tutorial pipeline. Please try again.";
}

function isMissingSession(error: unknown): boolean {
  return typeof error === "object" && error !== null && "status" in error &&
    (error as { status?: unknown }).status === 404;
}

/** Tutorial framing around the ordinary freeform Composer authoring surface. */
export function TutorialFreeformShell({
  sessionId,
  onCompleted,
  onSessionMissing,
}: TutorialFreeformShellProps): JSX.Element {
  const composer = useComposer();
  const composeTimeoutReady = useSessionStore((state) => state.compositionStateLoaded);
  const activeSessionId = useSessionStore((state) => state.activeSessionId);
  const compositionStateLoaded = useSessionStore((state) => state.compositionStateLoaded);
  const compositionState = useSessionStore((state) => state.compositionState);
  const messages = useSessionStore((state) => state.messages);
  const isComposing = useSessionStore((state) => state.isComposing);
  const proposals = useSessionStore((state) => state.compositionProposals);
  const proposalActionPendingIds = useSessionStore((state) => state.proposalActionPendingIds);
  const pendingReviewCount = useInterpretationEventsStore((state) =>
    Object.keys(state.pendingBySession[sessionId] ?? {}).length,
  );
  const [sampleUrls, setSampleUrls] = useState<string[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [sampleRetry, setSampleRetry] = useState(0);
  const [readinessError, setReadinessError] = useState<string | null>(null);
  const [sent, setSent] = useState(false);
  const [checking, setChecking] = useState(false);
  const sentRef = useRef(false);
  const checkingRef = useRef(false);
  const hasUserMessage = messages.some((message) => message.role === "user");
  const pendingProposal = proposals.some((proposal) => proposal.status === "pending");

  useEffect(() => {
    let active = true;
    setLoadError(null);
    void (async () => {
      try {
        const store = useSessionStore.getState();
        if (store.activeSessionId !== sessionId || !store.compositionStateLoaded || store.error !== null) {
          await store.selectSession(sessionId);
        }
        if (!active) return;
        const selected = useSessionStore.getState();
        if (selected.activeSessionId !== sessionId) {
          onSessionMissing?.(sessionId);
          return;
        }
        if (!selected.compositionStateLoaded || selected.error !== null) {
          setLoadError(selected.error ?? "Could not load the tutorial session. Please retry.");
          return;
        }
        const sample = await getTutorialSample(sessionId);
        if (active) setSampleUrls(sample.sample_urls);
      } catch (error) {
        if (!active) return;
        if (isMissingSession(error)) {
          onSessionMissing?.(sessionId);
        } else {
          setLoadError(errorDetail(error));
        }
      }
    })();
    return () => { active = false; };
  }, [sessionId, onSessionMissing, sampleRetry]);

  const onSendBrief = (): void => {
    if (sentRef.current || sampleUrls === null || hasUserMessage || isComposing ||
        !composeTimeoutReady || activeSessionId !== sessionId) return;
    sentRef.current = true;
    setSent(true);
    void composer.sendMessage(tutorialBrief(sampleUrls));
  };

  const onContinue = (): void => {
    if (checkingRef.current || !hasUserMessage || compositionState === null ||
        activeSessionId !== sessionId || isComposing || pendingProposal ||
        proposalActionPendingIds.length > 0 || pendingReviewCount > 0) return;
    checkingRef.current = true;
    setChecking(true);
    setReadinessError(null);
    const stateId = compositionState.id;
    void getTutorialReadiness(sessionId)
      .then((ready) => {
        const current = useSessionStore.getState();
        const currentReviews = useInterpretationEventsStore.getState().pendingBySession[sessionId] ?? {};
        if (current.activeSessionId !== sessionId || current.compositionState?.id !== stateId ||
            ready.state_id !== stateId || current.isComposing ||
            current.compositionProposals.some((proposal) => proposal.status === "pending") ||
            current.proposalActionPendingIds.length > 0 || Object.keys(currentReviews).length > 0) {
          setReadinessError("The pipeline changed while we checked it. Review the latest state and try again.");
          return;
        }
        onCompleted(sessionId);
      })
      .catch((error: unknown) => setReadinessError(errorDetail(error)))
      .finally(() => {
        checkingRef.current = false;
        setChecking(false);
      });
  };

  const readyToCheck = activeSessionId === sessionId && compositionStateLoaded &&
    hasUserMessage && compositionState !== null && !isComposing &&
    !pendingProposal && proposalActionPendingIds.length === 0 && pendingReviewCount === 0;

  return (
    <TutorialWorkspaceFrame ariaLabel="Tutorial build">
      <div className="tutorial-workspace-authoring">
        <p className="tutorial-kicker">Build</p>
        <h2>Build with the Composer.</h2>
        {loadError !== null && <p role="alert" className="tutorial-error">{loadError}</p>}
        {loadError !== null && (
          <Button onClick={() => setSampleRetry((attempt) => attempt + 1)}>Retry loading example</Button>
        )}
        {sampleUrls === null && loadError === null && <p role="status">Loading your example…</p>}
        {sampleUrls !== null && !hasUserMessage && !sent && (
          <>
            <p>This brief goes through the ordinary freeform Composer. Review its proposal before running anything.</p>
            <pre className="tutorial-brief">{tutorialBrief(sampleUrls)}</pre>
            <Button variant="primary" onClick={onSendBrief} disabled={activeSessionId !== sessionId || isComposing || !composeTimeoutReady}>
              Send tutorial brief
            </Button>
          </>
        )}
        {(hasUserMessage || sent) && <ChatPanelContent composer={composer} allowFork={false} />}
        {sampleUrls !== null && (hasUserMessage || sent) && (
          <>
            <p>Review the graph, YAML, and any pending decisions. Continue only when this pipeline is ready to run.</p>
            {readinessError !== null && <p role="alert" className="tutorial-error">{readinessError}</p>}
            <Button variant="primary" onClick={onContinue} disabled={!readyToCheck || checking}>
              {checking ? "Checking pipeline…" : "Continue to Run"}
            </Button>
          </>
        )}
      </div>
    </TutorialWorkspaceFrame>
  );
}
