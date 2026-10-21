import { describe, expect, it } from "vitest";
import { decodeCompositionState } from "./compositionDecoder";
import fixture from "../../../../../../tests/fixtures/web/composer/composition_state_validation_errors.json";

describe("composition state decoder", () => {
  it("rejects a malformed state on the freeform API path", () => {
    expect(() => decodeCompositionState({ id: "state" })).toThrow(/missing session_id/);
  });

  it("preserves the server checksum without hashing the redacted response", () => {
    const state = Object.values(fixture.states)[0];
    const checksum = "a".repeat(64);
    expect(decodeCompositionState({ ...state, composition_content_hash: checksum }).composition_content_hash).toBe(checksum);
  });

  it("accepts an older server response without claiming a checksum", () => {
    expect(decodeCompositionState(Object.values(fixture.states)[0]).composition_content_hash).toBeUndefined();
  });

  it("preserves the null-metadata read contract without claiming a checksum", () => {
    const state = Object.values(fixture.states)[0];
    expect(decodeCompositionState({ ...state, metadata: null, composition_content_hash: null }).composition_content_hash).toBeUndefined();
  });

  it.each([null, "", "a".repeat(63), "A".repeat(64), "g".repeat(64), "a".repeat(64) + "\n", 123])("rejects a malformed supplied checksum %s", (checksum) => {
    expect(() => decodeCompositionState({ ...Object.values(fixture.states)[0], composition_content_hash: checksum })).toThrow(/composition_content_hash/);
  });
});
