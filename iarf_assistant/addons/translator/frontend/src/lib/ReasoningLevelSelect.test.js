import { cleanup, fireEvent, render } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import ReasoningLevelSelect from "./ReasoningLevelSelect.svelte";

describe("ReasoningLevelSelect", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("aborts its settings request on teardown", async () => {
    const fetchMock = vi.fn(() => new Promise(() => {}));
    vi.stubGlobal("fetch", fetchMock);
    const view = render(ReasoningLevelSelect, {
      props: {
        models: [{ id: "model", supports_reasoning: true, supported_reasoning_levels: ["low"] }],
        modelId: "model",
        slot: "translation"
      }
    });

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
    const request = fetchMock.mock.calls[0][1];
    expect(request.credentials).toBe("same-origin");
    expect(request.signal).toBeInstanceOf(AbortSignal);
    expect(request.signal.aborted).toBe(false);
    view.unmount();
    expect(request.signal.aborted).toBe(true);
  });

  it("persists one exact slot and reports busy until the response is durable", async () => {
    let finishSave;
    const saveResponse = new Promise((resolve) => { finishSave = resolve; });
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ slots: {} }), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      }))
      .mockImplementationOnce(() => saveResponse);
    vi.stubGlobal("fetch", fetchMock);
    const onbusychange = vi.fn();
    const view = render(ReasoningLevelSelect, {
      props: {
        models: [{ id: "model", supports_reasoning: true, supported_reasoning_levels: ["off", "high"] }],
        modelId: "model",
        slot: "translation",
        label: "Translation reasoning",
        onbusychange
      }
    });

    const select = await view.findByRole("combobox", { name: "Translation reasoning" });
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
    await fireEvent.change(select, { target: { value: "off" } });
    await vi.waitFor(() => expect(onbusychange).toHaveBeenCalledWith(true));
    expect(select.disabled).toBe(true);
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({
      slots: { translation: { model_id: "model", level: "off" } }
    });

    finishSave(new Response(JSON.stringify({
      slots: { translation: { model_id: "model", level: "off" } }
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    await vi.waitFor(() => expect(onbusychange).toHaveBeenLastCalledWith(false));
    expect(select.disabled).toBe(false);
  });

  it("does not invent reasoning levels when capability metadata is missing", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ slots: {} }), {
      status: 200,
      headers: { "Content-Type": "application/json" }
    })));
    const view = render(ReasoningLevelSelect, {
      props: {
        models: [{ id: "model", supports_reasoning: true, supported_reasoning_levels: [] }],
        modelId: "model",
        slot: "translation"
      }
    });

    expect(view.queryByRole("combobox")).toBeNull();
    expect(view.getByText(/configurable levels are not recorded/i)).toBeTruthy();
  });
});
