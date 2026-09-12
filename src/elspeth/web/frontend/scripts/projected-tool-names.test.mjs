import assert from "node:assert/strict";
import test from "node:test";
import { projectedToolNames } from "./projected-tool-names.mjs";

test("reads literal ownership in source order without matching comments", () => {
  assert.deepEqual(projectedToolNames(`
    // case "invented": {}
    export const TOOL_PROJECTORS = {
      set_source: (args) => args,
      "clear_source": () => null,
    } satisfies Record<string, ToolProjector>;
  `), ["set_source", "clear_source"]);
});

for (const declaration of [
  "{ ...other }", "{ [name]: () => null }", "{ tool }",
  "{ tool: external }", "factory()", "{}",
  "{ tool: () => null, tool: () => null }", "{ __proto__: () => null }",
]) {
  test(`rejects unsupported ownership: ${declaration}`, () => {
    assert.throws(() => projectedToolNames(`const TOOL_PROJECTORS = ${declaration};`));
  });
}

test("rejects missing and duplicate registries", () => {
  assert.throws(() => projectedToolNames("const other = {};"));
  assert.throws(() => projectedToolNames("const TOOL_PROJECTORS = {}; const TOOL_PROJECTORS = {};"));
});
