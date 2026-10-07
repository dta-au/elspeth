import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ImportYamlModal } from "./ImportYamlModal";
import { useSessionStore } from "@/stores/sessionStore";
import { useExecutionStore } from "@/stores/executionStore";
import { advanceAuthGeneration } from "@/api/authSession";
import { compositionStateAuthorityFields } from "@/test/composerFixtures";
vi.mock("@/api/client", () => ({ importCompositionYaml: vi.fn(), listBlobs: vi.fn(), uploadBlob: vi.fn(), getPluginSchema: vi.fn() }));
import * as api from "@/api/client";
const sid = "11111111-1111-4111-8111-111111111111", other = "22222222-2222-4222-8222-222222222222";
const select = vi.fn(), validate = vi.fn();
const result = { ...compositionStateAuthorityFields, id: other, session_id: sid, version: 2, sources: {}, nodes: [], edges: [], outputs: [], metadata: { name: null, description: null } };
const yaml = "sources:\n  source:\n    plugin: csv\n    on_success: result\nsinks:\n  result:\n    plugin: json\n    on_write_failure: fail\n";
beforeEach(() => { vi.resetAllMocks(); useSessionStore.getState().reset(); useSessionStore.setState({ activeSessionId: sid, compositionState: null, compositionStateLoaded: true, selectSession: select }); useExecutionStore.setState({ validate }); vi.mocked(api.listBlobs).mockResolvedValue([]); });
afterEach(cleanup);
async function submit(): Promise<void> {
  render(<ImportYamlModal onClose={vi.fn()} />);
  fireEvent.change(screen.getByLabelText(/pipeline yaml/i), { target: { value: yaml } });
  await waitFor(() => expect(screen.getByRole("button", { name: /^import$/i })).toBeEnabled());
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: /^import$/i })); });
  expect(api.importCompositionYaml).toHaveBeenCalledWith(sid, yaml);
}
it("same-session import refreshes its current session and validation", async () => {
  vi.mocked(api.importCompositionYaml).mockResolvedValue(result);
  await submit(); expect(select).toHaveBeenCalledWith(sid); expect(validate).toHaveBeenCalledWith(sid);
});
it.each(["session", "auth"])("a held import cannot publish or reselect after %s ownership changes", async (change) => {
  let release!: (value: typeof result) => void;
  vi.mocked(api.importCompositionYaml).mockReturnValue(new Promise((resolve) => { release = resolve; }));
  await submit();
  await act(async () => { if (change === "session") useSessionStore.getState().resetForTutorialSession(other); else advanceAuthGeneration(); release(result); });
  expect(select).not.toHaveBeenCalled(); expect(validate).not.toHaveBeenCalled();
  expect(screen.queryByText("Imported as version 2.")).not.toBeInTheDocument();
});
