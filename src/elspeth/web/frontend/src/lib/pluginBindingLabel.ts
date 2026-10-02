import { llmBindingLabel } from "./llmBindingLabel";

const SAFE_PROFILE_ALIAS = /^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$/;
const SAFE_DOCUMENT_MODEL_ID = /^[A-Za-z0-9][A-Za-z0-9._~-]{1,63}$/;

/** Display only authored profile aliases, never resolved operator bindings. */
export function pluginBindingLabel(plugin: string | null, options: Record<string, unknown>): string | null {
  if (plugin === "llm") return llmBindingLabel(options);
  if (plugin === "azure_document_intelligence") {
    const model = options.model_id;
    return typeof model === "string" && SAFE_DOCUMENT_MODEL_ID.test(model)
      ? `model ${model}`
      : "configured model";
  }
  if (!Object.prototype.hasOwnProperty.call(options, "profile")) return null;
  const profile = options.profile;
  return typeof profile === "string" && SAFE_PROFILE_ALIAS.test(profile)
    ? `profile ${profile}`
    : "configured profile";
}
