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
  /** Run `proceed` now, or ask first when it would throw away typed input. */
  guardLeave: (proceed: () => void) => void;
}

const inert: PeoplePanelServices = {
  setEscapeHandler: () => undefined,
  setDirty: () => undefined,
  onAuthorityRefused: () => undefined,
  guardLeave: (proceed) => proceed(),
};

/** The most records one person's roles or approver links are collected to. */
export const PERSON_RECORDS_MAX = 2000;
const COLLECT_PAGE_SIZE = 200;

export interface Collected<T> {
  items: T[];
  /** True when the bound was reached, so the list may be incomplete and must SAY so. */
  truncated: boolean;
}

/**
 * Read every page of a paginated list, up to a stated bound.
 *
 * These lists answer questions of ABSENCE ("nobody approves for Jane", "Jane
 * does not hold Approver"), and a first page cannot answer those. The routes
 * report no total, so a short page is the end.
 */
export async function collectPages<T>(readPage: (offset: number, limit: number) => Promise<T[]>): Promise<Collected<T>> {
  const items: T[] = [];
  while (items.length < PERSON_RECORDS_MAX) {
    const page = await readPage(items.length, COLLECT_PAGE_SIZE);
    items.push(...page);
    if (page.length < COLLECT_PAGE_SIZE) return { items, truncated: false };
  }
  return { items, truncated: true };
}

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
 * - `uncertain`: no answer arrived, or the server failed (5xx) part-way
 *   through. The write may have landed, so the next
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
      if (!live.current) return;
      const status = errorStatus(error);
      if (status === 404 || status === 403) onAuthorityRefused();
      const reason = adminErrorMessage(error, "Could not refresh details");
      // The CHECK failed, which says nothing about the write it was checking.
      // The notice keeps its kind, because the kind is what carries the retry.
      setNotice((current) => current?.kind === "saved_stale"
        ? { kind: "saved_stale", message: `Saved. ${reason}` }
        : { kind: "uncertain", message: `${reason}, so it is still not known whether the change was saved. Check again before trying it again.` });
    } finally {
      if (live.current) setBusy(false);
    }
  }, [reload, onAuthorityRefused]);

  const run = useCallback(
    async (write: () => Promise<unknown>, done: string, fallback: string): Promise<boolean> => {
      setBusy(true);
      setNotice(null);
      try {
        await write();
      } catch (error) {
        if (!live.current) return false;
        const status = errorStatus(error);
        if (isUncertainOutcome(error) || (status !== null && status >= 500)) {
          // No answer, or a server that failed part-way: neither says the write did not land.
          setMustReconcile(true);
          setNotice({ kind: "uncertain", message: `${status === null ? "The connection dropped before the server answered" : "The server failed while handling this"}, so this may or may not have been saved. Check the current details before trying again.` });
        } else {
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

/** How long "Copied" stays before the button offers Copy again. */
export const COPIED_RESET_MS = 3000;

/**
 * Return a copy button to idle a few seconds after it reports success. A
 * label that stays "Copied" says nothing the second time it is pressed.
 */
export function useCopiedReset(copied: boolean, reset: () => void): void {
  const resetRef = useRef(reset);
  resetRef.current = reset;
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => resetRef.current(), COPIED_RESET_MS);
    return () => window.clearTimeout(timer);
  }, [copied]);
}
