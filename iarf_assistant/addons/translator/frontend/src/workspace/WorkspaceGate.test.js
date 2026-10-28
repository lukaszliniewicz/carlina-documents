import { cleanup, fireEvent, render, waitFor } from "@testing-library/svelte";
import { describe, expect, it, vi, afterEach } from "vitest";
import WorkspaceGate from "./WorkspaceGate.svelte";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function response(payload, status = 200) {
  return new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });
}

describe("WorkspaceGate", () => {
  it("uses the authoritative engine and keeps block workspaces off legacy endpoints", async () => {
    const fetchMock = vi.fn((url) => {
      if (url.includes("/api/projects/project-1/block-workspace")) {
        return Promise.resolve(response({
          contract: "translator-block-workspace-v5",
          access: { role: "owner", label: null },
          project: { engine_contract: "document-block-v1", name: "Blocks" },
          source_plan: { id: "plan-1", version: 1 },
          latest_operation: {},
          blocks: [],
          pagination: { offset: 0, limit: 40, total: 0, has_more: false },
          target_summary: {
            total_blocks: 0,
            active_targets: 0,
            structurally_valid_active_targets: 0,
            document_complete: true
          }
        }));
      }
      return Promise.resolve(response({
        id: "project-1",
        name: "Blocks",
        source_lang: "English",
        target_lang: "Polish",
        engine_contract: "document-block-v1"
      }));
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(WorkspaceGate, { props: { bootstrapData: { projectId: "project-1" } } });
    await waitFor(() => expect(view.getByTestId("block-workspace")).toBeTruthy());
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("workspace_state"))).toBe(false);
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("segments"))).toBe(false);
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("block-workspace"))).toBe(true);
  });

  it("fails closed when the project engine contract is unknown", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response({ engine_contract: "mystery-v9" }))));
    const view = render(WorkspaceGate, { props: { bootstrapData: { projectId: "project-2" } } });
    await waitFor(() => expect(view.getByRole("alert")).toBeTruthy());
    expect(view.getByText(/no recognized engine contract/i)).toBeTruthy();
  });

  it("mounts legacy only when the authoritative contract field is omitted", async () => {
    const fetchMock = vi.fn((url) => {
      if (String(url).includes("workspace_state")) {
        return Promise.resolve(response({
          project: { id: "project-legacy", name: "Legacy" },
          segments: [],
          translation_status: {}
        }));
      }
      return Promise.resolve(response({ id: "project-legacy", name: "Legacy" }));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(WorkspaceGate, { props: { bootstrapData: { projectId: "project-legacy" } } });
    await waitFor(() => expect(
      fetchMock.mock.calls.some(([url]) => String(url).includes("workspace_state"))
    ).toBe(true));
  });

  it("rejects an explicitly blank engine contract", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(response({ engine_contract: "" }))));
    const view = render(WorkspaceGate, { props: { bootstrapData: { projectId: "project-blank" } } });
    await waitFor(() => expect(view.getByRole("alert")).toBeTruthy());
    expect(view.getByText(/no recognized engine contract/i)).toBeTruthy();
  });

  it("redeems a fragment capability before loading a review project and clears the secret", async () => {
    window.history.replaceState({}, "", "/addons/translator/review/project-review#access=one-time-secret");
    const fetchMock = vi.fn((url, options = {}) => {
      if (String(url).endsWith("/api/share/redeem")) {
        expect(JSON.parse(options.body)).toEqual({ project_id: "project-review", secret: "one-time-secret" });
        return Promise.resolve(response({ project_id: "project-review", role: "reviewer", label: "Friend", csrf: "csrf-1", expires_at: "2099-01-01T00:00:00Z" }));
      }
      if (String(url).includes("/api/projects/project-review/block-workspace")) {
        expect(options.headers["X-Translator-CSRF"]).toBeUndefined();
        return Promise.resolve(response({
          contract: "translator-block-workspace-v5",
          access: { role: "reviewer", label: "Friend" },
          project: { id: "project-review", engine_contract: "document-block-v1", name: "Review" },
          source_plan: { id: "plan-1", version: 1 },
          latest_operation: {},
          blocks: [],
          pagination: { offset: 0, limit: 40, total: 0, has_more: false },
          target_summary: { total_blocks: 0, active_targets: 0, structurally_valid_active_targets: 0, document_complete: true }
        }));
      }
      return Promise.resolve(response({ id: "project-review", name: "Review", engine_contract: "document-block-v1" }));
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(WorkspaceGate, { props: { bootstrapData: { projectId: "project-review", reviewMode: true } } });
    await waitFor(() => expect(view.getByTestId("block-workspace")).toBeTruthy());
    expect(window.location.hash).toBe("");
    expect(fetchMock.mock.calls[0][0]).toContain("/api/share/redeem");
  });

  it("restores an existing review session with a same-origin POST", async () => {
    window.history.replaceState({}, "", "/addons/translator/review/project-review");
    const fetchMock = vi.fn((url, options = {}) => {
      if (String(url).endsWith("/api/share/session")) {
        expect(options.method).toBe("POST");
        expect(JSON.parse(options.body)).toEqual({ project_id: "project-review" });
        return Promise.resolve(response({ project_id: "project-review", role: "viewer", label: "Reader", csrf: "csrf-2", expires_at: "2099-01-01T00:00:00Z" }));
      }
      if (String(url).includes("/api/projects/project-review/block-workspace")) {
        return Promise.resolve(response({
          contract: "translator-block-workspace-v5",
          access: { role: "viewer", label: "Reader" },
          project: { id: "project-review", engine_contract: "document-block-v1", name: "Review" },
          source_plan: { id: "plan-1", version: 1 },
          latest_operation: {},
          blocks: [],
          pagination: { offset: 0, limit: 40, total: 0, has_more: false },
          target_summary: { total_blocks: 0, active_targets: 0, structurally_valid_active_targets: 0, document_complete: true }
        }));
      }
      return Promise.resolve(response({ id: "project-review", name: "Review", engine_contract: "document-block-v1" }));
    });
    vi.stubGlobal("fetch", fetchMock);
    const view = render(WorkspaceGate, { props: { bootstrapData: { projectId: "project-review", reviewMode: true } } });
    await waitFor(() => expect(view.getByTestId("block-workspace")).toBeTruthy());
    expect(fetchMock.mock.calls[0][0]).toContain("/api/share/session");
  });
});


it("waits for source planning without requesting missing block data", async () => {
  const fetchMock = vi.fn(() => Promise.resolve(response({
    id: "project-planning", engine_contract: "document-block-v1",
    latest_operation: { kind: "plan_document_blocks", state: "running", message: "Segmented 19 source projections; found 41 sentences." }
  })));
  vi.stubGlobal("fetch", fetchMock);
  const view = render(WorkspaceGate, { props: { bootstrapData: { projectId: "project-planning" } } });
  await waitFor(() => expect(view.getByText(/Segmented 19/)).toBeTruthy());
  expect(view.queryByTestId("block-workspace")).toBeNull();
  expect(fetchMock.mock.calls.some(([url]) => String(url).includes("block-workspace"))).toBe(false);
});

it("retries failed planning using the existing project", async () => {
  const fetchMock = vi.fn((url) => Promise.resolve(response(
    String(url).endsWith("/source-planning/retry") ? { project_id: "project-planning" } : {
      id: "project-planning", engine_contract: "document-block-v1",
      latest_operation: { kind: "plan_document_blocks", state: "failed", message: "Segmenter unavailable." }
    }
  )));
  vi.stubGlobal("fetch", fetchMock);
  const view = render(WorkspaceGate, { props: { bootstrapData: { projectId: "project-planning" } } });
  await waitFor(() => expect(view.getByRole("button", { name: "Retry source planning" })).toBeTruthy());
  await fireEvent.click(view.getByRole("button", { name: "Retry source planning" }));
  await waitFor(() => expect(fetchMock.mock.calls.some(([url, options]) =>
    String(url).endsWith("/project-planning/source-planning/retry") && options.method === "POST"
  )).toBe(true));
});
