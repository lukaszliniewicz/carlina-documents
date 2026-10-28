import { cleanup, fireEvent, render } from "@testing-library/svelte";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ProjectCreateDialog from "./ProjectCreateDialog.svelte";

describe("ProjectCreateDialog", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ slots: {} }) })));
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("offers sentence segmentation before the project is created", async () => {
    const onFieldChange = vi.fn();
    const view = render(ProjectCreateDialog, { props: {
      open: true, onFieldChange,
      form: { source_type: "document", translation_unit_mode: "block" }
    } });
    const aligned = view.getByRole("radio", { name: /Aligned sentence groups/ });
    await fireEvent.click(aligned);
    expect(onFieldChange).toHaveBeenCalledWith("translation_unit_mode", "aligned_sentences");
    expect(view.getByText(/requires a working Sentence Segmenter/)).toBeTruthy();
    await view.rerender({ form: { source_type: "document", translation_unit_mode: "aligned_sentences" }, isCreatingProject: true });
    expect(view.getByText(/Preparing and verifying the source sentences/).getAttribute("role")).toBe("status");
    expect(view.getByRole("radio", { name: /Aligned sentence groups/ }).disabled).toBe(true);
    await view.rerender({ form: { source_type: "subtitle_studio" } });
    expect(view.queryByRole("radio", { name: /Aligned sentence groups/ })).toBeNull();
  });

  it("keeps project actions in the dialog footer", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ slots: {} })
    }));
    const onClose = vi.fn();
    const onSubmit = vi.fn((event) => event.preventDefault());
    const view = render(ProjectCreateDialog, {
      props: {
        open: true,
        canCreateProject: true,
        onClose,
        onSubmit,
        modelOptions: [{ id: "ready", name: "Ready" }],
        modelOptionsByProvider: { Test: [{ id: "ready", name: "Ready" }] },
        documents: [{ id: "doc-1", filename: "source.html" }],
        form: {
          name: "New project",
          source_type: "document",
          document_id: "doc-1",
          target_lang: "pl",
          research_plugin_ids: []
        }
      }
    });

    const createButton = view.getByRole("button", { name: "Create Project" });
    expect(createButton.getAttribute("form")).toBe("translator-project-create-form");
    expect(createButton.closest("footer")).toBeTruthy();
    await fireEvent.click(createButton);
    expect(onSubmit).toHaveBeenCalledOnce();

    await fireEvent.click(view.getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalledOnce();
    view.unmount();
  });

  it("does not close while project creation is in flight", async () => {
    const onClose = vi.fn();
    const view = render(ProjectCreateDialog, {
      props: {
        open: true,
        isCreatingProject: true,
        onClose,
        modelOptions: [{ id: "ready", name: "Ready" }],
        modelOptionsByProvider: { Test: [{ id: "ready", name: "Ready" }] },
        documents: [{ id: "doc-1", filename: "source.html" }],
        form: { source_type: "document", document_id: "doc-1", research_plugin_ids: [] }
      }
    });

    await fireEvent.click(view.getByRole("button", { name: "Close dialog" }));
    expect(onClose).not.toHaveBeenCalled();

    await view.rerender({ isCreatingProject: false });
    await fireEvent.click(view.getByRole("button", { name: "Close dialog" }));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("uses semantic document blocks exclusively and keeps subtitles on timed segments", async () => {
    const view = render(ProjectCreateDialog, {
      props: {
        open: true,
        modelOptions: [{ id: "ready", name: "Ready" }],
        modelOptionsByProvider: { Test: [{ id: "ready", name: "Ready" }] },
        documents: [{ id: "doc-1", filename: "source.html" }],
        form: { source_type: "document", engine_contract: "document-block-v1", document_id: "doc-1", research_plugin_ids: [] }
      }
    });
    expect(view.container.textContent).toMatch(/translation engine: semantic document blocks/i);
    expect(view.container.textContent).not.toMatch(/compatibility engine/i);
    const advanced = [...view.container.querySelectorAll("summary")]
      .find((item) => item.textContent === "Advanced Agent Options");
    await fireEvent.click(advanced);
    expect(view.getByRole("radio", { name: /Restrained/i })).toBeTruthy();
    const batchBudget = view.getByRole("spinbutton", { name: /translation batch input budget/i });
    expect(batchBudget.value).toBe("16000");
    expect(batchBudget.min).toBe("2048");
    expect(batchBudget.step).toBe("1");
    expect(batchBudget.checkValidity()).toBe(true);
    const blockLimit = view.getByRole("spinbutton", { name: /maximum semantic blocks per batch/i });
    expect(blockLimit.value).toBe("4");
    expect(blockLimit.min).toBe("1");
    expect(blockLimit.max).toBe("256");
    expect(blockLimit.checkValidity()).toBe(true);
    expect(view.getByText(/stops before contacting the model/i)).toBeTruthy();
    expect(view.getByText(/conservative default/i)).toBeTruthy();
    expect(view.container.textContent).not.toMatch(/translation chunk size/i);
    await view.rerender({ form: { source_type: "subtitle_studio", engine_contract: "segment-v1", subtitle_project_id: "sub-1", research_plugin_ids: [] } });
    expect(view.getByText(/subtitle projects always use the timed segment engine/i)).toBeTruthy();
    expect(view.container.querySelector('input[type="number"][max="20000"]')).toBeTruthy();
  });

  it("renders exact block research choices and keeps unavailable choices non-executable", () => {
    const onToggleResearchTool = vi.fn();
    const view = render(ProjectCreateDialog, {
      props: {
        open: true,
        modelOptions: [{ id: "ready", name: "Ready" }],
        modelOptionsByProvider: { Test: [{ id: "ready", name: "Ready" }] },
        documents: [{ id: "doc-1", filename: "source.html" }],
        form: {
          source_type: "document",
          engine_contract: "document-block-v1",
          document_id: "doc-1",
          enable_pre_analysis: true,
          enable_research: true,
          research_tool_selections: [{
            tool_id: "web.search",
            provider_id: "serp",
            provider_domain: "web",
            addon_id: "research-addon",
            connection_mode: "saved",
            connection_id: "old-account"
          }]
        },
        researchToolOptions: [{
          tool_id: "web.search",
          tool_name: "Search",
          provider_id: "serp",
          provider_name: "Search Provider",
          provider_domain: "web",
          addon_id: "research-addon",
          connection_mode: "saved",
          connection_id: "old-account",
          connection_label: "Unavailable saved selection",
          selection: {
            tool_id: "web.search",
            provider_id: "serp",
            provider_domain: "web",
            addon_id: "research-addon",
            connection_mode: "saved",
            connection_id: "old-account"
          },
          selected: true,
          executable: false,
          unavailable: true,
          private_data_capable: false
        }, {
          tool_id: "archive.search",
          tool_name: "Archive Search",
          provider_id: "archive",
          provider_name: "Archive Provider",
          provider_domain: "archive",
          addon_id: "archive-addon",
          connection_mode: "connectionless",
          connection_id: "",
          connection_label: "Unavailable",
          selection: {
            tool_id: "archive.search",
            provider_id: "archive",
            provider_domain: "archive",
            addon_id: "archive-addon",
            connection_mode: "connectionless",
            connection_id: ""
          },
          selected: false,
          executable: false,
          unavailable: true,
          private_data_capable: false
        }],
        onToggleResearchTool
      }
    });
    expect(view.getByText(/research sources/i)).toBeTruthy();
    expect(view.getAllByText(/unavailable — uncheck this saved selection/i)).toHaveLength(2);
    const researchToggle = view.getAllByLabelText(/enable research during translation/i)[0];
    expect(researchToggle.disabled).toBe(true);
    const staleChoice = view.getByRole("checkbox", { name: /search — search provider/i });
    expect(staleChoice.disabled).toBe(false);
    expect(staleChoice.checked).toBe(true);
    expect(view.getByRole("checkbox", { name: /archive search — archive provider/i }).disabled).toBe(true);
    expect(onToggleResearchTool).not.toHaveBeenCalled();
  });

  it("renders reasoning controls for all three configured model slots", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ slots: {} })
    }));
    const model = {
      id: "mimo",
      name: "MiMo",
      supports_reasoning: true,
      supported_reasoning_levels: ["off", "high"]
    };
    const view = render(ProjectCreateDialog, {
      props: {
        open: true,
        modelOptions: [model],
        modelOptionsByProvider: { Test: [model] },
        documents: [{ id: "doc-1", filename: "source.html" }],
        form: {
          source_type: "document",
          document_id: "doc-1",
          analyzer_model: "mimo",
          translator_model: "mimo",
          researcher_model: "mimo",
          research_plugin_ids: []
        }
      }
    });

    await vi.waitFor(() => {
      const reasoningSelects = [...view.container.querySelectorAll("select")]
        .filter((select) => [...select.options].some((option) => option.value === "high"));
      expect(reasoningSelects).toHaveLength(3);
      expect(reasoningSelects.every((select) => [...select.options]
        .some((option) => option.value === "off"))).toBe(true);
    });
  });
});
