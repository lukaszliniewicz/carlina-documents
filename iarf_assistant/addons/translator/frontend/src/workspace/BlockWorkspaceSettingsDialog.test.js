import { cleanup, fireEvent, render } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";
import BlockWorkspaceSettingsDialog from "./BlockWorkspaceSettingsDialog.svelte";

const staleSelection = {
  tool_id: "old.search",
  provider_id: "old-provider",
  provider_domain: "old-domain",
  addon_id: "old-addon",
  connection_mode: "saved",
  connection_id: "old-connection"
};

const executableTool = {
  tool_id: "web.search",
  tool_name: "Search",
  provider_id: "search-provider",
  provider_name: "Search provider",
  provider_domain: "web",
  addon_id: "research-addon",
  connection_modes: ["connectionless"],
  connections: [],
  executable: true,
  private_data_capable: false,
  authority_warning: "",
  unavailable_reason: ""
};

const readyModels = [
  { id: "translator-ready", name: "Translator ready", provider: "Test" },
  { id: "analyzer-ready", name: "Analyzer ready", provider: "Test" },
  { id: "researcher-ready", name: "Researcher ready", provider: "Test" }
];

function form(overrides = {}) {
  return {
    translator_model: "translator-ready",
    analyzer_model: "analyzer-ready",
    researcher_model: "researcher-ready",
    enable_pre_analysis: true,
    enable_research: false,
    enable_translator_notes: false,
    annotation_profile: "none",
    translation_unit_mode: "block",
    research_tool_selections: [],
    translation_batch_input_tokens: 16000,
    custom_prompt: "",
    ...overrides
  };
}

describe("BlockWorkspaceSettingsDialog", () => {
  it("lets a passive translator retain context and fences changes during an operation", async () => {
    const onFieldChange = vi.fn();
    const view = render(BlockWorkspaceSettingsDialog, { props: {
      open: true, executionMode: "passive", settingsForm: form(), onFieldChange
    } });
    expect(view.getByRole("radio", { name: /Fresh context for each batch/ }).checked).toBe(true);
    const retained = view.getByRole("radio", { name: /Continue in the same conversation/ });
    await fireEvent.click(retained);
    expect(onFieldChange).toHaveBeenCalledWith("translation_context_mode", "retained");
    await view.rerender({ operationActive: true });
    expect(retained.disabled).toBe(true);
    await view.rerender({ executionMode: "managed", operationActive: false });
    expect(view.queryByRole("radio", { name: /Continue in the same conversation/ })).toBeNull();
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("preserves unavailable exact selections so owners can see and uncheck them", async () => {
    const onFieldChange = vi.fn();
    const view = render(BlockWorkspaceSettingsDialog, {
      props: {
        open: true,
        models: readyModels,
        settingsForm: form({ research_tool_selections: [staleSelection] }),
        researchTools: [executableTool],
        onFieldChange
      }
    });

    const staleChoice = view.getByRole("checkbox", { name: /old\.search — old-provider/i });
    expect(staleChoice.checked).toBe(true);
    expect(staleChoice.disabled).toBe(false);
    expect(view.getByText(/unavailable — uncheck this saved selection/i)).toBeTruthy();
    expect(view.container.textContent).not.toContain("chunk size");
    expect(view.container.textContent).not.toContain("research_plugin_ids");

    await fireEvent.click(staleChoice);
    expect(onFieldChange).toHaveBeenCalledWith("research_tool_selections", []);
  });

  it("prevents saving research without analysis and an executable exact selection", () => {
    const onSubmit = vi.fn();
    const view = render(BlockWorkspaceSettingsDialog, {
      props: {
        open: true,
        models: readyModels,
        settingsForm: form({ enable_pre_analysis: false, enable_research: true, research_tool_selections: [staleSelection] }),
        researchTools: [executableTool],
        onSubmit
      }
    });

    expect(view.getByRole("alert").textContent).toMatch(/preliminary terminology analysis/i);
    expect(view.getByRole("button", { name: "Save settings" }).disabled).toBe(true);
    expect(view.getByRole("checkbox", { name: /run preliminary terminology analysis/i }).disabled).toBe(true);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("allows a valid exact selection and keeps the research dependency enabled", async () => {
    const onFieldChange = vi.fn();
    const view = render(BlockWorkspaceSettingsDialog, {
      props: {
        open: true,
        models: readyModels,
        settingsForm: form({ enable_research: true, research_tool_selections: [{
          tool_id: executableTool.tool_id,
          provider_id: executableTool.provider_id,
          provider_domain: executableTool.provider_domain,
          addon_id: executableTool.addon_id,
          connection_mode: "connectionless",
          connection_id: ""
        }] }),
        researchTools: [executableTool],
        onFieldChange
      }
    });

    expect(view.getByRole("button", { name: "Save settings" }).disabled).toBe(false);
    const analysis = view.getByRole("checkbox", { name: /run preliminary terminology analysis/i });
    expect(analysis.checked).toBe(true);
    expect(analysis.disabled).toBe(true);
    await fireEvent.click(view.getByRole("checkbox", { name: /enable preliminary research/i }));
    expect(onFieldChange).toHaveBeenCalledWith("enable_research", false);
  });

  it("exposes restrained translator notes as an explicit opt-in", async () => {
    const onFieldChange = vi.fn();
    const view = render(BlockWorkspaceSettingsDialog, {
      props: {
        open: true,
        models: readyModels,
        settingsForm: form(),
        researchTools: [executableTool],
        onFieldChange
      }
    });

    const toggle = view.getByRole("radio", { name: /restrained/i });
    expect(toggle.checked).toBe(false);
    expect(view.container.textContent).toMatch(/cultural or conceptual context/i);
    await fireEvent.click(toggle);
    expect(onFieldChange).toHaveBeenCalledWith("annotation_profile", "restrained");
  });

  it("keeps block mode as the default and offers aligned sentence groups explicitly", async () => {
    const onFieldChange = vi.fn();
    const view = render(BlockWorkspaceSettingsDialog, {
      props: {
        open: true,
        models: readyModels,
        settingsForm: form(),
        researchTools: [executableTool],
        onFieldChange
      }
    });

    expect(view.getByRole("radio", { name: /Semantic blocks/i }).checked).toBe(true);
    const aligned = view.getByRole("radio", { name: /Aligned sentence groups/i });
    expect(aligned.checked).toBe(false);
    expect(view.getByText(/Segment the source sentences first/i)).toBeTruthy();
    await fireEvent.click(aligned);
    expect(onFieldChange).toHaveBeenCalledWith("translation_unit_mode", "aligned_sentences");
  });

  it("allows passive projects to save Study settings without a Carlina model", async () => {
    const onFieldChange = vi.fn();
    const view = render(BlockWorkspaceSettingsDialog, {
      props: {
        open: true,
        executionMode: "passive",
        models: readyModels,
        settingsForm: form({
          translator_model: "",
          analyzer_model: "",
          researcher_model: "",
          enable_pre_analysis: false,
          annotation_profile: "restrained",
          enable_translator_notes: true
        }),
        researchTools: [executableTool],
        onFieldChange
      }
    });

    expect(view.getByText(/external executor supplies the model/i)).toBeTruthy();
    expect(view.getByRole("combobox", { name: "Translator model" }).disabled).toBe(true);
    expect(view.getByRole("button", { name: "Save settings" }).disabled).toBe(false);

    await fireEvent.click(view.getByRole("radio", { name: /Study translation/i }));
    expect(onFieldChange).toHaveBeenCalledWith("annotation_profile", "study");
  });

  it("visibly selects the minimal Jina defaults when research is enabled", async () => {
    const onFieldChange = vi.fn();
    const search = {
      ...executableTool,
      tool_id: "web_search.text",
      tool_name: "Text Search",
      provider_id: "jina",
      provider_name: "Live Web Search (Jina first)",
      provider_domain: "web_search",
      addon_id: "web_search",
      research_section: "web",
      research_role: "general_search",
      research_recommended: true,
      research_priority: 10
    };
    const reader = {
      ...executableTool,
      tool_id: "scraping.get_web_content",
      tool_name: "Get Web Content",
      provider_id: "jina",
      provider_name: "Jina Reader API",
      provider_domain: "scraping",
      addon_id: "scraping",
      research_section: "web",
      research_role: "page_reader",
      research_recommended: true,
      research_priority: 20
    };
    const view = render(BlockWorkspaceSettingsDialog, {
      props: {
        open: true,
        models: readyModels,
        settingsForm: form(),
        researchTools: [search, reader],
        onFieldChange
      }
    });

    await fireEvent.click(view.getByRole("checkbox", { name: /enable preliminary research/i }));

    expect(onFieldChange).toHaveBeenCalledWith("enable_pre_analysis", true);
    expect(onFieldChange).toHaveBeenCalledWith("research_tool_selections", [
      expect.objectContaining({ tool_id: "web_search.text", provider_id: "jina" }),
      expect.objectContaining({ tool_id: "scraping.get_web_content", provider_id: "jina" })
    ]);
    expect(onFieldChange).toHaveBeenCalledWith("enable_research", true);
  });

  it("disables settings controls while an operation or export is active", () => {
    const view = render(BlockWorkspaceSettingsDialog, {
      props: {
        open: true,
        models: readyModels,
        settingsForm: form(),
        researchTools: [executableTool],
        operationActive: true
      }
    });

    expect(view.getByRole("combobox", { name: "Translator model" }).disabled).toBe(true);
    expect(view.getByRole("spinbutton", { name: /translation batch input budget/i }).disabled).toBe(true);
    expect(view.getByRole("textbox", { name: /Custom prompt/ }).disabled).toBe(true);
    expect(view.getByRole("button", { name: "Save settings" }).disabled).toBe(true);
  });

  it("groups the three model phases into calm disclosure sections", () => {
    const view = render(BlockWorkspaceSettingsDialog, {
      props: {
        open: true,
        models: readyModels,
        settingsForm: form(),
        researchTools: [executableTool]
      }
    });

    const sections = [...view.container.querySelectorAll("details.settings-section")];
    expect(sections).toHaveLength(3);
    expect(sections[0].open).toBe(true);
    expect(sections[1].open).toBe(false);
    expect(sections[2].open).toBe(false);
    expect(sections.map((section) => section.querySelector("summary").textContent)).toEqual([
      expect.stringMatching(/Translation.*Translator ready/s),
      expect.stringMatching(/Preliminary analysis.*Analyzer ready.*On/s),
      expect.stringMatching(/Research.*Researcher ready.*Off/s)
    ]);
  });

  it("validates the configurable token-aware batch budget", async () => {
    const onFieldChange = vi.fn();
    const view = render(BlockWorkspaceSettingsDialog, {
      props: {
        open: true,
        models: readyModels,
        settingsForm: form(),
        researchTools: [executableTool],
        onFieldChange
      }
    });

    const budget = view.getByRole("spinbutton", { name: /translation batch input budget/i });
    expect(budget.value).toBe("16000");
    expect(budget.min).toBe("2048");
    expect(budget.step).toBe("1");
    expect(budget.checkValidity()).toBe(true);
    const blockLimit = view.getByRole("spinbutton", { name: /maximum semantic blocks per batch/i });
    expect(blockLimit.value).toBe("4");
    expect(blockLimit.min).toBe("1");
    expect(blockLimit.max).toBe("256");
    expect(blockLimit.checkValidity()).toBe(true);
    expect(view.getByText(/stops before contacting the model/i)).toBeTruthy();
    await fireEvent.input(budget, { target: { value: "128000" } });
    expect(onFieldChange).toHaveBeenCalledWith("translation_batch_input_tokens", 128000);
    await fireEvent.input(blockLimit, { target: { value: "18" } });
    expect(onFieldChange).toHaveBeenCalledWith("translation_batch_max_blocks", 18);

    await view.rerender({ settingsForm: form({ translation_batch_input_tokens: 2048 }) });
    expect(view.getByRole("button", { name: "Save settings" }).disabled).toBe(false);
    expect(view.queryByText(/use a batch budget between/i)).toBeNull();

    await view.rerender({ settingsForm: form({ translation_batch_input_tokens: 1024 }) });
    expect(view.getByRole("button", { name: "Save settings" }).disabled).toBe(true);
    expect(view.getByText(/between 2,048 and 1,000,000 tokens/i)).toBeTruthy();

    await view.rerender({ settingsForm: form({ translation_batch_max_blocks: 257 }) });
    expect(view.getByRole("button", { name: "Save settings" }).disabled).toBe(true);
    expect(view.getByText(/block limit between 1 and 256/i)).toBeTruthy();
  });

  it("exposes the exact reasoning slot for each enabled model phase", async () => {
    const reasoningModels = readyModels.map((model) => ({
      ...model,
      supports_reasoning: true,
      supported_reasoning_levels: ["off", "high"]
    }));
    const fetchMock = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ slots: {} }), {
      status: 200,
      headers: { "Content-Type": "application/json" }
    }));
    vi.stubGlobal("fetch", fetchMock);
    const view = render(BlockWorkspaceSettingsDialog, {
      props: {
        open: true,
        models: reasoningModels,
        settingsForm: form({ enable_research: true }),
        researchTools: [executableTool]
      }
    });

    expect(await view.findByRole("combobox", { name: "Translation reasoning" })).toBeTruthy();
    expect(view.getByRole("combobox", { name: "Analysis reasoning" })).toBeTruthy();
    expect(view.getByRole("combobox", { name: "Research reasoning" })).toBeTruthy();
    await fireEvent.change(view.getByRole("combobox", { name: "Translation reasoning" }), { target: { value: "off" } });
    await vi.waitFor(() => expect(fetchMock.mock.calls.some(([, options]) => {
      if (options?.method !== "PUT") return false;
      return JSON.parse(options.body).slots.translation?.level === "off";
    })).toBe(true));
  });
});
