import { fireEvent, render, screen } from "@testing-library/svelte";
import { describe, expect, it, vi } from "vitest";

import ChatComposer from "../../src/components/chat/ChatComposer.svelte";

const config = {
  model: "",
  thinkingLevel: "default",
  assistantMode: "default" as const,
  capabilityIds: [],
  runtimeProfileIds: [],
  enableDelegation: false,
  runtimePolicy: "review" as const,
  runtimePolicySource: "administrator" as const,
  contextMode: "assisted" as const,
  voiceModel: "",
  voice: "",
};

function renderComposer(skills = [{
  id: "research.web",
  name: "Web research",
  description: "Find primary sources.",
  source: "research",
}]) {
  return render(ChatComposer, {
    props: {
      config,
      attachments: [],
      models: [],
      capabilities: [],
      commands: [],
      skills,
      optionsLoading: false,
      busy: false,
      uploading: false,
      voiceModels: [],
      voices: [],
      voiceLoading: false,
      administratorApprovalMode: "review" as const,
      onsend: vi.fn(),
      onstop: vi.fn(),
      onupload: vi.fn(async () => {}),
      onremoveattachment: vi.fn(),
      onstrategy: vi.fn(),
      onloadvoice: vi.fn(async () => {}),
      oncommand: vi.fn(),
      oninlinequery: vi.fn(async () => []),
      onruntimepolicychange: vi.fn(),
      onrouting: vi.fn(),
      onannounce: vi.fn(),
    },
  });
}

describe("chat composer skill picker", () => {
  it("shows matching allowed skills for /sk and inserts the established /skill contract", async () => {
    renderComposer();
    const input = screen.getByRole("textbox", { name: "Message" });

    await fireEvent.input(input, { target: { value: "/sk web" } });
    expect(screen.getByRole("listbox", { name: "Available skills" })).not.toBeNull();
    await fireEvent.click(screen.getByRole("option", { name: /web research/i }));

    expect((input as HTMLTextAreaElement).value).toBe("/skill research.web ");
  });

  it("does not expose unavailable skills when discovery returns none", async () => {
    renderComposer([]);
    const input = screen.getByRole("textbox", { name: "Message" });

    await fireEvent.input(input, { target: { value: "/sk restricted" } });
    expect(screen.getByText(/unavailable skills are not selectable/i)).not.toBeNull();
    expect(screen.queryByRole("option")).toBeNull();
  });
});
