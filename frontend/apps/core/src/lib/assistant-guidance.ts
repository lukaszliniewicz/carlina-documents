import type {
  FrontendAssistantGuidanceDashboard,
  FrontendAssistantGuidancePromptId,
} from "@carlina/sdk";

export type GuidancePromptDrafts = Record<
  FrontendAssistantGuidancePromptId,
  string
>;

export function guidancePromptDrafts(
  guidance: FrontendAssistantGuidanceDashboard,
): GuidancePromptDrafts {
  const drafts: GuidancePromptDrafts = {
    chief_assistant: "",
    junior_assistant_mandate: "",
    direct_assistant_default: "",
  };
  for (const prompt of guidance.prompts) {
    drafts[prompt.id] = prompt.value;
  }
  return drafts;
}

export function guidancePromptUsesDefault(
  value: string,
  defaultValue: string,
): boolean {
  const normalized = value.trim();
  return !normalized || normalized === defaultValue.trim();
}

export function guidanceCustomizedPromptCount(
  guidance: FrontendAssistantGuidanceDashboard,
  drafts: GuidancePromptDrafts,
): number {
  return guidance.prompts.filter(
    (prompt) =>
      !guidancePromptUsesDefault(
        drafts[prompt.id] ?? "",
        prompt.default_value,
      ),
  ).length;
}

export function guidanceHasChanges(
  guidance: FrontendAssistantGuidanceDashboard,
  organizationEnabled: boolean,
  organizationProfile: string,
  drafts: GuidancePromptDrafts,
): boolean {
  if (organizationEnabled !== guidance.organization.enabled) return true;
  if (organizationProfile.trim() !== guidance.organization.profile.trim()) {
    return true;
  }
  return guidance.prompts.some(
    (prompt) =>
      (drafts[prompt.id] ?? "").trim() !== prompt.value.trim(),
  );
}
