/**
 * The scroll-owner VOCABULARY for the composer workspace.
 *
 * Scrolling inside the workspace is a capability that is granted and named,
 * never an ambient property a box picks up. The grant lives in CSS: the rule
 * that declares `overflow*: auto` also declares `--scroll-owner: <name>`, in
 * the same block, so the grant and its name are one act in one place and
 * cannot drift apart (elspeth-73849d9d16 — the predecessor of this module was
 * a selector list in the e2e helpers, and `.ack-stack` had already quietly
 * diverged from it when d5f53fde0 landed). `--scroll-owner` is registered in
 * workspace.css with `inherits: false`; without that registration custom
 * properties inherit, and a nested unnamed scroller would borrow its
 * ancestor's name.
 *
 * This module owns only the NAMES and their meaning — the closed vocabulary
 * the e2e gate enforces caps over. It deliberately holds no selectors: which
 * box carries which name is the stylesheet's statement, read off the DOM at
 * gate time. A scroller with no name is a defect by construction; a name
 * outside this list is a defect by construction. Adding a name here is a
 * deliberate act: give a new scroll region its own entry rather than reusing
 * an existing one, so the one-owner-per-name cap keeps meaning what it says.
 * `authoring-dock` and `authoring-ack` exist exactly because the authoring
 * pane legitimately shows three scroll regions at once; folding them into
 * `authoring` would trip the cap in a state the layout permits.
 */
export const WORKSPACE_SCROLL_OWNERS = [
  // The conversation transcript. Bound by BOTH `.chat-panel-messages`
  // (freeform) and `.guided-authoring-scroll` (guided): the two mount
  // exclusively, so one name — and the cap holds the exclusivity.
  "authoring",
  // Docked chrome between transcript and composer (elspeth-ecf973fb9f). A
  // scroll container BY DESIGN: that is what zeroes its automatic minimum
  // size so it, rather than the composer, absorbs a vertical deficit.
  "authoring-dock",
  // Pinned acknowledgement stack, capped at 45vh with its own internal
  // scroll (chat.css).
  "authoring-ack",
  // The active artifact tab panel — the one owner that may also scroll
  // horizontally (wide YAML, wide graphs).
  "artifact",
  // The inspector drawer's body.
  "inspector",
] as const;

export type WorkspaceScrollOwner = (typeof WORKSPACE_SCROLL_OWNERS)[number];
