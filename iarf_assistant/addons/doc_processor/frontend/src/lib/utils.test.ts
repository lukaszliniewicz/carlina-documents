import { describe, expect, it } from "vitest";
import type { ModelOption } from "./types";
import { selectDefaultModels } from "./utils";

const model = (id: string, overrides: Partial<ModelOption> = {}): ModelOption => ({ id, name: id, ...overrides });

describe("selectDefaultModels", () => {
  it("prefers a vision-capable preset over a text-only preset for PDF Book Quality", () => {
    const defaults = selectDefaultModels([
      model("text-medium", { is_preset_medium: true }),
      model("vision-pro", { supports_vision: true }),
      model("vision-flash", { supports_vision: true }),
      model("vision-medium", { supports_vision: true, is_preset_medium: true })
    ]);

    expect(defaults).toEqual({ parsingModel: "vision-medium", metadataModel: "vision-medium" });
  });

  it("uses a vision Pro for extraction and a vision Flash for metadata without a vision preset", () => {
    const defaults = selectDefaultModels([
      model("text-flash"),
      model("vision-pro", { supports_vision: true }),
      model("vision-flash", { supports_vision: true })
    ]);

    expect(defaults).toEqual({ parsingModel: "vision-pro", metadataModel: "vision-flash" });
  });

  it("falls back safely when no vision-capable model is available", () => {
    expect(selectDefaultModels([model("text-pro"), model("text-flash")])).toEqual({
      parsingModel: "text-pro",
      metadataModel: "text-flash"
    });
    expect(selectDefaultModels([])).toEqual({ parsingModel: "", metadataModel: "" });
  });
});
