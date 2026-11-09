import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  fetchShareableLink,
  fetchSharedInspect,
  fetchShareableFiles,
  importSharedSnapshot,
  markReadyForReview,
} from "./shareableReviews";
import { useAuthStore } from "../stores/authStore";

const SESSION_ID = "00000000-0000-0000-0000-000000000001";
const READY_READINESS = {
  authoring_valid: true,
  execution_ready: true,
  completion_ready: true,
  blockers: [],
};

const _validReadinessSnapshot = {
  session_id: SESSION_ID,
  composition_version: 3,
  checked_at: new Date().toISOString(),
  rows: [
    { id: "validation", label: "Validation", status: "ok", summary: "ok", detail: null, component_ids: [] },
    { id: "plugin_trust", label: "Plugin trust", status: "ok", summary: "ok", detail: null, component_ids: [] },
    { id: "provenance", label: "Provenance", status: "ok", summary: "ok", detail: null, component_ids: [] },
    { id: "retention", label: "Retention", status: "ok", summary: "ok", detail: null, component_ids: [] },
    { id: "llm_interpretations", label: "LLM interpretations", status: "ok", summary: "ok", detail: null, component_ids: [] },
    { id: "secrets", label: "Secrets", status: "ok", summary: "ok", detail: null, component_ids: [] },
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

describe("shareableReviews API client", () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn();
    localStorage.clear();
    useAuthStore.setState({
      token: null,
      user: null,
      loginError: null,
      isLoading: false,
    } as never);
  });

  // ── markReadyForReview ──────────────────────────────────────────────

  it("markReadyForReview POSTs the right URL with auth header", async () => {
    localStorage.setItem("auth_token", "test-token");
    try {
      const body = {
        token: "ZmFrZS10b2tlbg",
        share_url: "/#/shared/ZmFrZS10b2tlbg",
        expires_at: "2026-06-19T00:00:00+00:00",
        payload_digest: "sha256:" + "ab".repeat(32),
      };
      (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );

      const response = await markReadyForReview(SESSION_ID);
      expect(response.token).toBe("ZmFrZS10b2tlbg");
      expect(response.payload_digest.startsWith("sha256:")).toBe(true);

      const mock = globalThis.fetch as ReturnType<typeof vi.fn>;
      const [url, init] = mock.mock.calls[0];
      expect(url).toBe(`/api/sessions/${SESSION_ID}/mark-ready-for-review`);
      expect(init?.method).toBe("POST");
      const headers = init?.headers as Record<string, string> | undefined;
      expect(headers?.Authorization).toBe("Bearer test-token");
    } finally {
      localStorage.removeItem("auth_token");
    }
  });

  it("markReadyForReview rejects malformed responses (missing token)", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          share_url: "/#/shared/abc",
          expires_at: "2026-06-19T00:00:00+00:00",
          payload_digest: "sha256:" + "ab".repeat(32),
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    await expect(markReadyForReview(SESSION_ID)).rejects.toMatchObject({
      detail: expect.stringContaining("mark-ready-for-review"),
    });
  });

  // ── fetchShareableLink ──────────────────────────────────────────────

  it("fetchShareableLink GETs the right URL and parses the response", async () => {
    const body = {
      token: "tk-1",
      share_url: "/#/shared/tk-1",
      expires_at: "2026-06-19T00:00:00+00:00",
      state_id: "11111111-1111-1111-1111-111111111111",
      payload_digest: "sha256:" + "cd".repeat(32),
    };
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    const response = await fetchShareableLink(SESSION_ID);
    expect(response.state_id).toBe("11111111-1111-1111-1111-111111111111");
    expect(response.payload_digest.startsWith("sha256:")).toBe(true);

    const mock = globalThis.fetch as ReturnType<typeof vi.fn>;
    const [url, init] = mock.mock.calls[0];
    expect(url).toBe(`/api/sessions/${SESSION_ID}/shareable-link`);
    expect(init?.method).toBe("GET");
  });

  it("fetchShareableLink rejects malformed responses (missing state_id)", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          token: "tk",
          share_url: "/#/shared/tk",
          expires_at: "2026-06-19T00:00:00+00:00",
          payload_digest: "sha256:" + "cd".repeat(32),
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    await expect(fetchShareableLink(SESSION_ID)).rejects.toMatchObject({
      detail: expect.stringContaining("shareable-link"),
    });
  });

  // ── fetchSharedInspect ──────────────────────────────────────────────

  it("fetchSharedInspect GETs /shared/{token} URL-encoded", async () => {
    const token = "abc/def+ghi=";
    const body = {
      session_id: SESSION_ID,
      state_id: "22222222-2222-2222-2222-222222222222",
      pipeline_metadata: { name: "Demo", description: "" },
      composition_snapshot: {
        version: 1,
        metadata: { name: "Demo", description: "" },
        sources: {},
        nodes: [],
        edges: [],
        outputs: [],
      },
      yaml: "version: 1\n",
      audit_readiness: _validReadinessSnapshot,
      created_by_user_id: "alice",
      created_by_username: "alice-the-analyst",
      created_at: "2026-05-19T00:00:00+00:00",
      expires_at: "2026-06-19T00:00:00+00:00",
      import_allowed: true,
      files: [{ filename: "a.csv", size_bytes: 12 }],
    };
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    const response = await fetchSharedInspect(token);
    expect(response.import_allowed).toBe(true);
    expect(response.files).toEqual([{ filename: "a.csv", size_bytes: 12 }]);
    expect(response.session_id).toBe(SESSION_ID);
    expect(response.audit_readiness.rows).toHaveLength(6);

    const mock = globalThis.fetch as ReturnType<typeof vi.fn>;
    const [url] = mock.mock.calls[0];
    // URL-encoded so '/' and '+' don't break the path.
    expect(url).toBe(`/api/sessions/shared/${encodeURIComponent(token)}`);
  });

  it("fetchSharedInspect rejects malformed audit_readiness (not a record)", async () => {
    const body = {
      session_id: SESSION_ID,
      state_id: "22222222-2222-2222-2222-222222222222",
      pipeline_metadata: { name: "Demo", description: "" },
      composition_snapshot: {
        version: 1,
        metadata: { name: "Demo", description: "" },
        sources: {},
        nodes: [],
        edges: [],
        outputs: [],
      },
      yaml: "version: 1\n",
      audit_readiness: "not-an-object", // wrong type
      created_by_user_id: "alice",
      created_by_username: "alice-the-analyst",
      created_at: "2026-05-19T00:00:00+00:00",
      expires_at: "2026-06-19T00:00:00+00:00",
    };
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    await expect(fetchSharedInspect("any-token")).rejects.toMatchObject({
      detail: expect.stringContaining("shared-inspect"),
    });
  });

  // ── FIX-K: tighter Tier-3 → Tier-1 validation at the API boundary ─────

  function _buildValidSharedInspectBody(
    overrides: Partial<Record<string, unknown>> = {},
  ): Record<string, unknown> {
    return {
      session_id: SESSION_ID,
      state_id: "22222222-2222-2222-2222-222222222222",
      pipeline_metadata: { name: "Demo", description: "" },
      composition_snapshot: {
        version: 1,
        metadata: { name: "Demo", description: "" },
        sources: {},
        nodes: [],
        edges: [],
        outputs: [],
      },
      yaml: "version: 1\n",
      audit_readiness: _validReadinessSnapshot,
      created_by_user_id: "alice",
      created_by_username: "alice-the-analyst",
      created_at: "2026-05-19T00:00:00+00:00",
      expires_at: "2026-06-19T00:00:00+00:00",
      import_allowed: false,
      files: [],
      ...overrides,
    };
  }

  function _mockJsonResponse(body: unknown, status = 200): void {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      }),
    );
  }

  it("fetchSharedInspect accepts a fully-shaped happy-path body (smoke)", async () => {
    _mockJsonResponse(_buildValidSharedInspectBody());
    const response = await fetchSharedInspect("happy-token");
    expect(response.audit_readiness.rows).toHaveLength(6);
    expect(response.pipeline_metadata.name).toBe("Demo");
    expect(response.composition_snapshot.nodes).toEqual([]);
  });

  it("fetchSharedInspect rejects a null created_by_username", async () => {
    _mockJsonResponse(_buildValidSharedInspectBody({ created_by_username: null }));
    await expect(fetchSharedInspect("any-token")).rejects.toMatchObject({
      detail: expect.stringContaining("shared-inspect"),
    });
  });

  it("fetchSharedInspect rejects a body with created_by_username absent", async () => {
    const body = _buildValidSharedInspectBody();
    delete body.created_by_username;
    _mockJsonResponse(body);
    await expect(fetchSharedInspect("any-token")).rejects.toMatchObject({
      detail: expect.stringContaining("shared-inspect"),
    });
  });

  it("fetchSharedInspect rejects a blank created_by_username", async () => {
    _mockJsonResponse(_buildValidSharedInspectBody({ created_by_username: "  \t " }));
    await expect(fetchSharedInspect("any-token")).rejects.toMatchObject({
      detail: expect.stringContaining("shared-inspect"),
    });
  });

  it("fetchSharedInspect rejects audit_readiness with malformed row (missing required row fields)", async () => {
    const bad_readiness = {
      ..._validReadinessSnapshot,
      rows: [{ id: "validation" }], // missing label/status/summary/detail/component_ids
    };
    _mockJsonResponse(_buildValidSharedInspectBody({ audit_readiness: bad_readiness }));
    await expect(fetchSharedInspect("any-token")).rejects.toMatchObject({
      detail: expect.stringContaining("shared-inspect"),
    });
  });

  it("fetchSharedInspect rejects audit_readiness with non-object row (null)", async () => {
    const bad_readiness = { ..._validReadinessSnapshot, rows: [null] };
    _mockJsonResponse(_buildValidSharedInspectBody({ audit_readiness: bad_readiness }));
    await expect(fetchSharedInspect("any-token")).rejects.toMatchObject({
      detail: expect.stringContaining("shared-inspect"),
    });
  });

  it("fetchSharedInspect rejects audit_readiness with non-object row (string)", async () => {
    const bad_readiness = { ..._validReadinessSnapshot, rows: ["string-row"] };
    _mockJsonResponse(_buildValidSharedInspectBody({ audit_readiness: bad_readiness }));
    await expect(fetchSharedInspect("any-token")).rejects.toMatchObject({
      detail: expect.stringContaining("shared-inspect"),
    });
  });

  it("fetchSharedInspect rejects pipeline_metadata with missing name", async () => {
    _mockJsonResponse(
      _buildValidSharedInspectBody({ pipeline_metadata: { description: "" } }),
    );
    await expect(fetchSharedInspect("any-token")).rejects.toMatchObject({
      detail: expect.stringContaining("shared-inspect"),
    });
  });

  it("fetchSharedInspect rejects pipeline_metadata with wrong type for name", async () => {
    _mockJsonResponse(
      _buildValidSharedInspectBody({ pipeline_metadata: { name: 123, description: "" } }),
    );
    await expect(fetchSharedInspect("any-token")).rejects.toMatchObject({
      detail: expect.stringContaining("shared-inspect"),
    });
  });

  it("fetchSharedInspect rejects composition_snapshot with missing nodes", async () => {
    _mockJsonResponse(
      _buildValidSharedInspectBody({
        composition_snapshot: {
          version: 1,
          metadata: { name: "Demo", description: "" },
          sources: {},
          edges: [],
          outputs: [],
        },
      }),
    );
    await expect(fetchSharedInspect("any-token")).rejects.toMatchObject({
      detail: expect.stringContaining("shared-inspect"),
    });
  });

  it("fetchSharedInspect rejects composition_snapshot with wrong type for nodes", async () => {
    _mockJsonResponse(
      _buildValidSharedInspectBody({
        composition_snapshot: {
          version: 1,
          metadata: { name: "Demo", description: "" },
          sources: {},
          nodes: "not-array",
          edges: [],
          outputs: [],
        },
      }),
    );
    await expect(fetchSharedInspect("any-token")).rejects.toMatchObject({
      detail: expect.stringContaining("shared-inspect"),
    });
  });

  it("fetchSharedInspect rejects composition_snapshot with missing metadata", async () => {
    _mockJsonResponse(
      _buildValidSharedInspectBody({
        composition_snapshot: {
          version: 1,
          sources: {},
          nodes: [],
          edges: [],
          outputs: [],
        },
      }),
    );
    await expect(fetchSharedInspect("any-token")).rejects.toMatchObject({
      detail: expect.stringContaining("shared-inspect"),
    });
  });

  it.each([undefined, "Basic", "BearerOther", 'Bearer realm="other"', "Bearer, Basic"])(
    "fetchSharedInspect preserves auth for an unrecognized challenge %s",
    async (challenge) => {
      localStorage.setItem("auth_token", "reviewer-session-token");
      useAuthStore.setState({
        token: "reviewer-session-token",
        user: {
          user_id: "reviewer",
          username: "reviewer",
          display_name: null,
          email: null,
          groups: [],
        },
        loginError: null,
        isLoading: false,
      } as never);
      (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "Invalid or expired share token" }), {
          status: 401,
          headers: {
            "content-type": "application/json",
            ...(challenge === undefined ? {} : { "WWW-Authenticate": challenge }),
          },
        }),
      );
      const request = fetchSharedInspect("bad-token");
      await expect(request).rejects.toMatchObject({
        status: 401,
      });
      await expect(request).rejects.not.toMatchObject({ error_type: "authentication_required" });
      expect(useAuthStore.getState().token).toBe("reviewer-session-token");
      expect(useAuthStore.getState().user?.username).toBe("reviewer");
      expect(localStorage.getItem("auth_token")).toBe("reviewer-session-token");
    },
  );

  it.each(["Bearer", "bearer"])("fetchSharedInspect clears rejected authentication with a %s challenge", async (challenge) => {
    localStorage.setItem("auth_token", "expired-reviewer-token");
    useAuthStore.setState({ token: "expired-reviewer-token", isLoading: false });
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "Token expired" }), {
        status: 401,
        headers: { "content-type": "application/json", "WWW-Authenticate": challenge },
      }),
    );

    await expect(fetchSharedInspect("valid-share-token")).rejects.toMatchObject({
      status: 401,
      error_type: "authentication_required",
      detail: "Token expired",
    });
    expect(useAuthStore.getState().token).toBeNull();
    expect(localStorage.getItem("auth_token")).toBeNull();
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
  });

  it.each(["replacement-token", "original-reviewer-token"])(
    "preserves the replacement login %s after a delayed shared authentication refusal",
    async (replacementToken) => {
      let finishRequest!: (value: Response) => void;
      const oldResponse = new Promise<Response>((resolve) => { finishRequest = resolve; });
      localStorage.setItem("auth_token", "original-reviewer-token");
      useAuthStore.setState({ token: "original-reviewer-token", isLoading: false });
      (globalThis.fetch as ReturnType<typeof vi.fn>)
        .mockReturnValueOnce(oldResponse)
        .mockResolvedValueOnce(new Response(JSON.stringify({
          user_id: "reviewer", username: "reviewer", display_name: null,
          email: null, groups: [], dev_admin: false,
        })))
        .mockResolvedValueOnce(new Response(JSON.stringify({
          provider: "local", registration_mode: "closed", sso_start_url: null,
        })));
      const pending = fetchSharedInspect("valid-share-token");
      await useAuthStore.getState().loginWithToken(replacementToken);
      finishRequest(new Response(JSON.stringify({ detail: "Token expired" }), {
        status: 401,
        headers: { "content-type": "application/json", "WWW-Authenticate": "Bearer" },
      }));

      await expect(pending).rejects.toMatchObject({ status: 401, error_type: "authentication_required" });
      expect(useAuthStore.getState().token).toBe(replacementToken);
      expect(localStorage.getItem("auth_token")).toBe(replacementToken);
      expect(useAuthStore.getState().user?.username).toBe("reviewer");
    },
  );

  it("fetchSharedInspect propagates 404 from the parser (blob expired)", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          detail: "Shared snapshot is no longer available; ask the sender for a fresh link",
        }),
        { status: 404, headers: { "content-type": "application/json" } },
      ),
    );
    await expect(fetchSharedInspect("good-but-stale-token")).rejects.toMatchObject({
      status: 404,
    });
  });

  // ── share options + import ──────────────────────────────────────────

  const MARK_READY_BODY = {
    token: "tk",
    share_url: "/#/shared/tk",
    expires_at: "2026-06-19T00:00:00+00:00",
    payload_digest: "sha256:" + "ab".repeat(32),
  };

  it("markReadyForReview sends allow_import and lifetime_seconds as a JSON body", async () => {
    _mockJsonResponse(MARK_READY_BODY);
    await markReadyForReview(SESSION_ID, { allowImport: true, lifetimeSeconds: 86400 });
    const [, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse(init?.body as string)).toEqual({ allow_import: true, lifetime_seconds: 86400 });
    const headers = init?.headers as Record<string, string> | undefined;
    expect(headers?.["Content-Type"]).toBe("application/json");
  });

  it("markReadyForReview without options sends no body", async () => {
    _mockJsonResponse(MARK_READY_BODY);
    await markReadyForReview(SESSION_ID);
    const [, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(init?.body).toBeUndefined();
  });

  it("fetchSharedInspect rejects a missing import_allowed", async () => {
    const body = _buildValidSharedInspectBody();
    delete body.import_allowed;
    _mockJsonResponse(body);
    await expect(fetchSharedInspect("any-token")).rejects.toMatchObject({
      detail: expect.stringContaining("shared-inspect"),
    });
  });

  it("fetchSharedInspect rejects a malformed files entry", async () => {
    _mockJsonResponse(_buildValidSharedInspectBody({ files: [{ filename: "a.csv" }] }));
    await expect(fetchSharedInspect("any-token")).rejects.toMatchObject({
      detail: expect.stringContaining("shared-inspect"),
    });
  });

  it("fetchShareableFiles GETs the URL and parses the response", async () => {
    _mockJsonResponse({
      files: [{ filename: "a.csv", size_bytes: 3, used_by: ["source"] }],
      importable: false,
      not_importable_reason: "too big",
    });
    const response = await fetchShareableFiles(SESSION_ID);
    expect(response.files[0].used_by).toEqual(["source"]);
    expect(response.not_importable_reason).toBe("too big");
    const [url, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe(`/api/sessions/${SESSION_ID}/shareable-files`);
    expect(init?.method).toBe("GET");
  });

  it("fetchShareableFiles rejects a malformed body", async () => {
    _mockJsonResponse({ files: [{ filename: "a.csv", size_bytes: 3 }], importable: true, not_importable_reason: null });
    await expect(fetchShareableFiles(SESSION_ID)).rejects.toMatchObject({
      detail: expect.stringContaining("shareable-files"),
    });
  });

  it("importSharedSnapshot POSTs the URL-encoded import path and parses 201", async () => {
    _mockJsonResponse({ session_id: SESSION_ID, state_id: "st-1" }, 201);
    const response = await importSharedSnapshot("a/b");
    expect(response).toEqual({ session_id: SESSION_ID, state_id: "st-1" });
    const [url, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe(`/api/sessions/shared/${encodeURIComponent("a/b")}/import`);
    expect(init?.method).toBe("POST");
  });

  it("importSharedSnapshot rejects a malformed body", async () => {
    _mockJsonResponse({ session_id: SESSION_ID }, 201);
    await expect(importSharedSnapshot("tk")).rejects.toMatchObject({
      detail: expect.stringContaining("shared-import"),
    });
  });

  it("importSharedSnapshot does not log out on a capability 401", async () => {
    localStorage.setItem("auth_token", "reviewer-session-token");
    useAuthStore.setState({ token: "reviewer-session-token", isLoading: false });
    _mockJsonResponse({ detail: "Invalid or expired share token" }, 401);
    const request = importSharedSnapshot("bad-token");
    await expect(request).rejects.toMatchObject({ status: 401 });
    await expect(request).rejects.not.toMatchObject({ error_type: "authentication_required" });
    expect(useAuthStore.getState().token).toBe("reviewer-session-token");
    expect(localStorage.getItem("auth_token")).toBe("reviewer-session-token");
  });

  it("importSharedSnapshot logs out on a bare Bearer 401", async () => {
    localStorage.setItem("auth_token", "expired-token");
    useAuthStore.setState({ token: "expired-token", isLoading: false });
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "Token expired" }), {
        status: 401,
        headers: { "content-type": "application/json", "WWW-Authenticate": "Bearer" },
      }),
    );
    await expect(importSharedSnapshot("tk")).rejects.toMatchObject({
      status: 401,
      error_type: "authentication_required",
    });
    expect(useAuthStore.getState().token).toBeNull();
  });
});
