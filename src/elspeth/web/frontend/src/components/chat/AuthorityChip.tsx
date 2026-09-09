// src/components/chat/AuthorityChip.tsx
//
// Persistent composer-authority chip for the chat header
// (elspeth-f5e6723133). The session's trust_mode decides whether the
// composer applies pipeline mutations immediately (auto_commit — the
// deliberate default; see sessions/models.py for the convergence
// rationale) or stages every mutation as a reviewable proposal
// (explicit_approve). That authority was loaded into the store on every
// session select but surfaced nowhere, so users could not distinguish
// "the agent is browsing" from "the agent is changing my pipeline right
// now". Sibling of ModelChip: an auditability product names the authority
// in the authoring chrome, not only in the audit trail.
//
// When preferences have not loaded, the chip renders nothing — absence of
// chrome, never a fabricated authority claim.

import { useSessionStore } from "@/stores/sessionStore";

const AUTHORITY_COPY = {
  auto_commit: {
    label: "Auto-apply on",
    title:
      "The composer applies pipeline changes immediately, without asking first. " +
      "Every change is versioned and audited, and the transcript labels each applied change.",
  },
  explicit_approve: {
    label: "Approval required",
    title:
      "Pipeline changes wait for your review as proposals before anything is applied.",
  },
} as const;

export function AuthorityChip() {
  const trustMode = useSessionStore(
    (s) => s.composerPreferences?.trust_mode ?? null,
  );

  if (trustMode === null) return null;
  const copy = AUTHORITY_COPY[trustMode];

  return (
    <span
      className={`chat-authority-chip chat-authority-chip--${trustMode}`}
      aria-label={`Composer authority: ${copy.label}`}
      title={copy.title}
    >
      {copy.label}
    </span>
  );
}
