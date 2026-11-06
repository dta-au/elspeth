// Observer errors the store tells apart with instanceof.
export class ComposerObservationDetached extends Error { constructor() { super("Composer observation detached; the durable action remains pending"); this.name = "ComposerObservationDetached"; } }
export class ComposerSessionMissing extends Error { constructor() { super("Session not found"); this.name = "ComposerSessionMissing"; } }
