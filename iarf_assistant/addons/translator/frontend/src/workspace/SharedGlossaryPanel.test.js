import { cleanup, fireEvent, render, waitFor } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import SharedGlossaryPanel from "./SharedGlossaryPanel.svelte";
import { createSharedGlossaryClient, glossaryMutation } from "./shared-glossary-api.js";

const contract = "translator-shared-glossary-v1";
const pagination = { next_offset: null };
afterEach(cleanup);
const asset = { contract, id: "asset-1", name: "Collected essays", source_lang: "de", target_lang: "en", revision: 2, items: [], proposals: [], pagination };
const term = { concept_id: "local-1", revision: 1, source_term: "Gemeinde", target_term: "congregation", sense: "religious", authority: "model_advisory" };

function setup({ locked = false, operationActive = false, failApply = false } = {}) {
  const local = { ...term, authority: locked ? "human_locked" : "model_advisory" };
  const preview = {
    contract, project_id: "project-1", asset_id: "asset-1", revision: 2, mode: "link", preview_token: "preview-1",
    rows: [{ id: "shared-1", status: locked ? "conflict" : "update", local, incoming: { ...term, target_term: "religious community" }, base: term, can_take_incoming: !locked }]
  };
  let attempts = 0;
  const fetchJson = vi.fn(async (url) => {
    if (url.endsWith("/preview")) return preview;
    if (url.endsWith("/apply")) {
      if (failApply && attempts++ === 0) throw new Error("Connection lost. Retry the same action.");
      return { contract };
    }
    if (url.includes("/shared-glossaries/asset-1")) return asset;
    if (url.includes("/shared-glossaries?")) return { contract, items: [asset], pagination };
    if (url.includes("/shared-glossary?")) return { contract, project_id: "project-1", binding: null, local_terms: [local], pagination };
    throw new Error(`Unexpected fixture route: ${url}`);
  });
  const onCheckTranslation = vi.fn();
  const onApplied = vi.fn();
  const view = render(SharedGlossaryPanel, { props: { client: { fetchJson }, projectId: "project-1", operationActive, onCheckTranslation, onApplied } });
  return { view, fetchJson, onCheckTranslation, onApplied };
}

async function selectAsset(view) {
  await waitFor(() => expect(view.getByRole("option", { name: /Collected essays/ })).toBeTruthy());
  await waitFor(() => expect(view.getByLabelText("Shared glossary").disabled).toBe(false));
  await fireEvent.change(view.getByLabelText("Shared glossary"), { target: { value: "asset-1" } });
  await waitFor(() => expect(view.getByRole("button", { name: "Review and link" })).toBeTruthy());
}

describe("shared glossary review", () => {
  it("requires a decision before adoption and offers checking existing prose", async () => {
    const { view, fetchJson, onApplied, onCheckTranslation } = setup();
    await selectAsset(view);
    await fireEvent.click(view.getByRole("button", { name: "Review and link" }));
    const apply = await view.findByRole("button", { name: "Adopt reviewed revision" });
    expect(apply.disabled).toBe(true);
    await fireEvent.change(view.getByLabelText("Decision for Gemeinde"), { target: { value: "incoming" } });
    await fireEvent.click(apply);
    await waitFor(() => expect(onApplied).toHaveBeenCalledOnce());
    const request = fetchJson.mock.calls.find(([url]) => url.endsWith("/apply"));
    expect(JSON.parse(request[1].body)).toEqual(expect.objectContaining({
      preview_token: "preview-1", decisions: [{ id: "shared-1", choice: "incoming" }], revision: 2
    }));
    expect(view.getByRole("status").textContent).toContain("Existing translated text is unchanged");
    await fireEvent.click(view.getByRole("button", { name: "Check existing translation" }));
    expect(onCheckTranslation).toHaveBeenCalledOnce();
  });

  it("does not offer to replace a human lock", async () => {
    const { view } = setup({ locked: true });
    await selectAsset(view);
    await fireEvent.click(view.getByRole("button", { name: "Review and link" }));
    await view.findByLabelText("Decision for Gemeinde");
    expect(view.queryByRole("option", { name: "Use shared term" })).toBeNull();
    expect(view.getByText(/This term is human-locked/)).toBeTruthy();
    await fireEvent.click(view.getByRole("button", { name: "Select non-conflicting updates" }));
    expect(view.getByRole("button", { name: "Adopt reviewed revision" }).disabled).toBe(true);
  });

  it("retains the retry key when the apply result is uncertain", async () => {
    const { view, fetchJson, onApplied } = setup({ failApply: true });
    await selectAsset(view);
    await fireEvent.click(view.getByRole("button", { name: "Review and link" }));
    await view.findByLabelText("Decision for Gemeinde");
    await fireEvent.change(view.getByLabelText("Decision for Gemeinde"), { target: { value: "incoming" } });
    await fireEvent.click(view.getByRole("button", { name: "Adopt reviewed revision" }));
    await view.findByRole("alert");
    await fireEvent.click(view.getByRole("button", { name: "Adopt reviewed revision" }));
    await waitFor(() => expect(onApplied).toHaveBeenCalledOnce());
    const requests = fetchJson.mock.calls.filter(([url]) => url.endsWith("/apply")).map(([, options]) => JSON.parse(options.body));
    expect(requests).toHaveLength(2);
    expect(requests[0].idempotency_key).toBe(requests[1].idempotency_key);
  });

  it("disables import during a translation run", async () => {
    const { view } = setup({ operationActive: true });
    await selectAsset(view);
    expect(view.getByRole("button", { name: "Review and link" }).disabled).toBe(true);
    expect(view.getByRole("button", { name: "Copy selected terms…" }).disabled).toBe(true);
  });
});

describe("shared glossary API", () => {
  it("loads every page and rejects nonadvancing pagination", async () => {
    const fetchJson = vi.fn().mockResolvedValueOnce({ contract, items: [asset], pagination: { next_offset: 1 } })
      .mockResolvedValueOnce({ contract, items: [{ ...asset, id: "asset-2" }], pagination });
    const api = createSharedGlossaryClient({ fetchJson }, "project");
    expect((await api.list()).items).toHaveLength(2);
    expect(fetchJson.mock.calls[1][0]).toContain("offset=1");
    fetchJson.mockResolvedValue({ contract, items: [], pagination: { next_offset: 0 } });
    await expect(api.list()).rejects.toThrow("did not finish loading");
  });

  it("changes a retry key only when the intended request changes", () => {
    const first = glossaryMutation(null, "apply", { revision: 1 });
    expect(glossaryMutation(first, "apply", { revision: 1 })).toBe(first);
    expect(glossaryMutation(first, "apply", { revision: 2 }).key).not.toBe(first.key);
  });
});
