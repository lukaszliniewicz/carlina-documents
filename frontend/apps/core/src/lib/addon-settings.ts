import type {
  FrontendAddonSettingField,
  FrontendAddonSettingOption,
} from "@carlina/sdk";

export type AddonSettingsDraft = Record<string, unknown>;

export interface AddonSettingGroup {
  name: string;
  fields: FrontendAddonSettingField[];
}

function comparable(value: unknown): string {
  if (Array.isArray(value)) return JSON.stringify(value);
  if (value === null || value === undefined) return "";
  return String(value);
}

export function initialAddonSettingValue(
  field: FrontendAddonSettingField,
): unknown {
  if (field.kind === "checkbox") return Boolean(field.value);
  if (field.kind === "mapping") {
    return Array.isArray(field.value)
      ? field.value.map((row) => ({ ...(row as Record<string, unknown>) }))
      : [];
  }
  return field.value ?? "";
}

export function buildAddonSettingsDraft(
  fields: FrontendAddonSettingField[],
): AddonSettingsDraft {
  return Object.fromEntries(
    fields.map((field) => [field.id, initialAddonSettingValue(field)]),
  );
}

export function changedAddonSettings(
  fields: FrontendAddonSettingField[],
  draft: AddonSettingsDraft,
): AddonSettingsDraft {
  return Object.fromEntries(
    fields
      .filter((field) => field.editable)
      .filter((field) => {
        const value = draft[field.id];
        if (field.sensitive) return comparable(value) !== "";
        return comparable(value) !== comparable(initialAddonSettingValue(field));
      })
      .map((field) => [field.id, draft[field.id]]),
  );
}

export function groupAddonSettings(
  fields: FrontendAddonSettingField[],
): AddonSettingGroup[] {
  const groups = new Map<string, FrontendAddonSettingField[]>();
  for (const field of fields) {
    const name = field.section || field.scope_label;
    const group = groups.get(name) ?? [];
    group.push(field);
    groups.set(name, group);
  }
  return [...groups].map(([name, groupFields]) => ({
    name,
    fields: groupFields,
  }));
}

export function addonSettingOptions(
  field: FrontendAddonSettingField,
  modelOptions: FrontendAddonSettingOption[],
): FrontendAddonSettingOption[] {
  const options = [
    ...field.options,
    ...(field.options_source === "llm_models" ? modelOptions : []),
  ];
  const seen = new Set<string>();
  const unique = options.filter((option) => {
    if (seen.has(option.value)) return false;
    seen.add(option.value);
    return true;
  });
  const current = String(field.value ?? "");
  if (current && !seen.has(current)) {
    unique.unshift({ value: current, label: `Current · ${current}` });
  }
  return unique;
}

export function formatAddonConnectionDate(value: string | null): string {
  if (!value) return "Date not recorded";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return "Date not recorded";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(
    parsed,
  );
}
