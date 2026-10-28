import { fireEvent, render, screen } from "@testing-library/svelte";
import { describe, expect, it, vi } from "vitest";

import ModelSelector from "../../src/components/chat/ModelSelector.svelte";

const models = [
  {
    id: "vertex/gemini-pro",
    name: "Gemini Pro",
    provider: "Vertex",
    category: "chat",
    supports_vision: true,
    supports_pdf: true,
    supports_audio_in: false,
  },
  {
    id: "openai/gpt-fast",
    name: "GPT Fast",
    provider: "OpenAI",
    category: "chat",
    supports_vision: false,
    supports_pdf: false,
    supports_audio_in: false,
  },
];

describe("model selector", () => {
  it("keeps automatic selection and presents searchable provider groups", async () => {
    const onchange = vi.fn();
    const onrouting = vi.fn();
    const { container } = render(ModelSelector, { props: { models, value: "", onchange, onrouting } });

    await fireEvent.click(screen.getByRole("combobox", { name: /language model: automatic selection/i }));

    expect(
      screen
        .getByRole("option", { name: /automatic selection/i })
        .getAttribute("aria-selected"),
    ).toBe("true");
    expect(screen.getByRole("group", { name: "OpenAI" })).not.toBeNull();
    expect(screen.getByRole("group", { name: "Vertex" })).not.toBeNull();

    const search = screen.getByRole("combobox", { name: /search language models/i });
    await fireEvent.input(search, { target: { value: "gemini" } });
    expect(screen.getByRole("option", { name: /gemini pro/i })).not.toBeNull();
    expect(screen.queryByRole("option", { name: /gpt fast/i })).toBeNull();
    expect(container.querySelector(".model-selector__panel")?.getAttribute("style")).toContain("left:");
    const routing = screen.getByRole("button", { name: /manage app routing defaults/i });
    expect(routing.closest('[role="listbox"]')).toBeNull();
    await fireEvent.click(routing);
    expect(onrouting).toHaveBeenCalledOnce();
  });

  it("dismisses its fixed viewport layer with Escape", async () => {
    render(ModelSelector, { props: { models, value: "", onchange: vi.fn() } });

    await fireEvent.click(screen.getByRole("combobox", { name: /language model/i }));
    expect(screen.getByRole("listbox", { name: "Language models" })).not.toBeNull();

    await fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("listbox", { name: "Language models" })).toBeNull();
  });

  it("chooses an active result from the keyboard", async () => {
    const onchange = vi.fn();
    render(ModelSelector, { props: { models, value: "", onchange } });

    await fireEvent.click(screen.getByRole("combobox", { name: /language model: automatic selection/i }));
    const search = screen.getByRole("combobox", { name: /search language models/i });
    await fireEvent.keyDown(search, { key: "ArrowDown" });
    await fireEvent.keyDown(search, { key: "Enter" });

    expect(onchange).toHaveBeenCalledWith("openai/gpt-fast");
  });
});
