import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type FocusEvent,
  type KeyboardEvent,
} from "react";
import { ConfirmDialog } from "@/components/common/ConfirmDialog";
import { Button } from "@/components/ui";
import {
  buildVersionRows,
  type VersionListRow,
} from "@/components/header/versionGrouping";
import {
  deriveVersionLabel,
  versionLabelKind,
  versionOperationIdentifier,
  isSnapshotOnly,
} from "@/components/header/versionLabels";
import { useShowAdvanced } from "@/stores/preferencesStore";
import { useSessionStore } from "@/stores/sessionStore";
import type { CompositionStateVersion } from "@/types/index";
import { plural } from "@/utils/plural";
import { relativeTime } from "@/utils/time";

export function HeaderVersionSelector(): JSX.Element | null {
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const compositionState = useSessionStore((s) => s.compositionState);
  const messages = useSessionStore((s) => s.messages);
  const stateVersions = useSessionStore((s) => s.stateVersions);
  const isLoadingVersions = useSessionStore((s) => s.isLoadingVersions);
  const loadStateVersions = useSessionStore((s) => s.loadStateVersions);
  const revertToVersion = useSessionStore((s) => s.revertToVersion);
  const showAdvanced = useShowAdvanced();

  const [isOpen, setIsOpen] = useState(false);
  // Roving cursor over the FLATTENED tree order (a group, then its members
  // when expanded) — not over sortedVersions, which stops being the visual
  // order the moment a group forms.
  const [focusedIndex, setFocusedIndex] = useState(-1);
  // Selection is keyed by version NUMBER, never by list index: expanding or
  // collapsing a group changes the row count, and an index-addressed
  // selection would silently re-aim Revert — a destructive, audit-visible
  // action — at whatever row slid into that slot (elspeth-c8a402a9a4).
  const [selectedVersionNumber, setSelectedVersionNumber] = useState<
    number | null
  >(null);
  const [expandedGroups, setExpandedGroups] = useState<ReadonlySet<string>>(
    new Set(),
  );
  const [revertTarget, setRevertTarget] =
    useState<CompositionStateVersion | null>(null);
  const treeId = useId();
  const containerRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLUListElement>(null);

  const currentVersion = compositionState?.version ?? null;

  // Adjacent predecessor by version number, from the FULL fetched list —
  // never the filtered one, or every row after a hidden snapshot would be
  // classified against the wrong neighbour. With >50 versions the window's
  // oldest row has no predecessor and isSnapshotOnly honestly returns false.
  const findPredecessor = (
    version: CompositionStateVersion,
  ): CompositionStateVersion | undefined =>
    stateVersions.find(
      (candidate) => candidate.version === version.version - 1,
    );

  const sortedVersions: CompositionStateVersion[] = [];
  if (currentVersion !== null) {
    const currentEntry = stateVersions.find(
      (version) => version.version === currentVersion,
    );
    if (currentEntry) {
      sortedVersions.push(currentEntry);
    } else {
      sortedVersions.push({
        id: "",
        version: currentVersion,
        created_at: new Date().toISOString(),
        node_count: compositionState?.nodes.length ?? 0,
      });
    }
    stateVersions
      .filter((version) => version.version !== currentVersion)
      // Snapshot-only rows offer nothing a user can decide on, so they are
      // hidden. Only on POSITIVE evidence: any row the projection cannot
      // prove unchanged stays visible. Version numbers keep honest gaps —
      // they must agree with the chat revert message and the audit trail.
      // The current version is exempt above: it anchors the trigger.
      .filter((version) => !isSnapshotOnly(version, findPredecessor(version)))
      .sort((left, right) => right.version - left.version)
      .forEach((version) => sortedVersions.push(version));
  }

  // Grouping keys on the STRUCTURAL kind, never on the visible copy: a
  // register rewrite of the word "Edited" must not be able to turn grouping
  // off. Snapshot-only rows report their own kind so they can never be
  // folded into an "edited" run.
  const kindFor = (version: CompositionStateVersion) =>
    isSnapshotOnly(version, findPredecessor(version))
      ? ("snapshot" as const)
      : versionLabelKind(version, stateVersions, messages);
  const rows = buildVersionRows(
    sortedVersions,
    kindFor,
    currentVersion,
    showAdvanced,
    expandedGroups,
  );

  // ONE traversal builds both the flattened focus order and the per-row
  // render indices, so document order and `focusOrder` cannot drift apart —
  // which is what makes the [role='treeitem'] scroll-into-view arithmetic
  // and aria-activedescendant valid.
  const focusOrder: Array<{
    row: VersionListRow;
    member?: CompositionStateVersion;
    /** Focus index of the owning group row — members only. ArrowLeft on a
     *  member moves focus to its parent, per the WAI-ARIA tree pattern. */
    parentIndex?: number;
  }> = [];
  const renderEntries: Array<{
    row: VersionListRow;
    index: number;
    memberIndices: number[];
  }> = [];
  for (const row of rows) {
    const index = focusOrder.length;
    focusOrder.push({ row });
    const memberIndices: number[] = [];
    if (row.kind === "group" && row.expanded) {
      for (const member of row.versions) {
        memberIndices.push(focusOrder.length);
        focusOrder.push({ row, member, parentIndex: index });
      }
    }
    renderEntries.push({ row, index, memberIndices });
  }

  const focusedVersion = (index: number): CompositionStateVersion | null => {
    const entry = focusOrder[index];
    if (entry === undefined) {
      return null;
    }
    if (entry.member !== undefined) {
      return entry.member;
    }
    return entry.row.kind === "version" ? entry.row.version : null;
  };

  const selectedVersion =
    selectedVersionNumber === null
      ? null
      : (sortedVersions.find(
          (version) => version.version === selectedVersionNumber,
        ) ?? null);
  const canRevertSelected =
    selectedVersion !== null && selectedVersion.version !== currentVersion;
  // Scalars, not the freshly-built arrays, so the effects below have stable
  // dependencies.
  const focusCount = focusOrder.length;
  const selectionIsStale =
    selectedVersionNumber !== null && selectedVersion === null;

  const toggle = useCallback(() => {
    setIsOpen((prev) => {
      const next = !prev;
      if (next) {
        void loadStateVersions();
        setFocusedIndex(0);
        setSelectedVersionNumber(currentVersion);
      }
      return next;
    });
  }, [loadStateVersions, currentVersion]);

  const close = useCallback(() => {
    setIsOpen(false);
    setFocusedIndex(-1);
    triggerRef.current?.focus();
  }, []);

  // Focus leaving the selector subtree closes the dropdown
  // (elspeth-83eb51334f): a keyboard user could previously Tab past the
  // trigger while the tree stayed visually open. Mirrors UserMenu: only
  // a real relatedTarget outside the container closes; null relatedTargets
  // are left to the click-outside handler so in-dropdown clicks are never
  // swallowed. No focus-return — focus is already moving somewhere else
  // deliberately.
  const onContainerBlur = useCallback((e: FocusEvent<HTMLDivElement>) => {
    const next = e.relatedTarget;
    if (
      next instanceof Node &&
      containerRef.current !== null &&
      !containerRef.current.contains(next)
    ) {
      setIsOpen(false);
      setFocusedIndex(-1);
    }
  }, []);

  useEffect(() => {
    if (!isOpen) return;

    function handleMouseDown(e: MouseEvent) {
      if (
        containerRef.current &&
        !containerRef.current.contains(e.target as Node)
      ) {
        setIsOpen(false);
        setFocusedIndex(-1);
      }
    }

    document.addEventListener("mousedown", handleMouseDown);
    return () => document.removeEventListener("mousedown", handleMouseDown);
  }, [isOpen]);

  useEffect(() => {
    if (isOpen) {
      listRef.current?.focus();
    }
  }, [isOpen]);

  useEffect(() => {
    if (!isOpen || focusedIndex < 0) return;
    const items = listRef.current?.querySelectorAll("[role='treeitem']");
    items?.[focusedIndex]?.scrollIntoView?.({ block: "nearest" });
  }, [isOpen, focusedIndex]);

  // Collapsing a group (or a shorter fetched window) can leave the roving
  // cursor past the end of the tree.
  useEffect(() => {
    if (focusedIndex >= focusCount) {
      setFocusedIndex(focusCount > 0 ? 0 : -1);
    }
  }, [focusedIndex, focusCount]);

  // A selected version that is no longer in the list (a refetch dropped it)
  // falls back to the current version rather than to whatever now occupies
  // its former position.
  useEffect(() => {
    if (selectionIsStale) {
      setSelectedVersionNumber(currentVersion);
    }
  }, [selectionIsStale, currentVersion]);

  if (!activeSessionId || currentVersion === null) {
    return null;
  }

  function handleTriggerKeyDown(e: KeyboardEvent<HTMLButtonElement>) {
    if (e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      if (!isOpen) {
        toggle();
      }
    }
  }

  function toggleGroup(id: string) {
    setExpandedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }

  function handleListKeyDown(e: KeyboardEvent<HTMLUListElement>) {
    const count = focusOrder.length;
    if (e.key === "Escape") {
      e.preventDefault();
      close();
      return;
    }
    // Arrow up/down move the CURSOR only. Selection no longer follows focus:
    // an explicit Enter/Space (or click) on a version item is what re-aims
    // Revert, so walking past a row can never change the target.
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (count > 0) {
        setFocusedIndex((prev) => (prev + 1) % count);
      }
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      if (count > 0) {
        setFocusedIndex((prev) => (prev - 1 + count) % count);
      }
      return;
    }
    const entry = focusOrder[focusedIndex];
    if (entry === undefined) {
      return;
    }
    // WAI-ARIA tree pattern: ArrowLeft on a node that is not itself an
    // expanded parent moves focus to its parent. Without this a member is a
    // dead end — the only way back out of an expanded group is ArrowUp past
    // every sibling.
    if (e.key === "ArrowLeft" && entry.parentIndex !== undefined) {
      e.preventDefault();
      setFocusedIndex(entry.parentIndex);
      return;
    }
    if (entry.member === undefined && entry.row.kind === "group") {
      const group = entry.row;
      if (e.key === "ArrowRight" && !group.expanded) {
        e.preventDefault();
        toggleGroup(group.id);
        return;
      }
      if (e.key === "ArrowLeft" && group.expanded) {
        e.preventDefault();
        toggleGroup(group.id);
        return;
      }
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggleGroup(group.id);
      }
      return;
    }
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      const version = focusedVersion(focusedIndex);
      if (version === null) {
        return;
      }
      setSelectedVersionNumber(version.version);
      if (version.version !== currentVersion) {
        setRevertTarget(version);
      }
    }
  }

  function confirmRevert() {
    if (!revertTarget) return;
    void revertToVersion(revertTarget.id);
    setRevertTarget(null);
    close();
  }

  const activeEntry = focusOrder[focusedIndex];
  const activeDescendantId = ((): string | undefined => {
    if (activeEntry === undefined) {
      return undefined;
    }
    if (activeEntry.member !== undefined) {
      return `${treeId}-option-${activeEntry.member.version}`;
    }
    return activeEntry.row.kind === "group"
      ? `${treeId}-group-${activeEntry.row.id}`
      : `${treeId}-option-${activeEntry.row.version.version}`;
  })();

  /** One version row — identical markup at the top level and nested inside
   *  an expanded group; `index` is its slot in the flattened focus order. */
  function renderVersionItem(
    version: CompositionStateVersion,
    index: number,
  ): JSX.Element {
    const isCurrent = version.version === currentVersion;
    const isFocused = focusedIndex === index;
    const nodeCount = version.nodes?.length ?? version.node_count ?? 0;
    // Only the current row can reach here snapshot-only — the filter above
    // hides every other one.
    const snapshotOnly = isSnapshotOnly(version, findPredecessor(version));
    // "no VISIBLE change", not "no pipeline change": isSnapshotOnly
    // compares the redacted wire projection, so the strongest honest claim
    // is about what this surface can see. Do not strengthen the wording
    // without a backend content hash.
    const operationLabel = snapshotOnly
      ? "no visible change"
      : deriveVersionLabel(version, stateVersions, messages);
    const operationTitle = snapshotOnly
      ? undefined
      : (versionOperationIdentifier(version, messages) ?? undefined);
    return (
      <li
        key={version.version}
        id={`${treeId}-option-${version.version}`}
        role="treeitem"
        aria-selected={selectedVersionNumber === version.version}
        aria-label={`Version ${version.version}${
          isCurrent ? " (current)" : ""
        } — ${operationLabel}`}
        className={`version-selector-item${
          isFocused ? " version-selector-item--focused" : ""
        }${isCurrent ? " version-selector-item--current" : ""}${
          snapshotOnly ? " version-selector-item--snapshot" : ""
        }`}
        onClick={(e) => {
          // A member <li> is nested INSIDE its group's <li>, so without this
          // the click would also reach the group's toggle and collapse the
          // row the user just selected. A no-op for top-level rows.
          e.stopPropagation();
          setFocusedIndex(index);
          setSelectedVersionNumber(version.version);
        }}
        onMouseEnter={() => setFocusedIndex(index)}
      >
        <span className="version-selector-item-info">
          <span className="version-selector-item-label">
            v{version.version}
            {isCurrent && (
              <span className="version-selector-item-tag">(current)</span>
            )}
          </span>
          <span className="version-selector-item-meta">
            {plural(nodeCount, "node")}
          </span>
          <span
            className="version-selector-item-meta version-selector-item-op"
            title={operationTitle}
          >
            {operationLabel}
          </span>
          <span className="version-selector-item-meta">
            {relativeTime(version.created_at)}
          </span>
        </span>
      </li>
    );
  }

  return (
    <div
      ref={containerRef}
      className="version-selector header-version-selector"
      onBlur={onContainerBlur}
    >
      <Button
        compact
        ref={triggerRef}
        aria-haspopup="tree"
        aria-expanded={isOpen}
        aria-controls={treeId}
        aria-label={`Composition history (currently v${currentVersion})`}
        onClick={toggle}
        onKeyDown={handleTriggerKeyDown}
        // Chrome-row rung, not the 44px canvas rung: this trigger lives in the
        // 40px .app-header band (--size-header-height), so .btn's
        // min-height: var(--size-control) overflowed it by 2px at every
        // viewport taller than 800px — the only regime the header.css compact
        // override did NOT cover (elspeth-2d29ccf56e). `compact` composes
        // .btn-compact per the tokens.css:248 rule rather than redeclaring a
        // literal min-height on .version-selector-trigger.
        className="version-selector-trigger"
      >
        v{currentVersion} <span aria-hidden="true">▾</span>
      </Button>

      {isOpen && (
        <div className="version-selector-dropdown">
          <ul
            ref={listRef}
            id={treeId}
            role="tree"
            aria-label="Composition history"
            aria-activedescendant={activeDescendantId}
            onKeyDown={handleListKeyDown}
            tabIndex={0}
            className="version-selector-list"
          >
            {isLoadingVersions && sortedVersions.length === 0 && (
              <li className="version-selector-loading">Loading versions...</li>
            )}
            {renderEntries.map(({ row, index, memberIndices }) => {
              if (row.kind === "version") {
                return renderVersionItem(row.version, index);
              }
              const numbers = row.versions.map((member) => member.version);
              const low = Math.min(...numbers);
              const high = Math.max(...numbers);
              const isFocused = focusedIndex === index;
              const editsLabel = plural(row.versions.length, "edit");
              return (
                <li
                  key={row.id}
                  id={`${treeId}-group-${row.id}`}
                  role="treeitem"
                  aria-expanded={row.expanded}
                  // A group is never a revert target — it stands for a run of
                  // versions, and Revert acts on exactly one.
                  aria-selected={false}
                  aria-label={`Versions ${low} to ${high} — ${editsLabel}`}
                  className={`version-selector-item version-selector-group${
                    isFocused ? " version-selector-item--focused" : ""
                  }`}
                  onClick={() => {
                    setFocusedIndex(index);
                    toggleGroup(row.id);
                  }}
                  onMouseEnter={() => setFocusedIndex(index)}
                >
                  <span className="version-selector-item-info">
                    <span className="version-selector-item-label">
                      v{low}–v{high}
                    </span>
                    <span className="version-selector-item-meta">
                      {editsLabel}
                    </span>
                    <span className="version-selector-item-meta">
                      {relativeTime(row.versions[0].created_at)}
                    </span>
                    <span aria-hidden="true">{row.expanded ? "▾" : "▸"}</span>
                  </span>
                  {row.expanded && (
                    <ul role="group" className="version-selector-group-members">
                      {row.versions.map((member, ordinal) =>
                        renderVersionItem(member, memberIndices[ordinal]),
                      )}
                    </ul>
                  )}
                </li>
              );
            })}
          </ul>
          <div className="version-selector-actions">
            <Button
              type="button"
              className="version-selector-revert-btn"
              disabled={!canRevertSelected}
              aria-label={
                canRevertSelected
                  ? `Revert to version ${selectedVersion.version}`
                  : "Select a previous version to revert"
              }
              onClick={() => {
                if (canRevertSelected) {
                  setRevertTarget(selectedVersion);
                }
              }}
            >
              {canRevertSelected
                ? `Revert to v${selectedVersion.version}`
                : "Current version selected"}
            </Button>
          </div>
        </div>
      )}

      {revertTarget && (
        <ConfirmDialog
          title="Revert pipeline"
          message={`Revert pipeline to version ${revertTarget.version}? This will replace the current composition.`}
          confirmLabel="Revert"
          variant="danger"
          onConfirm={confirmRevert}
          onCancel={() => setRevertTarget(null)}
        />
      )}
    </div>
  );
}
