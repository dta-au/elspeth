import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import { adminErrorMessage } from "@/api/identityAdmin";
import { errorStatus, isUncertainOutcome } from "@/api/people";

/**
 * Services the People & access shell offers every section inside it.
 *
 * They exist so a section never has to know about its siblings: a role form
 * must not close an unrelated password result, Escape must cancel the
 * innermost open form before it closes the dialog, and a refusal that means
 * "you no longer hold this capability" must reach the shell that owns them.
 */
export interface PeoplePanelServices {
  /** Register the innermost cancellable subview. Escape runs it instead of closing. */
  setEscapeHandler: (owner: string, handler: (() => void) | null) => void;
  /** Report unsaved input, so leaving the person or the dialog asks first. */
  setDirty: (owner: string, dirty: boolean) => void;
  /** A refusal that hides a surface (404) or forbids it: re-read capabilities. */
  onAuthorityRefused: () => void;
}

const inert: PeoplePanelServices = {
  setEscapeHandler: () => undefined,
  setDirty: () => undefined,
  onAuthorityRefused: () => undefined,
};

export const PeoplePanelContext = createContext<PeoplePanelServices>(inert);

export function usePeoplePanel(): PeoplePanelServices {
  return useContext(PeoplePanelContext);
}

/**
 * Cancel a form and hand focus back to the control that opened it.
 *
 * A cancelled form unmounts, and focus left on a removed node falls to
 * <body>: a keyboard user is dropped at the top of the page behind a modal.
 * Success does NOT use this: a completed write moves focus to its outcome
 * notice instead, which is where the administrator needs to be.
 */
export function useCancelToTrigger<T extends HTMLElement>(close: () => void): { triggerRef: RefObject<T>; cancel: () => void } {
  const triggerRef = useRef<T>(null);
  const cancel = useCallback(() => {
    close();
    // The trigger is re-mounted by the render that closes the form.
    setTimeout(() => triggerRef.current?.focus(), 0);
  }, [close]);
  return { triggerRef, cancel };
}

/** Bind a section's open form to the shell: Escape cancels it, and leaving warns while it holds input. */
export function useSubview(owner: string, open: boolean, dirty: boolean, cancel: () => void): void {
  const { setEscapeHandler, setDirty } = usePeoplePanel();
  const cancelRef = useRef(cancel);
  cancelRef.current = cancel;
  useEffect(() => {
    setEscapeHandler(owner, open ? () => cancelRef.current() : null);
    return () => setEscapeHandler(owner, null);
  }, [owner, open, setEscapeHandler]);
  useEffect(() => {
    setDirty(owner, open && dirty);
    return () => setDirty(owner, false);
  }, [owner, open, dirty, setDirty]);
}

/**
 * What became of one write. The three failures are different facts and ask
 * different things of the administrator, so they are never one message:
 *
 * - `rejected`: the server answered no. Nothing changed; fix and retry.
 * - `uncertain`: no answer arrived. The write may have landed, so the next
 *   step is to LOOK, and another write is withheld until they have.
 * - `saved_stale`: the write landed and the re-read after it failed. Saying
 *   "failed" here would invite a duplicate grant.
 */
export type MutationNotice =
  | { kind: "saved"; message: string }
  | { kind: "saved_stale"; message: string }
  | { kind: "rejected"; message: string }
  | { kind: "uncertain"; message: string };

export interface PersonMutation {
  busy: boolean;
  notice: MutationNotice | null;
  /** True until an uncertain outcome has been reconciled by a successful re-read. */
  mustReconcile: boolean;
  run: (write: () => Promise<unknown>, done: string, fallback: string) => Promise<boolean>;
  refresh: () => Promise<void>;
  clear: () => void;
}

/**
 * Run writes for one person and re-read after each.
 *
 * `reload` is the section's own re-read. The write and the re-read have
 * separate failure handlers on purpose: sharing one made a successful save
 * report as a failed one whenever only the refresh went wrong.
 */
export function usePersonMutation(reload: () => Promise<void>): PersonMutation {
  const { onAuthorityRefused } = usePeoplePanel();
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<MutationNotice | null>(null);
  const [mustReconcile, setMustReconcile] = useState(false);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);

  const refresh = useCallback(async () => {
    setBusy(true);
    try {
      await reload();
      if (!live.current) return;
      setMustReconcile(false);
      setNotice((current) => (current?.kind === "saved_stale" ? { kind: "saved", message: "Details are up to date." } : current?.kind === "uncertain" ? null : current));
    } catch (error) {
      if (live.current) setNotice({ kind: "rejected", message: adminErrorMessage(error, "Could not refresh details") });
    } finally {
      if (live.current) setBusy(false);
    }
  }, [reload]);

  const run = useCallback(
    async (write: () => Promise<unknown>, done: string, fallback: string): Promise<boolean> => {
      setBusy(true);
      setNotice(null);
      try {
        await write();
      } catch (error) {
        if (!live.current) return false;
        if (isUncertainOutcome(error)) {
          setMustReconcile(true);
          setNotice({ kind: "uncertain", message: "The connection dropped before the server answered, so this may or may not have been saved. Check the current details before trying again." });
        } else {
          const status = errorStatus(error);
          if (status === 404 || status === 403) onAuthorityRefused();
          setNotice({ kind: "rejected", message: adminErrorMessage(error, fallback) });
        }
        setBusy(false);
        return false;
      }
      try {
        await reload();
        if (live.current) setNotice({ kind: "saved", message: done });
      } catch {
        if (live.current) setNotice({ kind: "saved_stale", message: "Saved. Could not refresh details." });
      } finally {
        if (live.current) setBusy(false);
      }
      return true;
    },
    [reload, onAuthorityRefused],
  );

  const clear = useCallback(() => setNotice(null), []);
  return { busy, notice, mustReconcile, run, refresh, clear };
}
