export const DEFAULT_TRANSLATION_BATCH_INPUT_TOKENS = 16000;
export const MIN_TRANSLATION_BATCH_INPUT_TOKENS = 2048;
export const MAX_TRANSLATION_BATCH_INPUT_TOKENS = 1000000;
export const DEFAULT_TRANSLATION_BATCH_MAX_BLOCKS = 4;
export const MIN_TRANSLATION_BATCH_MAX_BLOCKS = 1;
export const MAX_TRANSLATION_BATCH_MAX_BLOCKS = 256;
// Match the integer-valued API contract. A coarser HTML step can make an
// otherwise valid saved/default budget fail native form validation before the
// Svelte submit handler runs (for example, 16,000 is not 2,048 + n * 1,024).
export const TRANSLATION_BATCH_INPUT_STEP = 1;

export function isValidTranslationBatchInputTokens(value) {
  const tokens = Number(value);
  return Number.isInteger(tokens)
    && tokens >= MIN_TRANSLATION_BATCH_INPUT_TOKENS
    && tokens <= MAX_TRANSLATION_BATCH_INPUT_TOKENS;
}

export function isValidTranslationBatchMaxBlocks(value) {
  const blocks = Number(value);
  return Number.isInteger(blocks)
    && blocks >= MIN_TRANSLATION_BATCH_MAX_BLOCKS
    && blocks <= MAX_TRANSLATION_BATCH_MAX_BLOCKS;
}
