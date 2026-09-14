const SAFE_LLM_PROFILE_ALIAS = /^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$/;
const SAFE_LLM_MODEL_IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,511}$/;

/** Show authored bindings only. Operator profile resolution stays private. */
export function llmBindingLabel(options: Record<string, unknown>): string {
  if (Object.prototype.hasOwnProperty.call(options, "profile")) {
    const profile = options.profile;
    return typeof profile === "string" && SAFE_LLM_PROFILE_ALIAS.test(profile)
      ? `profile ${profile}`
      : "configured LLM";
  }
  if (
    Object.prototype.hasOwnProperty.call(options, "profile_alias") ||
    Object.prototype.hasOwnProperty.call(options, "resolved_model")
  ) {
    return "configured LLM";
  }
  const model = options.model;
  return typeof model === "string" && SAFE_LLM_MODEL_IDENTIFIER.test(model)
    ? `model ${model}`
    : "configured LLM";
}
