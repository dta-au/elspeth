// stores/validationRequests.ts
//
// Late-binding seam between sessionStore and the auto-validation queue in
// subscriptions.ts. sessionStore must not import subscriptions (subscriptions
// imports sessionStore; see initStoreSubscriptions), so the queue registers
// its resolve entry point here at startup and sessionStore calls through it.
// This module imports no store, so it adds no edge to the import graph.

export interface ResolveValidateRequest {
  sessionId: string;
  version: number;
  /**
   * True when the resolve returned a new composition state (accept/amend),
   * so `version` was minted by the resolve itself. False for the opt-out
   * path, which mints no state and re-validates the unchanged version.
   */
  mintedVersion: boolean;
}

type ResolveValidateRequester = (request: ResolveValidateRequest) => void;

let resolveValidateRequester: ResolveValidateRequester | null = null;

/** Called by initStoreSubscriptions(); pass null on teardown. */
export function registerResolveValidateRequester(
  requester: ResolveValidateRequester | null,
): void {
  resolveValidateRequester = requester;
}

/**
 * Hands a post-resolve validation to the queue. Returns false when no queue
 * is registered (initStoreSubscriptions() never ran), so the caller can fall
 * back to validating directly — with no queue there is nothing to bypass.
 */
export function requestValidateAfterResolve(
  request: ResolveValidateRequest,
): boolean {
  if (resolveValidateRequester === null) return false;
  resolveValidateRequester(request);
  return true;
}
