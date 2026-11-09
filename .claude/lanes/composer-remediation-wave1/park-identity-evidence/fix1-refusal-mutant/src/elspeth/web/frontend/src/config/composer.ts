export const COMPOSE_LOADING_SESSION_MESSAGE = "Loading this session…";
export const COMPOSE_CLIENT_GRACE_MS = 25_000;

/** The request may have taken effect; recovery must read before writing. */
export const COMPOSE_OUTCOME_UNCONFIRMED = "compose_outcome_unconfirmed";

export function isComposeRefreshOnlyFailureCode(code: string | undefined): boolean {
  return code === COMPOSE_OUTCOME_UNCONFIRMED ||
    code === "recompose_saved_proposal" ||
    code === "recompose_already_completed";
}

export const COMPOSE_TIMEOUT_ABORT_REASON = "compose_timeout";
export const COMPOSE_USER_CANCEL_ABORT_REASON = "compose_user_cancel";

export function isComposePermanentRefusal(code: string | undefined): boolean {
  return ["policy_blocked", "admission_refused", "token_accounting_unavailable", "composer_operation_conflict", "recompose_user_message_mismatch"].includes(code ?? "");
}
