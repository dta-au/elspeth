import type { SafeRequestRejection } from "@/types";

/** Render admitted primary evidence without using provider/debug prose. */
export function formatRequestRejection(rejection: SafeRequestRejection, context: "composer" | "run_diagnostics"): string {
  let explanation: string;
  switch (rejection.reason_code) {
    case "context_length_exceeded":
      explanation = "The request exceeded the model's context limit. Reduce the review context before trying again.";
      break;
    case "unsupported_parameter":
      explanation = "The request included an unsupported parameter. Check the provider request configuration before trying again.";
      break;
    case "invalid_request":
      explanation = "The provider rejected invalid request input. A more specific rejection reason is unavailable.";
      break;
    default:
      if (rejection.reason_disposition === "absent") {
        explanation = "The provider rejected the request. No structured reason code was supplied in the inspected error envelope.";
      } else if (rejection.reason_disposition === "unrecognized") {
        explanation = "The provider rejected the request. Its structured reason could not be safely recognized.";
      } else {
        explanation = "The provider rejected the request. The structured rejection reason is unavailable.";
      }
  }
  const prefix = context === "composer" ? "The Composer request was rejected." : "The run diagnostics request was rejected.";
  const reference = rejection.diagnostic_reference === null ? "" : ` Diagnostic reference: ${rejection.diagnostic_reference}.`;
  return `${prefix} ${explanation}${reference}`;
}
