import { describe, expect, it } from "vitest";
import { decodeUserComposerPreferences } from "./preferencesDecoder";

const full = {
  default_mode: "guided",
  banner_dismissed_at: null,
  freeform_intro_dismissed_at: "2026-05-19T12:00:00Z",
  tutorial_completed_at: null,
  tutorial_stage: "run",
  tutorial_session_id: "sess-1",
  tutorial_run_id: null,
  tutorial_source_data_hash: null,
  show_advanced: true,
  updated_at: null,
};

describe("decodeUserComposerPreferences", () => {
  it("accepts the exact payload", () => {
    expect(decodeUserComposerPreferences(full)).toEqual(full);
  });
  it("rejects an omitted show_advanced instead of loading undefined (elspeth-7d07df6438)", () => {
    const { show_advanced: _omitted, ...without } = full;
    expect(() => decodeUserComposerPreferences(without)).toThrow(/missing show_advanced/);
  });
  it("rejects a non-boolean show_advanced, an unknown mode, an unknown stage, and an extra key", () => {
    expect(() => decodeUserComposerPreferences({ ...full, show_advanced: "yes" })).toThrow(/show_advanced/);
    expect(() => decodeUserComposerPreferences({ ...full, default_mode: "wizard" })).toThrow(/default_mode/);
    expect(() => decodeUserComposerPreferences({ ...full, tutorial_stage: "welcome" })).toThrow(/tutorial_stage/);
    expect(() => decodeUserComposerPreferences({ ...full, extra: 1 })).toThrow(/unexpected extra/);
  });
  it("rejects a non-object payload", () => {
    // exactRecord's "expected object" arm. A 401 interceptor or a proxy can
    // hand back null, an array or an empty body; none of those is a payload.
    for (const value of [null, [], "", 0]) {
      expect(() => decodeUserComposerPreferences(value)).toThrow(/expected object/);
    }
  });
  it("accepts tutorial_stage: null — the COMMON production shape", () => {
    // No tutorial in progress. This is the one branch of the enum guard the
    // negative cases never walk (`stage !== null && ...`), and it is what
    // most real accounts send.
    const noTutorial = { ...full, tutorial_stage: null, tutorial_session_id: null };
    expect(decodeUserComposerPreferences(noTutorial)).toEqual(noTutorial);
  });
});
