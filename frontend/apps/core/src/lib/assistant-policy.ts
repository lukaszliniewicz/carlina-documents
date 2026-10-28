import type {
  FrontendAssistantApprovalMode,
  FrontendAssistantPolicyDashboard,
} from "@carlina/sdk";

export interface AssistantPolicyDraft {
  approvalMode: FrontendAssistantApprovalMode;
  maxToolCalls: number;
  userHotLimit: number;
  organizationHotLimit: number;
  includeArchiveByDefault: boolean;
}

export function assistantPolicyDraft(
  policy: FrontendAssistantPolicyDashboard,
): AssistantPolicyDraft {
  return {
    approvalMode: policy.runtime.approval_mode,
    maxToolCalls: policy.runtime.max_tool_calls,
    userHotLimit: policy.memory.user_hot_limit,
    organizationHotLimit: policy.memory.organization_hot_limit,
    includeArchiveByDefault:
      policy.memory.include_archive_by_default,
  };
}

export function assistantPolicyHasChanges(
  policy: FrontendAssistantPolicyDashboard,
  draft: AssistantPolicyDraft,
): boolean {
  return (
    draft.approvalMode !== policy.runtime.approval_mode ||
    draft.maxToolCalls !== policy.runtime.max_tool_calls ||
    draft.userHotLimit !== policy.memory.user_hot_limit ||
    draft.organizationHotLimit !==
      policy.memory.organization_hot_limit ||
    draft.includeArchiveByDefault !==
      policy.memory.include_archive_by_default
  );
}

function integerInRange(
  value: number,
  minimum: number,
  maximum: number,
): boolean {
  return (
    Number.isInteger(value) &&
    value >= minimum &&
    value <= maximum
  );
}

export function assistantPolicyDraftIsValid(
  policy: FrontendAssistantPolicyDashboard,
  draft: AssistantPolicyDraft,
): boolean {
  return (
    integerInRange(
      draft.maxToolCalls,
      policy.limits.max_tool_calls.minimum,
      policy.limits.max_tool_calls.maximum,
    ) &&
    integerInRange(
      draft.userHotLimit,
      policy.limits.hot_memory.minimum,
      policy.limits.hot_memory.maximum,
    ) &&
    integerInRange(
      draft.organizationHotLimit,
      policy.limits.hot_memory.minimum,
      policy.limits.hot_memory.maximum,
    )
  );
}

export function assistantPolicyNeedsFullAccessAcknowledgement(
  policy: FrontendAssistantPolicyDashboard,
  draft: AssistantPolicyDraft,
): boolean {
  return (
    policy.runtime.approval_mode !== "full_access" &&
    draft.approvalMode === "full_access"
  );
}
