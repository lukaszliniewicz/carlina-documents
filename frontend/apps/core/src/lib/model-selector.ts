import type { FrontendChatModel } from "@carlina/sdk";

export interface ModelProviderGroup {
  provider: string;
  models: FrontendChatModel[];
}

function modelProvider(model: FrontendChatModel): string {
  return model.provider.trim() || "Other providers";
}

export function modelSearchText(model: FrontendChatModel): string {
  return `${model.name} ${modelProvider(model)} ${model.id} ${model.category}`
    .toLocaleLowerCase();
}

/**
 * Keep the conversation picker deterministic even when providers return their
 * catalogues in a different order between requests.
 */
export function providerGroupedModels(
  models: readonly FrontendChatModel[],
  query = "",
): ModelProviderGroup[] {
  const needle = query.trim().toLocaleLowerCase();
  const byProvider = new Map<string, FrontendChatModel[]>();

  for (const model of models) {
    if (needle && !modelSearchText(model).includes(needle)) continue;
    const provider = modelProvider(model);
    const entries = byProvider.get(provider) ?? [];
    entries.push(model);
    byProvider.set(provider, entries);
  }

  const collator = new Intl.Collator(undefined, {
    sensitivity: "base",
    numeric: true,
  });
  return [...byProvider.entries()]
    .sort(([left], [right]) => collator.compare(left, right))
    .map(([provider, entries]) => ({
      provider,
      models: [...entries].sort((left, right) => {
        const byName = collator.compare(left.name, right.name);
        return byName || collator.compare(left.id, right.id);
      }),
    }));
}
