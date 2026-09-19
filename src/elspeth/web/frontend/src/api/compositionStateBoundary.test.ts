import { describe, expect, it } from "vitest";

import fixture from "../../../../../../tests/fixtures/web/composer/composition_state_validation_errors.json";
import { decodeCompositionState, decodeCompositionStateVersions } from "./guidedDecoder";

describe("composition state decoder boundaries", () => {
  it("keeps ordinary metadata values without retaining the caller's objects", () => {
    const state = structuredClone(fixture.states.coded);
    const metadata = { repair_turns_used: 2, independent_key: { mixed: [null, true, 1, "odd ☃"] } };

    const decoded = decodeCompositionState({ ...state, composer_meta: metadata });

    expect(decoded.composer_meta).toEqual(metadata);
    metadata.independent_key.mixed.push("changed");
    expect(decoded.composer_meta).not.toEqual(metadata);
  });

  it("preserves exact error strings, including empty and unfamiliar values", () => {
    const errors = [{ message: "", error_code: "not-enumerated: ☃", component: "" }];

    expect(decodeCompositionState({ ...fixture.states.coded, validation_errors: errors }).validation_errors).toEqual(errors);
  });

  it.each(["message", "error_code", "component"])("rejects a later error missing %s", (key) => {
    const incomplete: Record<string, unknown> = { ...fixture.states.coded.validation_errors[0] };
    delete incomplete[key];

    expect(() => decodeCompositionState({
      ...fixture.states.coded,
      validation_errors: [fixture.states.coded.validation_errors[0], incomplete],
    })).toThrow(/validation_errors/);
  });

  it("rejects a malformed later version rather than returning a valid prefix", () => {
    expect(() => decodeCompositionStateVersions([
      fixture.states.coded,
      { ...fixture.states.empty, validation_errors: ["legacy"] },
    ])).toThrow(/composition_states\[1\].validation_errors/);
  });
});
