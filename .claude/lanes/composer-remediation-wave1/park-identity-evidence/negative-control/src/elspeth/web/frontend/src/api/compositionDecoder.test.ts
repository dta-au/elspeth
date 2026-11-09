import { describe, expect, it } from "vitest";
import { decodeCompositionState } from "./compositionDecoder";

describe("composition state decoder", () => {
  it("rejects a malformed state on the freeform API path", () => {
    expect(() => decodeCompositionState({ id: "state" })).toThrow(/missing session_id/);
  });
});
