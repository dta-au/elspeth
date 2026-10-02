// The server's fixed "review cards are ready" notice, which MessageBubble drops
// from a message once its review is no longer pending. The filter matches the
// segment's text exactly, so this string and
// `_INTERPRETATION_REVIEW_HANDOFF_NOTICE` in
// src/elspeth/web/composer/no_tool_policy.py are one fact in two languages.
// pendingReviewNotice.test.ts reads both: reword one without the other and the
// stale notice comes back, silently, which is what the test is for.
export const PENDING_REVIEW_NOTICE =
  "Interpretation review cards are ready for this pipeline. Review the pending assumptions to continue.";
