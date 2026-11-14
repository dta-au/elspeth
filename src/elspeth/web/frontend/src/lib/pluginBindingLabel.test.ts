import { describe, expect, it } from "vitest";
import { pluginBindingLabel } from "./pluginBindingLabel";

describe("pluginBindingLabel", () => {
  it("retains LLM profile and direct model labels", () => {
    expect(pluginBindingLabel("llm", { profile: "sonnet" })).toBe("profile sonnet");
    expect(pluginBindingLabel("llm", { model: "provider/model" })).toBe("model provider/model");
  });

  it("does not display private binding fields or unrelated models", () => {
    expect(pluginBindingLabel("aws_s3", { profile_alias: "private", bucket: "private-bucket" })).toBeNull();
    expect(pluginBindingLabel("other", { model: "private-model" })).toBeNull();
    expect(pluginBindingLabel("csv", {})).toBeNull();
  });

  it("shows the public Azure Document Intelligence model selection", () => {
    expect(pluginBindingLabel("azure_document_intelligence", { model_id: "prebuilt-layout" })).toBe("model prebuilt-layout");
    expect(pluginBindingLabel("azure_document_intelligence", { model_id: "https://private.example" })).toBe("configured model");
    expect(pluginBindingLabel("azure_content_understanding", { analyzer_id: "prebuilt-invoice" })).toBe("analyzer prebuilt-invoice");
    expect(pluginBindingLabel("azure_content_understanding", { analyzer_id: "https://private.example" })).toBe("configured analyzer");
    expect(pluginBindingLabel("azure_content_understanding", {})).toBe("configured analyzer");
  });

  it.each(["arn:aws:private", "https://private.example", "bad profile", 42, null])(
    "does not display malformed profile values: %s", (profile) => {
      expect(pluginBindingLabel("aws_s3", { profile })).toBe("configured profile");
    },
  );
});
