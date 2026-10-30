export interface PickerOption<Value extends string = string> {
  description?: string;
  disabled?: boolean;
  label: string;
  value: Value;
}

export interface OverlayController<Result = unknown> {
  close(result?: Result): void;
  restoreFocus(): void;
}

export { classNames, prepareDownload } from "./utilities.ts";
export { confirmDiscardChanges, guardUnsavedChanges } from "./unsaved-changes.ts";

export { default as Badge } from "./Badge.svelte";
export { default as Button } from "./Button.svelte";
export { default as Dialog } from "./Dialog.svelte";
export { default as EmptyState } from "./EmptyState.svelte";
export { default as EpubReviewPanel } from "./EpubReviewPanel.svelte";
export { requireEpubPreflight, requireEpubEdition, resetEpubCoverReview, withEpubReviewAnchors } from "./epub.ts";
export type { EpubOptions, EpubPreflight, EpubQuality, EpubRecipe, EpubEdition, EpubImageMode } from "./epub.ts";
export { default as EpubImageFormat } from "./EpubImageFormat.svelte";
export { default as EpubCoverCrop } from "./EpubCoverCrop.svelte";
export { default as EpubEditionControls } from "./EpubEditionControls.svelte";
export { default as Icon } from "./Icon.svelte";
export type { IconName } from "./Icon.svelte";
export { default as IconButton } from "./IconButton.svelte";
export { default as PageHeader } from "./PageHeader.svelte";
export { default as SearchField } from "./SearchField.svelte";
