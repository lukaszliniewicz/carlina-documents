import { describe, expect, it } from "vitest";
import {
  DEFAULT_TRANSLATION_BATCH_MAX_BLOCKS,
  DEFAULT_TRANSLATION_BATCH_INPUT_TOKENS,
  TRANSLATION_BATCH_INPUT_STEP,
  isValidTranslationBatchMaxBlocks,
  isValidTranslationBatchInputTokens
} from "./translation-batch-limits.js";

describe("translation batch input limits", () => {
  it("keeps the balanced default while permitting focused 2,048-token windows", () => {
    expect(DEFAULT_TRANSLATION_BATCH_INPUT_TOKENS).toBe(16000);
    expect(TRANSLATION_BATCH_INPUT_STEP).toBe(1);
    expect(isValidTranslationBatchInputTokens(2048)).toBe(true);
    expect(DEFAULT_TRANSLATION_BATCH_MAX_BLOCKS).toBe(4);
    expect(isValidTranslationBatchMaxBlocks(1)).toBe(true);
    expect(isValidTranslationBatchMaxBlocks(256)).toBe(true);
  });

  it("rejects values below the supported floor or above the hard ceiling", () => {
    expect(isValidTranslationBatchInputTokens(1024)).toBe(false);
    expect(isValidTranslationBatchInputTokens(1000001)).toBe(false);
    expect(isValidTranslationBatchMaxBlocks(0)).toBe(false);
    expect(isValidTranslationBatchMaxBlocks(257)).toBe(false);
  });

  it("requires a whole token count", () => {
    expect(isValidTranslationBatchInputTokens(2048.5)).toBe(false);
    expect(isValidTranslationBatchInputTokens("not-a-number")).toBe(false);
    expect(isValidTranslationBatchMaxBlocks(24.5)).toBe(false);
  });
});
