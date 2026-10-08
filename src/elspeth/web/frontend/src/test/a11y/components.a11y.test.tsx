// ============================================================================
// components.a11y.test.tsx — component accessibility audit
//
// One axe-core pass per user-facing component. Originally the Phase 8 Task 7
// audit of the Phase-1-through-8 composer components; widened by the
// 2026-07-02 UX-review regression net (elspeth-adf5e679e7) to cover the
// tutorial surface, the auth surface, and the chrome/run components that
// review touched. The matcher `toHaveNoViolations` is registered globally by
// `setup.ts` (registered in vite.config.ts's `test.setupFiles`); do NOT call
// `expect.extend(...)` in this file — that would shadow the global
// registration and break the "register once" invariant. See `setup.ts` head
// comment for the rationale.
//
// Coverage is anchored by the AUDITED_COMPONENTS array. The first test in
// this file is a snapshot assertion that exits the build with a clear
// failure if a future PR adds or removes an audited component without
// updating the audit list — preventing silent erosion of the a11y safety
// net.
// ============================================================================

import { describe, it, expect, afterEach, beforeEach, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef, type ButtonHTMLAttributes, type ReactNode } from "react";

import { axe } from "./axe-config";

// --- Audit-surface coverage snapshot ---------------------------------------
//
// AUDITED_COMPONENTS is the source of truth for "what this suite audits".
// EXPECTED_AUDITED_COMPONENTS_SORTED is the snapshot it is compared against;
// the two are intentionally separate so a code-review can review the
// snapshot change as a deliberate audit-scope update.

const AUDITED_COMPONENTS = [
  "ComposerPreferencesPanel",
  "UserMenu",
  "AuditReadinessPanel",
  "ReadinessRowDetail",
  "ExplainDialog",
  "AppHeader",
  "HeaderSessionSwitcher",
  "HeaderVersionSelector",
  "GraphMiniView",
  "InlineSourceCreatedTurn",
  "InlineSourceFallbackPrompt",
  "CompletionBar",
  "PluginCard",
  "FilterChipStrip",
  "FreeformIntroduction",
  "ShortcutsHelp",
  "PipelineGloss",
  "PipelineValidationSummary",
  // 2026-07-02 UX-review regression net (elspeth-adf5e679e7): the tutorial
  // surface, the acknowledgement cards it leans on, the auth surface, and
  // the chrome/run components the review epic touched. Added centrally by
  // the wave-3 a11y-net pass — component waves deliberately do not edit
  // this list.
  "AcknowledgementCard",
  "AcknowledgementStack",
  "ChatInput",
  "CommandPalette",
  "ConfirmDialog",
  "WorkspaceSeparator",
  "GraphModal",
  "GraphView",
  "HelloWorldTutorial",
  "LoginPage",
  "ProgressView",
  "RecoveryPanel",
  "RunsHistoryDrawer",
  "TutorialFreeformShell",
  "TutorialTurn1Welcome",
  "TutorialTurn4Run",
  "TutorialTurn5AuditStory",
  "TutorialTurn7Graduation",
  // The tutorial's Build step uses the ordinary freeform authoring surface.
  "ChatPanelFreeformTutorialWorkspace",
  // Run-lifecycle feedback (elspeth-3a7b7c7b37): the app-level terminal-run
  // toast is the only completion surface mounted outside the Run panel.
  "RunOutcomeNotice",
] as const;

const EXPECTED_AUDITED_COMPONENTS_SORTED: readonly string[] = [
  "AcknowledgementCard",
  "AcknowledgementStack",
  "AppHeader",
  "AuditReadinessPanel",
  "ChatInput",
  "ChatPanelFreeformTutorialWorkspace",
  "CommandPalette",
  "ComposerPreferencesPanel",
  "CompletionBar",
  "ConfirmDialog",
  "ExplainDialog",
  "FilterChipStrip",
  "GraphMiniView",
  "GraphModal",
  "GraphView",
  "HeaderSessionSwitcher",
  "HeaderVersionSelector",
  "HelloWorldTutorial",
  "InlineSourceCreatedTurn",
  "InlineSourceFallbackPrompt",
  "LoginPage",
  "PipelineGloss",
  "PipelineValidationSummary",
  "PluginCard",
  "ProgressView",
  "ReadinessRowDetail",
  "RecoveryPanel",
  "RunOutcomeNotice",
  "RunsHistoryDrawer",
  "ShortcutsHelp",
  "FreeformIntroduction",
  "TutorialFreeformShell",
  "TutorialTurn1Welcome",
  "TutorialTurn4Run",
  "TutorialTurn5AuditStory",
  "TutorialTurn7Graduation",
  "UserMenu",
  "WorkspaceSeparator",
];

describe("audit surface — coverage snapshot", () => {
  it("audits exactly the expected component list", () => {
    expect([...AUDITED_COMPONENTS].sort()).toEqual(
      [...EXPECTED_AUDITED_COMPONENTS_SORTED].sort(),
    );
  });
});

// --- Mocks -----------------------------------------------------------------
//
// AppHeader/UserMenu pull useTheme(); CompletionBar renders ExecuteButton +
// ImportYamlButton which couple to additional stores; YamlView pulls in the
// full YAML rendering pipeline. Stub the heavy/coupled imports so the render
// produces deterministic DOM for axe without exercising unrelated logic.

vi.mock("@/components/inspector/YamlView", () => ({
  YamlView: () => (
    <button type="button" data-testid="yaml-view-stub">
      stub
    </button>
  ),
}));

// Spy-style mock of the HTTP layer (same idiom as ChatPanel.test.tsx): the
// actual module is preserved so exports the audited components merely import
// stay real, while every endpoint an audited component CALLS during a test is
// a vi.fn() the test seeds. jsdom has no backend, so an unseeded call would
// otherwise hit a dead socket.
vi.mock("@/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/client")>();
  return {
    ...actual,
    fetchUserComposerPreferences: vi.fn(),
    updateUserComposerPreferences: vi.fn(),
    fetchSessions: vi.fn(),
    createSession: vi.fn(),
    // Auth surface (LoginPage).
    fetchAuthConfig: vi.fn(),
    login: vi.fn(),
    register: vi.fn(),
    fetchCurrentUser: vi.fn(),
    // Tutorial surface (elspeth-adf5e679e7).
    deleteTutorialOrphans: vi.fn(),
    renameSession: vi.fn(),
    sendTutorialAbandonBeacon: vi.fn(),
    getTutorialSample: vi.fn(),
    runTutorialPipeline: vi.fn(),
    cancelTutorialRun: vi.fn(),
    getRunAuditSummary: vi.fn(),
    // Run surfaces (ChatPanelTutorialWorkspace mounts InlineRunResults,
    // which loads the session's runs on mount).
    fetchRuns: vi.fn(),
    // Interpretation acknowledgements (AcknowledgementCard/Stack).
    listInterpretationEvents: vi.fn(),
    resolveInterpretation: vi.fn(),
    optOutOfInterpretations: vi.fn(),
    getInterpretationOptOutSummary: vi.fn(),
  };
});

vi.mock("../../api/auditReadiness", () => ({
  fetchAuditReadiness: vi.fn(),
  fetchAuditReadinessExplain: vi.fn(),
  validateAuditReadinessSnapshot: vi.fn(),
}));

// GraphView (and GraphModal, which embeds it) render through @xyflow/react,
// which needs real DOM measurement jsdom cannot provide. Stub the flow canvas
// to deterministic DOM the same way GraphView's own unit tests do — the axe
// pass then covers GraphView's real chrome (the keyboard-operable a11y list,
// the role="img" diagram scope, the config panel) rather than third-party
// canvas internals.
vi.mock("@xyflow/react", () => ({
  ReactFlowProvider: ({ children }: { children?: ReactNode }) => (
    <div data-testid="react-flow-provider">{children}</div>
  ),
  ReactFlow: ({
    nodes,
    edges,
    children,
  }: {
    nodes?: Array<{ id: string; data?: { label?: ReactNode } }>;
    edges?: Array<{ id: string; label?: ReactNode }>;
    children?: ReactNode;
  }) => (
    <div data-testid="react-flow">
      {nodes?.map((n) => <div key={n.id}>{n.data?.label}</div>)}
      {edges?.map((e) => <div key={e.id}>{e.label}</div>)}
      {children}
    </div>
  ),
  Background: () => <div data-testid="react-flow-background" />,
  Controls: () => <div data-testid="react-flow-controls" />,
  ControlButton: ({ children, ...props }: ButtonHTMLAttributes<HTMLButtonElement>) => (
    <button type="button" {...props}>{children}</button>
  ),
  MiniMap: () => <div data-testid="minimap" />,
  // GraphView reaches MarkerType at render time (withDirectionMarkers), so it
  // must be mocked even though this file only exercises the accessible-name
  // surface. Handle/Position/BaseEdge stay out: they are only touched inside
  // the NODE_TYPES/EDGE_TYPES components, which the mocked ReactFlow above
  // never invokes.
  MarkerType: { ArrowClosed: "arrowclosed" },
}));
vi.mock("@xyflow/react/dist/style.css", () => ({}));
vi.mock("@dagrejs/dagre", () => ({
  default: {
    graphlib: {
      Graph: class {
        setDefaultEdgeLabel() {}
        setGraph() {}
        setNode() {}
        setEdge() {}
        node() {
          return { x: 0, y: 0 };
        }
        // GraphView's graph layout can read dimensions from the stub.
        graph() {
          return { width: 480, height: 200 };
        }
      },
    },
    layout() {},
  },
}));

// ProgressView's data source — no live socket exists in jsdom. Only
// ProgressView consumes this hook, so the file-wide mock is safe.
vi.mock("@/hooks/useWebSocket", () => ({
  useWebSocket: vi.fn(),
}));

// --- Imports (post-mock) ---------------------------------------------------

import { ComposerPreferencesPanel } from "@/components/settings/ComposerPreferencesPanel";
import { UserMenu } from "@/components/common/UserMenu";
import { AuditReadinessPanel } from "@/components/audit/AuditReadinessPanel";
import { ReadinessRowDetail } from "@/components/audit/ReadinessRowDetail";
import { ExplainDialog } from "@/components/audit/ExplainDialog";
import { AppHeader } from "@/components/common/AppHeader";
import { HeaderSessionSwitcher } from "@/components/sessions/HeaderSessionSwitcher";
import { HeaderVersionSelector } from "@/components/header/HeaderVersionSelector";
import { GraphMiniView } from "@/components/sidebar/GraphMiniView";
import { InlineSourceCreatedTurn } from "@/components/chat/InlineSourceCreatedTurn";
import { InlineSourceFallbackPrompt } from "@/components/chat/InlineSourceFallbackPrompt";
import { CompletionBar } from "@/components/composer/CompletionBar";
import { PluginCard } from "@/components/catalog/PluginCard";
import { FilterChipStrip, type CatalogFilters } from "@/components/catalog/FilterChipStrip";
import { FreeformIntroduction } from "@/components/chat/FreeformIntroduction";
import { ShortcutsHelp } from "@/components/common/ShortcutsHelp";
import { PipelineGloss } from "@/components/chat/PipelineGloss";
import { PipelineValidationSummary } from "@/components/chat/PipelineValidationSummary";
import { AcknowledgementCard } from "@/components/chat/AcknowledgementCard";
import { AcknowledgementStack } from "@/components/chat/AcknowledgementStack";
import { ChatInput } from "@/components/chat/ChatInput";
import { ChatPanel } from "@/components/chat/ChatPanel";
import { LoginPage } from "@/components/auth/LoginPage";
import { CommandPalette } from "@/components/common/CommandPalette";
import { ConfirmDialog } from "@/components/common/ConfirmDialog";
import { WorkspaceSeparator } from "@/components/workspace/WorkspaceSeparator";
import { ProgressView } from "@/components/execution/ProgressView";
import { RunOutcomeNotice } from "@/components/execution/RunOutcomeNotice";
import { RunsHistoryDrawer } from "@/components/execution/RunsHistoryDrawer";
import { GraphView } from "@/components/inspector/GraphView";
import { GraphModal } from "@/components/sidebar/GraphModal";
import { OPEN_GRAPH_MODAL_EVENT } from "@/lib/composer-events";
import { RecoveryPanel } from "@/components/recovery/RecoveryPanel";
import { HelloWorldTutorial } from "@/components/tutorial/HelloWorldTutorial";
import { TutorialFreeformShell } from "@/components/tutorial/TutorialFreeformShell";
import { TutorialTurn1Welcome } from "@/components/tutorial/TutorialTurn1Welcome";
import { TutorialTurn4Run } from "@/components/tutorial/TutorialTurn4Run";
import { TutorialTurn5AuditStory } from "@/components/tutorial/TutorialTurn5AuditStory";
import { TutorialTurn7Graduation } from "@/components/tutorial/TutorialTurn7Graduation";
import { useWebSocket } from "@/hooks/useWebSocket";

import { usePreferencesStore } from "@/stores/preferencesStore";
import { useSessionStore } from "@/stores/sessionStore";
import { useExecutionStore } from "@/stores/executionStore";
import { useAuthStore } from "@/stores/authStore";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { useAuditReadinessStore, getInitialState as getAuditInitialState } from "@/stores/auditReadinessStore";
import * as auditApi from "../../api/auditReadiness";
import * as apiClient from "@/api/client";
import { resetStore } from "@/test/store-helpers";
import type { InlineSourceSummary, ComposerRecoveryError } from "@/types/api";
import type {
  AuthConfig,
  CompositionState,
  PluginSummary,
  Run,
  ValidationReadiness,
} from "@/types/index";
import type { ReadinessRow, AuditReadinessSnapshot } from "@/types/api";
import type { InterpretationEvent } from "@/types/interpretation";
import {
  compositionStateAuthorityFields,
  makeVersionHistory,
} from "@/test/composerFixtures";

// --- Shared store reset ----------------------------------------------------

const READY_READINESS = {
  authoring_valid: true,
  execution_ready: true,
  completion_ready: true,
  blockers: [],
} satisfies ValidationReadiness;

function resetAllStores() {
  resetStore(usePreferencesStore);
  usePreferencesStore.setState({
    loaded: true,
    writing: false,
    writeError: null,
    bootstrapError: null,
  });
  useSessionStore.setState({
    activeSessionId: "sess-a11y",
    sessions: [
      { id: "sess-a11y", title: "Test session", updated_at: "2026-05-19T00:00:00Z" } as never,
    ],
    compositionState: {
      version: 1,
      sources: {},
      nodes: [{ id: "select_columns", node_type: "transform", plugin: "select_columns", options: {} }],
      edges: [],
      outputs: [],
    } as never,
    // R2-F5 (elspeth-139a345050): compositionState alone is ambiguous
    // between "still loading" and "loaded, empty" — GraphMiniView now
    // renders a "Loading pipeline…" skeleton instead of the populated
    // graph unless this is set, which would have silently dropped the
    // GraphMiniView a11y audit down to auditing a bare <span>.
    compositionStateLoaded: true,
    stateVersions: [],
    isLoadingVersions: false,
  } as never);
  useExecutionStore.setState({
    validationResult: null,
    // Run-lifecycle fields (elspeth-3a7b7c7b37). RunOutcomeNotice reads
    // lastRunOutcome, so a test that seeds it and throws before its inline
    // cleanup would otherwise leak a mounted toast into every later test in
    // this large shared file. Baselines belong here, not at a test's tail.
    lastRunOutcome: null,
    activeRunSessionId: null,
  } as never);
  useAuditReadinessStore.setState(getAuditInitialState());
  resetStore(useInterpretationEventsStore);
  resetStore(useAuthStore);
  // Simulate a completed auth boot (loadFromStorage resolved, no stored
  // token) — same convention as LoginPage.test.tsx.
  useAuthStore.setState({ isLoading: false });
}

beforeEach(() => {
  resetAllStores();
  vi.clearAllMocks();
  localStorage.clear();
  sessionStorage.clear();
});

// --- Shared fixtures ---------------------------------------------------------

/**
 * Fully-typed CompositionState (the store's own resetAllStores state is a
 * minimal `as never` cast that GraphView's edge inference cannot walk).
 * One source → one transform → one sink, wired through named connection
 * points, so GraphView renders its accessible node list, the diagram scope,
 * and at least one inferred edge.
 */
function makeFullCompositionState(): CompositionState {
  return {
    id: "state-a11y",
    ...compositionStateAuthorityFields,
    version: 1,
    sources: {
      pages: {
        plugin: "web_scrape",
        options: {},
        on_success: "chain_in",
      },
    },
    nodes: [
      {
        id: "summarise",
        node_type: "transform",
        plugin: "llm_transform",
        input: "chain_in",
        on_success: "results",
        on_error: null,
        options: {},
      },
    ],
    edges: [],
    outputs: [{ name: "results", plugin: "json", options: {} }],
    metadata: { name: "A11y fixture pipeline", description: null },
  };
}

/** Pending interpretation event for the acknowledgement surface. */
function makeInterpretationEvent(
  id: string,
  overrides: Partial<InterpretationEvent> = {},
): InterpretationEvent {
  return {
    id,
    session_id: "sess-a11y",
    composition_state_id: "state-a11y",
    affected_node_id: "summarise",
    tool_call_id: "tool-1",
    user_term: "interesting",
    kind: "vague_term",
    llm_draft: "novel and relevant to the reader",
    accepted_value: null,
    choice: "pending",
    created_at: "2026-07-01T00:00:00Z",
    resolved_at: null,
    actor: "system:composer",
    interpretation_source: "user_approved",
    model_identifier: "anthropic/claude-sonnet-4.6",
    model_version: "20260518",
    provider: "anthropic",
    composer_skill_hash: "0".repeat(64),
    arguments_hash: null,
    hash_domain_version: null,
    runtime_model_identifier_at_resolve: null,
    runtime_model_version_at_resolve: null,
    approved_prompt_artifact_hash: null,
    ...overrides,
  };
}

// --- Per-component audits --------------------------------------------------

describe("ComposerPreferencesPanel", () => {
  it("has no axe violations", async () => {
    const { container } = render(
      <ComposerPreferencesPanel onClose={() => {}} />,
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("UserMenu", () => {
  it("has no axe violations", async () => {
    const { container } = render(
      <UserMenu onOpenSettings={() => {}} onSignOut={() => {}} />,
    );
    expect(await axe(container)).toHaveNoViolations();
  });

  // elspeth-312238838a: the open menu renders a non-focusable identity
  // header (display name + username) when the auth store holds a user.
  // Open the menu so the identity block is actually in the audited DOM.
  it("has no axe violations with the signed-in identity header open", async () => {
    useAuthStore.setState({
      user: {
        user_id: "user-a11y",
        username: "jdoe",
        display_name: "Jane Doe",
        email: null,
        groups: [],
        dev_admin: false,
      },
    });
    const { container } = render(
      <UserMenu onOpenSettings={() => {}} onSignOut={() => {}} />,
    );
    await userEvent.click(screen.getByRole("button", { name: /account/i }));
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("AuditReadinessPanel", () => {
  it("has no axe violations", async () => {
    const snapshot: AuditReadinessSnapshot = {
      session_id: "sess-a11y",
      composition_version: 1,
      checked_at: new Date().toISOString(),
      rows: [
        { id: "validation", label: "Validation", status: "ok", summary: "All checks pass", detail: null, component_ids: [] },
        { id: "plugin_trust", label: "Plugin trust", status: "ok", summary: "All Tier 1/2", detail: null, component_ids: [] },
        { id: "provenance", label: "Provenance", status: "warning", summary: "Identity passthrough", detail: "details", component_ids: [] },
        { id: "retention", label: "Retention", status: "not_applicable", summary: "n/a", detail: null, component_ids: [] },
        { id: "llm_interpretations", label: "LLM interpretations", status: "not_applicable", summary: "n/a", detail: null, component_ids: [] },
        { id: "secrets", label: "Secrets", status: "not_applicable", summary: "n/a", detail: null, component_ids: [] },
      ],
      validation_result: {
        is_valid: true,
        checks: [],
        errors: [],
        warnings: [],
        readiness: READY_READINESS,
        semantic_contracts: [],
      },
    };
    vi.mocked(auditApi.fetchAuditReadiness).mockResolvedValue(snapshot);
    const { container } = render(<AuditReadinessPanel />);
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("ReadinessRowDetail", () => {
  it("has no axe violations", async () => {
    const row: ReadinessRow = {
      id: "provenance",
      label: "Provenance",
      status: "warning",
      summary: "Identity passthrough detected",
      detail: "Identity passthrough — provenance gap on 'select_columns'.",
      component_ids: ["select_columns"],
    };
    const { container } = render(
      <ReadinessRowDetail row={row} onClose={() => {}} />,
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("ExplainDialog", () => {
  it("has no axe violations", async () => {
    vi.mocked(auditApi.fetchAuditReadinessExplain).mockResolvedValue({
      session_id: "sess-a11y",
      composition_version: 1,
      narrative: "When you run this pipeline, ELSPETH will record provenance.",
    });
    const { container } = render(
      <ExplainDialog
        sessionId="sess-a11y"
        compositionVersion={1}
        onClose={() => {}}
      />,
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("AppHeader", () => {
  it("has no axe violations", async () => {
    const { container } = render(
      <AppHeader onOpenSettings={() => {}} onSignOut={() => {}} />,
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("HeaderSessionSwitcher", () => {
  it("has no axe violations (closed/default state)", async () => {
    const { container } = render(<HeaderSessionSwitcher />);
    expect(await axe(container)).toHaveNoViolations();
  });

  // I5: the closed-state audit above only covers the trigger button.
  // The entire interactive surface (filter input, archived toggle,
  // session rows, rename form, archive error region) lives in the open
  // menu and was previously unaudited.  Without this test a missing
  // ``aria-label`` on the filter input, a focus-trap regression on the
  // archive confirmation, or a missing ``role="alert"`` on the inline
  // error would not be caught.
  it("has no axe violations in the open state", async () => {
    const { container } = render(<HeaderSessionSwitcher />);
    const trigger = screen.getByRole("button", { name: /session switcher/i });
    await userEvent.click(trigger);
    // Menu, filter input, archived-toggle checkbox, and any session
    // rows are now rendered.  axe walks the full container.
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("HeaderVersionSelector", () => {
  it("has no axe violations", async () => {
    const { container } = render(<HeaderVersionSelector />);
    expect(await axe(container)).toHaveNoViolations();
  });

  // The case above renders only the closed trigger — it audits none of the
  // dropdown. Seed a history that GROUPS (elspeth-c8a402a9a4) and open it, so
  // axe actually sees ul[role=tree] > li[role=treeitem] > ul[role=group] >
  // li[role=treeitem] — the WAI-ARIA tree pattern this widget adopted when it
  // stopped being a listbox (a collapsible group cannot be an `option`).
  it("has no axe violations with the history tree open and a group expanded", async () => {
    useSessionStore.setState({
      compositionState: {
        version: 19,
        sources: {},
        nodes: [],
        edges: [],
        outputs: [],
      } as never,
      stateVersions: makeVersionHistory(),
      isLoadingVersions: false,
    } as never);
    const { container } = render(<HeaderVersionSelector />);
    await userEvent.click(
      screen.getByRole("button", { name: /Composition history/ }),
    );
    const group = screen.getByRole("treeitem", { name: /Versions 11 to 18/ });
    await userEvent.click(group);
    // Guard against auditing a collapsed (or empty) tree by accident.
    expect(group).toHaveAttribute("aria-expanded", "true");
    expect(screen.getAllByRole("treeitem").length).toBeGreaterThan(2);
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("GraphMiniView", () => {
  it("has no axe violations", async () => {
    const { container } = render(<GraphMiniView />);
    // Guard against the fixture silently regressing to the "Loading
    // pipeline…" skeleton (a bare <span> with no interactive markup, which
    // would trivially pass axe without covering the real button/SVG/label
    // this test exists to audit) — assert the populated graph actually
    // rendered.
    expect(
      screen.getByRole("button", { name: /pipeline graph/i }),
    ).toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("InlineSourceCreatedTurn", () => {
  it("has no axe violations", async () => {
    const summary: InlineSourceSummary = {
      blobId: "b1",
      filename: "chat.csv",
      mimeType: "text/csv",
      contentPreview: "url\nhttps://example.gov.au",
      rowCount: 1,
      contentHash: "abc123def456789",
      provenance: "llm-generated",
    };
    const { container } = render(
      <InlineSourceCreatedTurn summary={summary} onEdit={() => {}} />,
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("InlineSourceFallbackPrompt", () => {
  it("has no axe violations", async () => {
    const { container } = render(
      <InlineSourceFallbackPrompt
        shouldRender={true}
        candidateText="https://example.com"
        onAccept={() => {}}
        onDismiss={() => {}}
      />,
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("CompletionBar", () => {
  it("has no axe violations", async () => {
    useExecutionStore.setState({
      validationResult: {
        is_valid: true,
        checks: [],
        errors: [],
        readiness: READY_READINESS,
      },
    } as never);
    const { container } = render(<CompletionBar />);
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("PluginCard", () => {
  it("has no axe violations", async () => {
    const plugin: PluginSummary = {
      name: "csv",
      plugin_type: "source",
      description: "Read rows from a CSV file.",
      config_fields: [],
      usage_when_to_use: "When you have a CSV file already.",
      usage_when_not_to_use: "When the data is inline.",
      example_use: "source:\n  plugin: csv\n  options:\n    path: data.csv",
      capability_tags: ["csv", "file"],
      audit_characteristics: ["io_read", "quarantine"],
    } as PluginSummary;
    const { container } = render(
      <PluginCard plugin={plugin} schema={null} onExpand={() => {}} />,
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("FilterChipStrip", () => {
  it("has no axe violations", async () => {
    const filters: CatalogFilters = {
      capabilityTags: new Set(),
      auditCharacteristics: new Set(),
    };
    const { container } = render(
      <FilterChipStrip
        availableCapabilityTags={["csv", "file"]}
        availableAuditCharacteristics={["io_read", "quarantine"]}
        filters={filters}
        onChange={() => {}}
      />,
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("FreeformIntroduction", () => {
  it("has no axe violations", async () => {
    usePreferencesStore.setState({
      loaded: true,
      freeformIntroDismissedAt: null,
      writing: false,
    });
    const { container } = render(<FreeformIntroduction />);
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("ShortcutsHelp", () => {
  it("has no axe violations", async () => {
    const { container } = render(<ShortcutsHelp onClose={() => {}} />);
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("PipelineGloss", () => {
  it("has no axe violations", async () => {
    const { container } = render(
      <PipelineGloss
        compositionState={useSessionStore.getState().compositionState}
      />,
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("PipelineValidationSummary", () => {
  it("has no axe violations (warning state with a tinted glyph)", async () => {
    // Exercise the richest DOM (glyph + plain status text), not the neutral
    // null state, so the axe pass covers the aria-hidden glyph + role=status.
    useExecutionStore.setState({
      validationResult: {
        is_valid: true,
        checks: [],
        errors: [],
        warnings: [
          {
            component_id: "select_columns",
            component_type: "transform",
            message: "Review the optional mapping",
            suggestion: null,
          },
        ],
        readiness: READY_READINESS,
      },
    } as never);
    const { container } = render(<PipelineValidationSummary />);
    expect(await axe(container)).toHaveNoViolations();
  });
});

// --- Tutorial surface (elspeth-adf5e679e7) -----------------------------------
//
// The tutorial is the surface being promoted to flagship; until this pass it
// had ZERO automated a11y regression coverage. Each turn is audited in its
// richest deterministic state; the top-level shell is audited on the welcome
// step (progress nav + sr-only step announcement + welcome turn).

describe("HelloWorldTutorial", () => {
  it("has no axe violations on the welcome step (shell + progress nav)", async () => {
    vi.mocked(apiClient.deleteTutorialOrphans).mockResolvedValue({
      deleted_count: 0,
    });
    const { container } = render(<HelloWorldTutorial />);
    // Guard against a vacuous pass: the shell + progress nav must be real.
    screen.getByRole("group", { name: "Tutorial progress" });
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("TutorialTurn1Welcome", () => {
  it("has no axe violations", async () => {
    const { container } = render(
      <TutorialTurn1Welcome onStart={() => {}} onSkip={() => {}} />,
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("TutorialFreeformShell", () => {
  it("has no axe violations while preparing the freeform sample brief", async () => {
    // Keep the sample request pending to audit the loading surface.
    useSessionStore.setState({
      activeSessionId: "00000000-0000-4000-8000-000000000999",
      compositionStateLoaded: true,
      error: null,
    });
    vi.mocked(apiClient.getTutorialSample).mockReturnValue(
      new Promise<never>(() => {}),
    );
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe(): void {}
        unobserve(): void {}
        disconnect(): void {}
      },
    );
    const { container } = render(
      <TutorialFreeformShell
        sessionId="00000000-0000-4000-8000-000000000999"
        onCompleted={() => {}}
      />,
    );
    screen.getByText(/Loading your example/);
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("ChatPanelFreeformTutorialWorkspace", () => {
  it("has no axe violations in the tutorial's freeform Build surface", async () => {
    useSessionStore.setState({
      activeSessionId: "sess-a11y",
      messages: [],
      compositionState: null,
      compositionStateLoaded: true,
    });

    const { container } = render(<ChatPanel />);

    screen.getByRole("region", { name: "Chat panel" });
    screen.getByRole("log", { name: "Conversation" });
    screen.getByLabelText("Message input");
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("TutorialTurn4Run", () => {
  // The module-level run cache is keyed by sessionId — each test uses a
  // distinct id so a cached promise never leaks across tests. The run never
  // auto-fires (I-1): the executing/results states are reached by clicking
  // the Run button in the step header.
  //
  // The run step renders the real workspace frame (step header + panes), and
  // ComposerWorkspace observes its own width, so each test states the
  // ResizeObserver it needs rather than inheriting an earlier test's stub.
  beforeEach(() => {
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe(): void {}
        unobserve(): void {}
        disconnect(): void {}
      },
    );
  });

  it("has no axe violations on the pre-run card", async () => {
    const { container } = render(
      <TutorialTurn4Run
        sessionId="sess-a11y-run-ready"
        onCompleted={() => {}}
        onCancelled={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: "Run" })).toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });

  it("has no axe violations while the run is executing", async () => {
    vi.mocked(apiClient.runTutorialPipeline).mockReturnValue(
      new Promise<never>(() => {}),
    );
    const { container } = render(
      <TutorialTurn4Run
        sessionId="sess-a11y-run-pending"
        onCompleted={() => {}}
        onCancelled={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(screen.getByRole("status", { busy: true })).toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });

  it("has no axe violations on the results table with a discarded-rows notice", async () => {
    vi.mocked(apiClient.runTutorialPipeline).mockResolvedValue({
      run_id: "run-a11y",
      output: {
        source_data_hash: "a".repeat(64),
        rows: [
          {
            url: "https://example.gov.au/page-1",
            summary: "A one-line summary of the page.",
            error: null,
          },
        ],
        discarded_row_count: 1,
      },
    });
    const { container } = render(
      <TutorialTurn4Run
        sessionId="sess-a11y-run-done"
        onResult={() => {}}
        onCompleted={() => {}}
        onCancelled={() => {}}
        onBack={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByText(/rows returned/);
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("TutorialTurn5AuditStory", () => {
  it("has no axe violations with loaded audit evidence", async () => {
    vi.mocked(apiClient.getRunAuditSummary).mockResolvedValue({
      run_id: "run-a11y",
      session_id: "sess-a11y",
      llm_call_count: 3,
      source_data_hash: "b".repeat(64),
      started_at: "2026-07-01T00:00:00Z",
      plugin_versions: { web_scrape: "1.0.0", llm_transform: "2.1.0" },
      seeded_from_cache: false,
      cache_key: null,
    });
    const { container } = render(
      <TutorialTurn5AuditStory
        sessionId="sess-a11y"
        runId="run-a11y"
        onContinue={() => {}}
        onBack={() => {}}
      />,
    );
    await screen.findByText("Source data hash");
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("TutorialTurn7Graduation", () => {
  it("has no axe violations (completed tutorial)", async () => {
    const { container } = render(
      <TutorialTurn7Graduation
        sessionId="sess-a11y"
        skipped={false}
        cancelled={false}
        onBack={() => {}}
      />,
    );
    expect(await axe(container)).toHaveNoViolations();
  });

  it("has no axe violations (cancelled variant with the status note)", async () => {
    const { container } = render(
      <TutorialTurn7Graduation
        sessionId="sess-a11y"
        skipped={false}
        cancelled
      />,
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});

// --- Acknowledgement surface -------------------------------------------------

describe("AcknowledgementCard", () => {
  it("has no axe violations (vague_term with the amend affordance)", async () => {
    const { container } = render(
      <AcknowledgementCard
        event={makeInterpretationEvent("evt-1")}
        sessionId="sess-a11y"
        stepLabel="Summarise"
        showAmend
      />,
    );
    expect(await axe(container)).toHaveNoViolations();
  });

  it("has no axe violations (llm_prompt_template with the View/Approve controls, pre- and post-view)", async () => {
    const { container } = render(
      <AcknowledgementCard
        event={makeInterpretationEvent("evt-2", {
          kind: "llm_prompt_template",
          llm_draft: "Summarise {{ body }} in one paragraph for {{ audience }}.",
        })}
        sessionId="sess-a11y"
        stepLabel="Summarise"
      />,
    );
    // Pre-view: disabled Approve with the aria-describedby gate note.
    expect(await axe(container)).toHaveNoViolations();
    // Post-view: revealed prompt region + enabled Approve.
    await userEvent.click(screen.getByRole("button", { name: "View prompt" }));
    expect(await axe(container)).toHaveNoViolations();
  });

  it("has no axe violations on the resolved prompt with marked substitution slots", async () => {
    // Without a composition state the prompt renders as one flat text
    // segment, so the <mark> slots — and the visually-hidden
    // "accepted value: " / "pending value: " status prefixes inside them —
    // escape the audit entirely.  Supply the structured parts so both slot
    // kinds and the pending note are actually in the audited DOM.
    const state = makeFullCompositionState();
    const { container } = render(
      <AcknowledgementCard
        event={makeInterpretationEvent("evt-3", {
          kind: "llm_prompt_template",
          llm_draft: "Summarise pending interpretation for pending interpretation.",
        })}
        sessionId="sess-a11y"
        stepLabel="Summarise"
        compositionState={{
          ...state,
          nodes: [
            {
              ...state.nodes[0],
              options: {
                prompt_template_parts: [
                  { kind: "text", text: "Summarise " },
                  { kind: "interpretation_ref", requirement_id: "req-1" },
                  { kind: "text", text: " for " },
                  { kind: "interpretation_ref", requirement_id: "req-2" },
                  { kind: "text", text: "." },
                ],
                interpretation_requirements: [
                  {
                    id: "req-1",
                    status: "resolved",
                    draft: "briefly",
                    accepted_value: "concise and neutral",
                  },
                  {
                    id: "req-2",
                    status: "pending",
                    draft: "an auditor",
                    accepted_value: null,
                  },
                ],
              },
            },
          ],
        }}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "View prompt" }));
    // Guard against a vacuous pass: both slot kinds must be mounted, each
    // carrying its visually-hidden status prefix.
    const region = screen.getByRole("region", { name: /prompt template review/i });
    expect(
      region.querySelector("mark.ack-card-prompt-slot--resolved")?.textContent,
    ).toBe("accepted value: concise and neutral");
    expect(
      region.querySelector("mark.ack-card-prompt-slot--pending")?.textContent,
    ).toBe("pending value: an auditor");
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("AcknowledgementStack", () => {
  it("has no axe violations with pending acknowledgements", async () => {
    useSessionStore.setState({
      compositionState: makeFullCompositionState(),
    } as never);
    useInterpretationEventsStore.setState({
      pendingBySession: {
        "sess-a11y": {
          "evt-1": makeInterpretationEvent("evt-1"),
          "evt-2": makeInterpretationEvent("evt-2", {
            kind: "llm_model_choice",
            llm_draft: "anthropic/claude-sonnet-4.6",
          }),
        },
      },
    });
    const { container } = render(<AcknowledgementStack sessionId="sess-a11y" />);
    // Guard against a vacuous pass (the stack renders nothing when no
    // events are pending): both cards must be mounted.
    expect(
      screen.getAllByRole("button", { name: /Acknowledge/ }),
    ).toHaveLength(2);
    expect(await axe(container)).toHaveNoViolations();
  });
});

// --- Auth surface --------------------------------------------------------------

describe("LoginPage", () => {
  function mockLocalAuthConfig(): void {
    const config: AuthConfig = {
      provider: "local",
      registration_mode: "open",
      sso_start_url: null,
    };
    vi.mocked(apiClient.fetchAuthConfig).mockResolvedValue(config);
  }

  it("has no axe violations on the sign-in view", async () => {
    mockLocalAuthConfig();
    const { container } = render(<LoginPage />);
    await screen.findByLabelText("Username");
    expect(await axe(container)).toHaveNoViolations();
  });

  it("has no axe violations on the registration view", async () => {
    mockLocalAuthConfig();
    const { container } = render(<LoginPage />);
    await screen.findByLabelText("Username");
    await userEvent.click(
      screen.getByRole("button", { name: "Create an account" }),
    );
    await screen.findByLabelText("Confirm password");
    expect(await axe(container)).toHaveNoViolations();
  });
});

// --- Chrome / run surfaces -----------------------------------------------------

describe("ConfirmDialog", () => {
  it("has no axe violations (danger variant with structured content)", async () => {
    const { container } = render(
      <ConfirmDialog
        title="Discard this run?"
        message="The run's partial output will be discarded."
        confirmLabel="Discard"
        cancelLabel="Keep"
        variant="danger"
        onConfirm={() => {}}
        onCancel={() => {}}
      >
        <p>One output file will be removed.</p>
      </ConfirmDialog>,
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("WorkspaceSeparator", () => {
  it("has no axe violations while keyboard resizing is available", async () => {
    const { container } = render(
      <WorkspaceSeparator
        value={448}
        min={360}
        max={640}
        disabled={false}
        onResize={() => {}}
        onResizeEnd={() => {}}
      />,
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("CommandPalette", () => {
  it("has no axe violations when open", async () => {
    // jsdom does not implement Element.prototype.scrollIntoView (the
    // palette scrolls the selected row into view on mount) — same stub as
    // CommandPalette.test.tsx.
    Element.prototype.scrollIntoView = vi.fn();
    const { container } = render(
      <CommandPalette
        isOpen
        onClose={() => {}}
        runAdmissionAvailable
      />,
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("ChatInput", () => {
  it("has no axe violations with the composition affordances shown", async () => {
    const inputRef = createRef<HTMLTextAreaElement>();
    const { container } = render(
      <ChatInput
        onSend={() => {}}
        disabled={false}
        inputRef={inputRef}
        onToggleBlobManager={() => {}}
        onOpenSecrets={() => {}}
      />,
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("ProgressView", () => {
  it("has no axe violations during a live run", async () => {
    vi.mocked(useWebSocket).mockReturnValue({
      activeRunId: "run-a11y",
      wsDisconnected: false,
      progress: {
        source_rows_processed: 3,
        tokens_succeeded: 2,
        tokens_failed: 1,
        tokens_quarantined: 0,
        tokens_routed_success: 2,
        tokens_routed_failure: 1,
        cancel_requested: false,
        accounting: null,
        recent_errors: [],
        status: "running",
      },
    } as never);
    const { container } = render(<ProgressView />);
    screen.getByText("Source rows");
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("RunOutcomeNotice", () => {
  // resetAllStores() clears lastRunOutcome for the whole file; this is the
  // per-describe belt so an assertion failure above cannot leak a mounted
  // toast into the next test even within this block.
  afterEach(() => {
    useExecutionStore.setState({ lastRunOutcome: null } as never);
  });

  it("has no axe violations for a failed-run outcome toast", async () => {
    useExecutionStore.setState({
      lastRunOutcome: {
        runId: "run-a11y",
        status: "failed",
        sessionId: "sess-a11y",
      },
    } as never);
    const { container } = render(<RunOutcomeNotice />);
    // Guard against a vacuous pass on the empty (acknowledged) state: the
    // toast and both actions must actually be mounted. The notice announces
    // every terminal outcome POLITELY — role="status", never role="alert" —
    // so the live region, not an alert, is what proves it spoke.
    expect(screen.getByRole("status")).toHaveTextContent("Pipeline failed.");
    screen.getByRole("button", { name: "View run" });
    screen.getByRole("button", { name: "Dismiss run outcome notice" });
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("RunsHistoryDrawer", () => {
  it("has no axe violations on the run list", async () => {
    const runs = [
      { id: "run-1", status: "completed" },
      { id: "run-2", status: "completed_with_failures" },
      { id: "run-3", status: "running" },
    ] as unknown as ReadonlyArray<Run>;
    const { container } = render(
      <RunsHistoryDrawer onClose={() => {}} runsOverride={runs} />,
    );
    // Guard against a vacuous pass: the glyph-carrying StatusBadge row
    // must be listed.
    screen.getByText("completed with failures");
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("GraphView", () => {
  it("has no axe violations (accessible node list + diagram scope)", async () => {
    useSessionStore.setState({
      compositionState: makeFullCompositionState(),
      compositionProposals: [],
    } as never);
    const { container } = render(<GraphView />);
    // Guard against the empty-state fallback masquerading as coverage: the
    // role="img" diagram scope and the keyboard-operable node list must be
    // real (source + transform + sink = 3 components).
    screen.getByRole("img", { name: /Pipeline graph with 3 components/ });
    screen.getByRole("list", {
      name: /Pipeline components in source-to-sink order \(3\)/,
    });
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("GraphModal", () => {
  it("has no axe violations when open", async () => {
    useSessionStore.setState({
      compositionState: makeFullCompositionState(),
      compositionProposals: [],
    } as never);
    const { container } = render(<GraphModal />);
    // The dispatch is act()-wrapped so the listener's setState commits before
    // the assertion.
    act(() => {
      window.dispatchEvent(new CustomEvent(OPEN_GRAPH_MODAL_EVENT));
    });
    screen.getByRole("dialog", { name: "Pipeline graph" });
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("RecoveryPanel", () => {
  it("has no axe violations", async () => {
    const recoveryError: ComposerRecoveryError = {
      status: 500,
      detail: "Composer failed after a tool call",
      error_type: "composer_plugin_crash",
      partial_state: makeFullCompositionState(),
      failed_turn: {
        // null keeps RecoveryTranscript in its idle (no-fetch) state.
        assistant_message_id: null,
        tool_calls_attempted: 2,
        tool_responses_persisted: 1,
        transcript_url: null,
      },
    };
    const { container } = render(
      <RecoveryPanel
        activeSessionId="sess-a11y"
        currentState={makeFullCompositionState()}
        recoveryError={recoveryError}
        onApply={() => ({ applied: true, needsConfirmation: false })}
        onDiscard={() => {}}
      />,
    );
    screen.getByRole("button", { name: /Discard recovery/ });
    expect(await axe(container)).toHaveNoViolations();
  });
});
