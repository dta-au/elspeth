// Shared by the v1 and v2 observers so the store's instanceof checks see both.
export class ComposerObservationDetached extends Error { constructor() { super("Composer observation detached; the durable action remains pending"); this.name = "ComposerObservationDetached"; } }
export class ComposerSessionMissing extends Error { constructor() { super("Session not found"); this.name = "ComposerSessionMissing"; } }
