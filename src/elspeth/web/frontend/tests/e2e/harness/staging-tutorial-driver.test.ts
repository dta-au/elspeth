import { describe, expect, it, vi } from "vitest";

import {
  FREEFORM_BUILD_ACTION_NAMES,
  driveFreeformTutorial,
  finishTutorialAndVerifyGraduation,
  isAuditRequest,
  isComposeRequest,
  isRunRequest,
  resolveVisibleReviews,
} from "../../../scripts/staging-tutorial-driver.mjs";

describe("standalone staging tutorial driver contract", () => {
  const sessionId = "00000000-0000-4000-8000-000000000000";

  type ReviewButton = { id: number; name: string; disabled: boolean; region?: string };
  type ReviewWait = { action: "isEnabled" | "click" | "waitFor" | "evaluate"; timeout: number };
  // Playwright's fallback action timeout when the page sets none (the standalone harness sets none).
  const DEFAULT_ACTION_TIMEOUT_MS = 30_000;

  // A page double that keeps Playwright's distinction between absent and disabled elements:
  // count() reads the page once and never waits; isEnabled() on a disabled element answers at
  // once, but isEnabled(), click() or waitFor() on a locator that matches nothing (and click() on
  // a disabled element) waits for its timeout and then rejects. Every such wait is recorded in
  // `waits`. An action on a locator that matches more than one element rejects at once (strict
  // mode) unless first() or nth() narrowed it. A locator re-resolves on every call; a string name
  // matches as a case-insensitive substring unless `exact`; getByRole(..., { disabled: false })
  // matches only enabled buttons; a region's getByRole is scoped to the buttons in that region.
  // As in Playwright.
  function reviewPage(
    initial: ReviewButton[],
    hooks: {
      // Runs as a click is dispatched, before its target is resolved: a re-render here is the
      // measured race between the enabled check and the click.
      beforeClickResolves?: (attempt: number) => void;
      // Runs once the click's target resolved; throwing models a click Playwright could not finish.
      onClicked?: (button: ReviewButton) => void;
      // Runs on each page.waitForTimeout, the driver's poll interval.
      onPoll?: (poll: number) => void;
      // Runs right after a region list is read (count() or evaluateAll()): a card that unmounts
      // here is the race between counting the regions and scrolling one by index.
      afterRegionRead?: (read: number) => void;
    } = {},
  ) {
    let buttons = initial;
    let attempts = 0;
    let polls = 0;
    let regionReads = 0;
    const scrolledRegions: number[] = [];
    const clicks = vi.fn<(id: number) => void>();
    const clickTimeouts: Array<number | undefined> = [];
    const waits: ReviewWait[] = [];
    type RoleOptions = { name: string | RegExp; disabled?: boolean; exact?: boolean };
    const nameMatches = (pattern: string | RegExp, name: string, exact?: boolean) =>
      typeof pattern !== "string"
        ? pattern.test(name)
        : exact === true ? name === pattern : name.toLowerCase().includes(pattern.toLowerCase());
    const buttonLocator = (options: RoleOptions, region?: string | RegExp) => {
      const selected = () => buttons.filter((button) =>
        nameMatches(options.name, button.name, options.exact) &&
        (options.disabled !== false || !button.disabled) &&
        (region === undefined || (button.region !== undefined && nameMatches(region, button.region))));
      // `index` null is the locator itself, not narrowed by first() or nth().
      const resolve = (index: number | null, action: string) => {
        const matched = selected();
        if (index === null && matched.length > 1) {
          throw new Error(`locator.${action}: Error: strict mode violation: resolved to ${matched.length} elements`);
        }
        return matched[index ?? 0];
      };
      const element = (index: number | null) => ({
        isEnabled: async (actionOptions?: { timeout?: number }) => {
          const button = resolve(index, "isEnabled");
          if (button === undefined) {
            waits.push({ action: "isEnabled", timeout: actionOptions?.timeout ?? DEFAULT_ACTION_TIMEOUT_MS });
            throw new Error("locator.isEnabled: Timeout exceeded");
          }
          return !button.disabled;
        },
        click: async (actionOptions?: { timeout?: number }) => {
          clickTimeouts.push(actionOptions?.timeout);
          attempts += 1;
          hooks.beforeClickResolves?.(attempts);
          const button = resolve(index, "click");
          if (button === undefined || button.disabled) {
            waits.push({ action: "click", timeout: actionOptions?.timeout ?? DEFAULT_ACTION_TIMEOUT_MS });
            throw new Error("locator.click: Timeout exceeded");
          }
          hooks.onClicked?.(button);
          clicks(button.id);
        },
        waitFor: async (actionOptions?: { timeout?: number }) => {
          if (resolve(index, "waitFor") === undefined) {
            waits.push({ action: "waitFor", timeout: actionOptions?.timeout ?? DEFAULT_ACTION_TIMEOUT_MS });
            throw new Error("locator.waitFor: Timeout exceeded");
          }
        },
      });
      return {
        ...element(null),
        count: async () => selected().length,
        nth: (index: number) => element(index),
        first: () => element(0),
      };
    };
    const getByRole = vi.fn((role: string, options: RoleOptions) => {
      if (role === "heading") return { waitFor: async () => undefined };
      if (role === "region") {
        const regions = () => [...new Set(buttons.flatMap((button) =>
          button.region !== undefined && nameMatches(options.name, button.region) ? [button.region] : []))];
        const regionRead = () => {
          regionReads += 1;
          hooks.afterRegionRead?.(regionReads);
        };
        return {
          count: async () => {
            const count = regions().length;
            regionRead();
            return count;
          },
          evaluateAll: async () => {
            scrolledRegions.push(regions().length);
            regionRead();
          },
          nth: (index: number) => ({
            evaluate: async () => {
              if (regions()[index] === undefined) {
                waits.push({ action: "evaluate", timeout: DEFAULT_ACTION_TIMEOUT_MS });
                throw new Error("locator.evaluate: Timeout exceeded");
              }
            },
          }),
          getByRole: (_role: string, buttonOptions: RoleOptions) => buttonLocator(buttonOptions, options.name),
        };
      }
      return buttonLocator(options);
    });
    return {
      page: {
        getByRole,
        locator: () => ({ first: () => ({ isVisible: async () => false, textContent: async () => null }) }),
        waitForTimeout: async () => {
          polls += 1;
          hooks.onPoll?.(polls);
        },
      },
      clicks,
      clickTimeouts,
      scrolledRegions,
      waits,
      replace: (next: ReviewButton[]) => { buttons = next; },
    };
  }

  const SEND_BRIEF = { id: 0, name: "Send tutorial brief", disabled: false };
  const RUN_TURN = [{ id: 10, name: "Run", disabled: false }, { id: 11, name: "Continue", disabled: false }];

  it("does not wait on an absent Continue to Run or Accept while it polls", async () => {
    const tutorial = reviewPage([SEND_BRIEF], {
      onClicked: (button) => {
        if (button.id === 0) tutorial.replace([]);
        if (button.id === 1) tutorial.replace(RUN_TURN);
      },
      onPoll: (poll) => {
        if (poll === 1) tutorial.replace([{ id: 1, name: "Continue to Run", disabled: false }]);
      },
    });

    await driveFreeformTutorial(tutorial.page as never, { timeoutMs: 5_000 });

    expect(tutorial.clicks.mock.calls).toEqual([[0], [1], [10]]);
    expect(tutorial.waits).toEqual([]);
  });

  it("polls past a disabled Continue to Run without clicking or waiting on it", async () => {
    const tutorial = reviewPage([SEND_BRIEF], {
      onClicked: (button) => {
        if (button.id === 0) tutorial.replace([{ id: 1, name: "Continue to Run", disabled: true }]);
        if (button.id === 1) tutorial.replace(RUN_TURN);
      },
      onPoll: (poll) => {
        if (poll === 1) tutorial.replace([{ id: 1, name: "Continue to Run", disabled: false }]);
      },
    });

    await driveFreeformTutorial(tutorial.page as never, { timeoutMs: 5_000 });

    expect(tutorial.clicks.mock.calls).toEqual([[0], [1], [10]]);
    expect(tutorial.waits).toEqual([]);
  });

  it("accepts a present proposal, then continues to Run, without waiting on absent buttons", async () => {
    const tutorial = reviewPage([SEND_BRIEF], {
      onClicked: (button) => {
        if (button.id === 0) {
          tutorial.replace([{ id: 2, name: "Accept proposal: build it", disabled: false, region: "Awaiting your decision" }]);
        }
        if (button.id === 2) tutorial.replace([{ id: 1, name: "Continue to Run", disabled: false }]);
        if (button.id === 1) tutorial.replace(RUN_TURN);
      },
    });

    await driveFreeformTutorial(tutorial.page as never, { timeoutMs: 5_000 });

    expect(tutorial.clicks.mock.calls).toEqual([[0], [2], [1], [10]]);
    expect(tutorial.waits).toEqual([]);
  });

  it("polls past a disabled Accept without clicking or waiting on it, then accepts it once enabled", async () => {
    // The panel disables Accept while the composer is busy; the driver must keep polling, not spend attempts on it.
    const accept = { id: 2, name: "Accept proposal: build it", region: "Awaiting your decision" };
    const tutorial = reviewPage([SEND_BRIEF], {
      onClicked: (button) => {
        if (button.id === 0) tutorial.replace([{ ...accept, disabled: true }]);
        if (button.id === 2) tutorial.replace([{ id: 1, name: "Continue to Run", disabled: false }]);
        if (button.id === 1) tutorial.replace(RUN_TURN);
      },
      onPoll: (poll) => {
        if (poll === 1) tutorial.replace([{ ...accept, disabled: false }]);
      },
    });

    await driveFreeformTutorial(tutorial.page as never, { timeoutMs: 5_000 });

    expect(tutorial.clicks.mock.calls).toEqual([[0], [2], [1], [10]]);
    expect(tutorial.waits).toEqual([]);
    expect(tutorial.clickTimeouts).toEqual([undefined, 2_000, 2_000, undefined]);
  });

  it("rescans when Continue to Run becomes disabled as its click is dispatched", async () => {
    const tutorial = reviewPage([SEND_BRIEF], {
      beforeClickResolves: (attempt) => {
        // Attempt 1 is Send tutorial brief; attempt 2 is the first Continue to Run click.
        if (attempt === 2) tutorial.replace([{ id: 1, name: "Continue to Run", disabled: true }]);
      },
      onClicked: (button) => {
        if (button.id === 0) tutorial.replace([{ id: 1, name: "Continue to Run", disabled: false }]);
        if (button.id === 1) tutorial.replace(RUN_TURN);
      },
      onPoll: (poll) => {
        if (poll === 1) tutorial.replace([{ id: 1, name: "Continue to Run", disabled: false }]);
      },
    });

    await driveFreeformTutorial(tutorial.page as never, { timeoutMs: 5_000 });

    expect(tutorial.clicks.mock.calls).toEqual([[0], [1], [10]]);
    // The only wait is the bounded click on the button a new review disabled.
    expect(tutorial.waits).toEqual([{ action: "click", timeout: 2_000 }]);
    expect(tutorial.clickTimeouts).toEqual([undefined, 2_000, 2_000, undefined]);
  });

  // The driver's deadline reads Date.now(), and the page double answers at once. These tests move a
  // fake clock by each refused click's timeout and each poll's interval, so a driver that kept
  // retrying would reach its deadline instead of spinning on the wall clock.
  async function onFakeClock(run: () => Promise<void>) {
    vi.useFakeTimers({ toFake: ["Date"] });
    try {
      await run();
    } finally {
      vi.useRealTimers();
    }
  }
  const refusedAfterTimeout = (refused: Error) => {
    vi.setSystemTime(Date.now() + 2_000);
    throw refused;
  };

  it("ends with Playwright's own error when an enabled Continue to Run keeps refusing the click", async () => {
    await onFakeClock(async () => {
      const refused = new Error("locator.click: Timeout 2000ms exceeded: <div> intercepts pointer events");
      const tutorial = reviewPage([SEND_BRIEF], {
        onClicked: (button) => {
          if (button.id === 0) tutorial.replace([{ id: 1, name: "Continue to Run", disabled: false }]);
          if (button.id === 1) refusedAfterTimeout(refused);
        },
        onPoll: () => { vi.setSystemTime(Date.now() + 1_000); },
      });
      const started = Date.now();

      await expect(driveFreeformTutorial(tutorial.page as never, { timeoutMs: 420_000 })).rejects.toBe(refused);
      expect(tutorial.clickTimeouts).toEqual([undefined, 2_000, 2_000, 2_000]);
      expect(Date.now() - started).toBe(6_000);
    });
  });

  it("does not count clicks refused because a new review disabled Continue to Run", async () => {
    const tutorial = reviewPage([SEND_BRIEF], {
      beforeClickResolves: (attempt) => {
        // Attempts 2-4 are Continue to Run clicks that a new review disables as they run.
        if (attempt >= 2 && attempt <= 4) tutorial.replace([{ id: 1, name: "Continue to Run", disabled: true }]);
      },
      onClicked: (button) => {
        if (button.id === 0) tutorial.replace([{ id: 1, name: "Continue to Run", disabled: false }]);
        if (button.id === 1) tutorial.replace(RUN_TURN);
      },
      onPoll: () => { tutorial.replace([{ id: 1, name: "Continue to Run", disabled: false }]); },
    });

    await driveFreeformTutorial(tutorial.page as never, { timeoutMs: 5_000 });

    expect(tutorial.clicks.mock.calls).toEqual([[0], [1], [10]]);
    expect(tutorial.waits).toEqual(Array.from({ length: 3 }, () => ({ action: "click", timeout: 2_000 })));
  });

  it("clicks Continue to Run again after it refused twice while still enabled", async () => {
    let refusals = 0;
    const tutorial = reviewPage([SEND_BRIEF], {
      onClicked: (button) => {
        if (button.id === 0) tutorial.replace([{ id: 1, name: "Continue to Run", disabled: false }]);
        if (button.id === 1) {
          if (refusals < 2) {
            refusals += 1;
            throw new Error("element intercepts pointer events");
          }
          tutorial.replace(RUN_TURN);
        }
      },
    });

    await driveFreeformTutorial(tutorial.page as never, { timeoutMs: 5_000 });

    expect(tutorial.clicks.mock.calls).toEqual([[0], [1], [10]]);
    expect(tutorial.clickTimeouts).toEqual([undefined, 2_000, 2_000, 2_000, undefined]);
  });

  it("ends with Playwright's own error when an enabled Accept keeps refusing the click", async () => {
    await onFakeClock(async () => {
      const refused = new Error("locator.click: Timeout 2000ms exceeded: <div> intercepts pointer events");
      const tutorial = reviewPage([SEND_BRIEF], {
        onClicked: (button) => {
          if (button.id === 0) {
            tutorial.replace([{ id: 2, name: "Accept proposal: build it", disabled: false, region: "Awaiting your decision" }]);
          }
          if (button.id === 2) refusedAfterTimeout(refused);
        },
        onPoll: () => { vi.setSystemTime(Date.now() + 1_000); },
      });

      await expect(driveFreeformTutorial(tutorial.page as never, { timeoutMs: 420_000 })).rejects.toBe(refused);
      expect(tutorial.clickTimeouts).toEqual([undefined, 2_000, 2_000, 2_000]);
    });
  });

  it("counts refused clicks only since the last click that landed", async () => {
    // Proposal 2 is refused once, then lands; proposal 3 is refused twice, then lands.
    const refusalsLeft = new Map([[2, 1], [3, 2]]);
    const proposal = (id: number) => ({ id, name: `Accept proposal: ${id}`, disabled: false, region: "Awaiting your decision" });
    const tutorial = reviewPage([SEND_BRIEF], {
      onClicked: (button) => {
        const left = refusalsLeft.get(button.id) ?? 0;
        if (left > 0) {
          refusalsLeft.set(button.id, left - 1);
          throw new Error("element intercepts pointer events");
        }
        if (button.id === 0) tutorial.replace([proposal(2)]);
        if (button.id === 2) tutorial.replace([proposal(3)]);
        if (button.id === 3) tutorial.replace([{ id: 1, name: "Continue to Run", disabled: false }]);
        if (button.id === 1) tutorial.replace(RUN_TURN);
      },
    });

    await driveFreeformTutorial(tutorial.page as never, { timeoutMs: 5_000 });

    expect(tutorial.clicks.mock.calls).toEqual([[0], [2], [3], [1], [10]]);
  });

  it("keeps Accept refusals and Continue to Run refusals in separate counts", async () => {
    // Accept is refused twice while enabled; Continue to Run then appears and is refused once
    // while enabled. One shared count would reach the attempt limit on that third refusal.
    let acceptRefusals = 0;
    let continueRefusals = 0;
    const accept = { id: 2, name: "Accept proposal: build it", disabled: false, region: "Awaiting your decision" };
    const tutorial = reviewPage([SEND_BRIEF], {
      onClicked: (button) => {
        if (button.id === 0) tutorial.replace([accept]);
        if (button.id === 2 && acceptRefusals < 2) {
          acceptRefusals += 1;
          if (acceptRefusals === 2) tutorial.replace([{ id: 1, name: "Continue to Run", disabled: false }, accept]);
          throw new Error("element intercepts pointer events");
        }
        if (button.id === 1) {
          if (continueRefusals < 1) {
            continueRefusals += 1;
            throw new Error("element intercepts pointer events");
          }
          tutorial.replace(RUN_TURN);
        }
      },
    });

    await driveFreeformTutorial(tutorial.page as never, { timeoutMs: 5_000 });

    expect(tutorial.clicks.mock.calls).toEqual([[0], [1], [10]]);
    expect(tutorial.clickTimeouts).toEqual([undefined, 2_000, 2_000, 2_000, 2_000, undefined]);
  });

  it("clicks the remaining enabled review when the card list re-renders as the click is dispatched", async () => {
    const review = reviewPage([
      { id: 1, name: "Acknowledge", disabled: true },
      { id: 2, name: "Acknowledge", disabled: true },
      { id: 3, name: "Acknowledge", disabled: false },
    ], {
      beforeClickResolves: (attempt) => {
        if (attempt === 1) review.replace([{ id: 4, name: "Acknowledge", disabled: false }]);
      },
      onClicked: () => { review.replace([]); },
    });

    await expect(resolveVisibleReviews(review.page as never)).resolves.toBe(1);
    expect(review.clicks).toHaveBeenCalledExactlyOnceWith(4);
    expect(review.clickTimeouts).toEqual([2_000]);
    expect(review.waits).toEqual([]);
  });

  it("rescans after a review click rejects during a card change", async () => {
    const review = reviewPage([{ id: 1, name: "Acknowledge", disabled: false }], {
      onClicked: (button) => {
        if (button.id === 1) {
          review.replace([{ id: 2, name: "Acknowledge", disabled: false }]);
          throw new Error("element was detached from the DOM");
        }
        review.replace([]);
      },
    });

    await expect(resolveVisibleReviews(review.page as never)).resolves.toBe(1);
    expect(review.clicks).toHaveBeenCalledExactlyOnceWith(2);
    expect(review.clickTimeouts).toEqual([2_000, 2_000]);
    expect(review.waits).toEqual([]);
  });

  it("ends with Playwright's own error when an enabled review keeps refusing the click", async () => {
    const refused = new Error("locator.click: Timeout 2000ms exceeded: <div> intercepts pointer events");
    const review = reviewPage([{ id: 1, name: "Acknowledge", disabled: false }], {
      onClicked: () => { throw refused; },
    });

    await expect(resolveVisibleReviews(review.page as never)).rejects.toBe(refused);
    expect(review.clicks).not.toHaveBeenCalled();
    expect(review.clickTimeouts).toEqual([2_000, 2_000, 2_000]);
  });

  it("starts every review walk with a fresh refusal count", async () => {
    // The harness runs many tutorials in one process. The first walk ends after two refusals
    // while enabled, then the card goes; the second walk's one refusal must not add to them.
    const earlier = reviewPage([{ id: 1, name: "Acknowledge", disabled: false }], {
      beforeClickResolves: (attempt) => {
        if (attempt === 3) earlier.replace([]);
      },
      onClicked: () => { throw new Error("element intercepts pointer events"); },
    });
    await expect(resolveVisibleReviews(earlier.page as never)).resolves.toBe(0);
    expect(earlier.clickTimeouts).toEqual([2_000, 2_000, 2_000]);

    let refused = false;
    const later = reviewPage([{ id: 2, name: "Acknowledge", disabled: false }], {
      onClicked: () => {
        if (!refused) {
          refused = true;
          throw new Error("element intercepts pointer events");
        }
        later.replace([]);
      },
    });
    await expect(resolveVisibleReviews(later.page as never)).resolves.toBe(1);
    expect(later.clicks).toHaveBeenCalledExactlyOnceWith(2);
  });

  it("scrolls the prompt regions present now without waiting on one that unmounted", async () => {
    const review = reviewPage([
      { id: 5, name: "View prompt", disabled: false, region: "Prompt template review" },
    ], {
      afterRegionRead: (read) => {
        // The prompt card unmounts right after the first read of the region list.
        if (read === 1) review.replace([{ id: 6, name: "Acknowledge", disabled: false }]);
      },
      onClicked: () => { review.replace([]); },
    });

    await expect(resolveVisibleReviews(review.page as never)).resolves.toBe(1);
    expect(review.clicks).toHaveBeenCalledExactlyOnceWith(6);
    expect(review.waits).toEqual([]);
    // The one region present at the first read was scrolled.
    expect(review.scrolledRegions[0]).toBe(1);
  });

  it("never clicks disabled review buttons", async () => {
    const review = reviewPage([
      { id: 1, name: "Acknowledge", disabled: true },
      { id: 2, name: "Acknowledge", disabled: false },
    ], { onClicked: () => { review.replace([]); } });

    await expect(resolveVisibleReviews(review.page as never)).resolves.toBe(1);
    expect(review.clicks).toHaveBeenCalledExactlyOnceWith(2);
    expect(review.waits).toEqual([]);
  });

  it("clicks the first of several enabled review buttons, one per pass", async () => {
    const review = reviewPage([
      { id: 1, name: "Acknowledge", disabled: false },
      { id: 2, name: "Acknowledge", disabled: false },
    ], {
      onClicked: (button) => { review.replace(button.id === 1 ? [{ id: 2, name: "Acknowledge", disabled: false }] : []); },
    });

    await expect(resolveVisibleReviews(review.page as never)).resolves.toBe(2);
    expect(review.clicks.mock.calls).toEqual([[1], [2]]);
    expect(review.clickTimeouts).toEqual([2_000, 2_000]);
    expect(review.waits).toEqual([]);
  });

  it("returns at once when there is nothing to review", async () => {
    const review = reviewPage([]);

    await expect(resolveVisibleReviews(review.page as never)).resolves.toBe(0);
    expect(review.clicks).not.toHaveBeenCalled();
    expect(review.waits).toEqual([]);
  });

  it("does not wait on the absent earlier review actions before acknowledging", async () => {
    const review = reviewPage([{ id: 1, name: "Acknowledge", disabled: false }], {
      onClicked: () => { review.replace([]); },
    });

    await expect(resolveVisibleReviews(review.page as never)).resolves.toBe(1);
    expect(review.clicks).toHaveBeenCalledExactlyOnceWith(1);
    expect(review.waits).toEqual([]);
  });

  it("returns at once when every review button is disabled", async () => {
    const review = reviewPage([
      { id: 1, name: "View prompt", disabled: true },
      { id: 2, name: "Acknowledge", disabled: true },
    ]);

    await expect(resolveVisibleReviews(review.page as never)).resolves.toBe(0);
    expect(review.clicks).not.toHaveBeenCalled();
    expect(review.waits).toEqual([]);
  });

  it("drives the ordinary freeform Build and explicit Run gestures", () => {
    expect(FREEFORM_BUILD_ACTION_NAMES).toEqual(["Send tutorial brief", "Continue to Run", "Run"]);
  });

  it("treats composer messages, not a removed guided route, as the compose step", () => {
    expect(
      isComposeRequest(
        `https://staging.example/api/sessions/${sessionId}/guided/respond`,
        "POST",
      ),
    ).toBe(false);
    expect(
      isComposeRequest(
        `https://staging.example/api/sessions/${sessionId}/messages`,
        "POST",
      ),
    ).toBe(true);
  });

  it("identifies the tutorial run request independently", () => {
    expect(
      isRunRequest("https://staging.example/api/tutorial/run", "POST"),
    ).toBe(true);
    expect(
      isRunRequest("https://staging.example/api/tutorial/run", "GET"),
    ).toBe(false);
  });

  it("identifies the evidence-backed audit story read", () => {
    expect(isAuditRequest(`https://staging.example/api/sessions/${sessionId}/runs/run-1/audit-story`, "GET")).toBe(true);
    expect(isAuditRequest(`https://staging.example/api/sessions/${sessionId}/runs/run-1/audit-story`, "POST")).toBe(false);
  });

  function graduationPage(hash: string, clickError?: Error) {
    const click = vi.fn(async () => {
      if (clickError !== undefined) throw clickError;
    });
    const waitFor = vi.fn(async () => undefined);
    const page = {
      getByRole: vi.fn(() => ({ click, waitFor })),
      getByLabel: vi.fn(() => ({ waitFor })),
      waitForURL: vi.fn(async (matches: (url: URL) => boolean) => {
        if (!matches(new URL(`https://staging.example/${hash}`))) throw new Error("wrong session landing");
      }),
    };
    return { page, click, waitFor };
  }

  it("proves the final click, same-session landing, and persisted completion", async () => {
    const { page, click, waitFor } = graduationPage(`#/${sessionId}`);
    const loadPreferences = vi.fn(async () => ({
      tutorial_completed_at: "2026-09-28T00:00:00Z",
      tutorial_stage: null,
      tutorial_session_id: null,
      tutorial_run_id: null,
      tutorial_source_data_hash: null,
    }));

    await expect(finishTutorialAndVerifyGraduation(page as never, sessionId, loadPreferences)).resolves.toEqual({
      completed_at: "2026-09-28T00:00:00Z",
      landed_session_id: sessionId,
    });
    expect(click).toHaveBeenCalledOnce();
    expect(waitFor).toHaveBeenCalled();
    expect(page.waitForURL).toHaveBeenCalledOnce();
    expect(loadPreferences).toHaveBeenCalledOnce();
  });

  it("fails when the final click fails, the landing changes session, or completion did not persist", async () => {
    const persisted = async () => ({ tutorial_completed_at: "2026-09-28T00:00:00Z", tutorial_stage: null, tutorial_session_id: null, tutorial_run_id: null, tutorial_source_data_hash: null });
    const failedClick = graduationPage(`#/${sessionId}`, new Error("rename failed"));
    await expect(finishTutorialAndVerifyGraduation(failedClick.page as never, sessionId, persisted)).rejects.toThrow("rename failed");

    const wrongSession = graduationPage("#/some-other-session");
    await expect(finishTutorialAndVerifyGraduation(wrongSession.page as never, sessionId, persisted)).rejects.toThrow("wrong session landing");

    const missingCompletion = graduationPage(`#/${sessionId}`);
    await expect(finishTutorialAndVerifyGraduation(missingCompletion.page as never, sessionId, async () => ({ tutorial_completed_at: null, tutorial_stage: "graduation", tutorial_session_id: sessionId, tutorial_run_id: null, tutorial_source_data_hash: null }))).rejects.toThrow(/completion.*persist/i);
  });
});
