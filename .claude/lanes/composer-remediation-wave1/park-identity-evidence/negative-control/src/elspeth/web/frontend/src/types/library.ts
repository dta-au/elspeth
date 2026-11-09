export type LibraryView = "accepted" | "mine" | "queue";
export type LibraryEntryState = "pending" | "accepted" | "rejected" | "deprecated" | "recalled";
export type LibraryCuratorAction = "accept" | "reject" | "deprecate" | "recall";

export interface LibraryEntry {
  entry_id: string;
  published_from_session_id: string | null;
  payload_digest: string;
  compartment_id: string;
  title: string;
  version: number;
  published_by_identity_id: string;
  curated_by_identity_id: string | null;
  published_at: string;
  accepted_at: string | null;
  rejected_at: string | null;
  rejection_note: string | null;
  deprecated_at: string | null;
  recalled_at: string | null;
  note: string | null;
  state: LibraryEntryState;
}

export interface LibraryList {
  view: LibraryView;
  entries: LibraryEntry[];
}

export interface LibraryFork {
  session_id: string;
  state_id: string;
}
