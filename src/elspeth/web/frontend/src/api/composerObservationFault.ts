export type ComposerObservationFaultCode = "progress_callback_failed" | "ack_identity_mismatch" | "snapshot_kind_mismatch" | "cancel_kind_mismatch";

/** A local observation failure says nothing about the durable action's outcome. */
export class ComposerObservationFault extends Error {
  declare readonly cause: unknown;
  constructor(readonly code: ComposerObservationFaultCode, cause: unknown) {
    super("Composer observation stopped before the outcome could be confirmed");
    this.name = "ComposerObservationFault";
    Object.defineProperty(this, "cause", { value: cause, enumerable: false });
  }
}

/** Classify by the callback operation, including non-Error thrown values. */
export function deliverComposerObservation(callback: () => void): void {
  try { callback(); }
  catch (cause) { throw new ComposerObservationFault("progress_callback_failed", cause); }
}
