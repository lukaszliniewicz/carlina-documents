import type {
  FrontendApiClient,
  FrontendAddonInventoryResponse,
  FrontendAddonManagerItem,
  FrontendAddonManagerSummary,
  FrontendAddonSettingsDashboard,
  FrontendAdministrationDashboard,
  FrontendAutomationDashboard,
  FrontendAutomationReadiness,
  FrontendAutomationTemplate,
  FrontendAutomationWorkflow,
  FrontendArtifactContent,
  FrontendArtifactDetail,
  FrontendArtifactPage,
  FrontendArtifactSummary,
  FrontendAssistantGuidanceDashboard,
  FrontendAssistantGuidanceUpdate,
  FrontendAssistantPolicyDashboard,
  FrontendAssistantPolicyUpdate,
  FrontendBootstrap,
  FrontendCapability,
  FrontendChatDirectory,
  FrontendChatHistoryPage,
  FrontendChatModel,
  FrontendContextDetail,
  FrontendContextItem,
  FrontendContextPage,
  FrontendContextUpdate,
  FrontendCredentialSharingDashboard,
  FrontendIntelligenceSettingsDashboard,
  FrontendLogChunk,
  FrontendLogSource,
  FrontendMemoryPage,
  FrontendModelRecord,
  FrontendOperationsDashboard,
  FrontendProfile,
  FrontendProviderSettingsDetail,
  FrontendRunDirectory,
  FrontendRunPage,
  FrontendRunStepDetail,
  FrontendRunStudio,
  FrontendRunSummary,
  FrontendSlashCommand,
  FrontendWorkspaceSummary,
  FrontendUsageDashboard,
} from "@carlina/sdk";

import { mountCoreApplication } from "./main.ts";

const clientRoutes = [
  "/",
  "/workspaces",
  "/automations",
  "/runs",
  "/approvals",
  "/runtime/processes",
  "/knowledge",
  "/logs",
  "/settings",
];
if (
  !clientRoutes.some(
    (path) =>
      window.location.pathname === path ||
      (path !== "/" && window.location.pathname.startsWith(`${path}/`)),
  )
) {
  window.history.replaceState({}, "", "/");
}

const navigation = [
  {
    id: "chat",
    label: "Chat",
    href: "/",
    icon: "chat",
    section: "primary",
    client_route: true,
  },
  {
    id: "workspaces",
    label: "Workspaces",
    href: "/workspaces",
    icon: "workspaces",
    section: "primary",
    client_route: true,
  },
  {
    id: "runtime",
    label: "Runtime",
    href: "/runs",
    icon: "activity",
    section: "primary",
    client_route: true,
  },
  {
    id: "knowledge",
    label: "Knowledge",
    href: "/knowledge/",
    icon: "book",
    section: "primary",
    client_route: true,
  },
  {
    id: "settings",
    label: "Profile & settings",
    href: "/settings",
    icon: "settings",
    section: "utility",
    client_route: true,
  },
  {
    id: "logs",
    label: "Logs",
    href: "/logs",
    icon: "logs",
    section: "utility",
    client_route: true,
  },
] satisfies FrontendBootstrap["navigation"];

const bootstrap: FrontendBootstrap = {
  frontend_api_version: "1.0",
  supported_frontend_api_majors: [1],
  product: {
    name: "Carlina",
    short_name: "Carlina",
    descriptor: "Local-first agent workbench",
    mark: "C",
    distribution_name: "carlina",
    cli_name: "carlina",
    environment_prefix: "CARLINA_",
    import_package: "iarf_assistant",
    repository_url:
      "https://github.com/lukaszliniewicz/iarf_assistant-Refactor",
  },
  current_path: `${window.location.pathname}${window.location.search}`,
  user: {
    id: "7",
    display_name: "Mara Editorial",
    is_admin: true,
  },
  feature_flags: {
    "frontend.shell": true,
    "frontend.chat": true,
    "frontend.context_inspector": true,
    "frontend.run_studio": true,
    "frontend.settings_profile": true,
    "frontend.logs": true,
  },
  navigation,
  capabilities: {
    same_origin_modules: true,
    history_api_routing: true,
    server_driven_navigation: true,
  },
  runtime_readiness: {
    state: "ready",
    phase: "ready",
    started_at: new Date().toISOString(),
    completed_at: new Date().toISOString(),
    elapsed_ms: 0,
    timings_ms: {},
    details: {},
    errors: [],
  },
  build: {
    core: {
      id: "phase2-fixture",
      status: "built",
      entry_url: null,
      css_urls: [],
    },
  },
  addons: [],
  ui_contributions: [
    {
      id: "meeting_minutes.workspace",
      addon_id: "meeting_minutes",
      kind: "workspace",
      title: "Meeting Minutes",
      description:
        "Shape recordings and notes into clear decisions, actions, and publishable minutes.",
      routes: ["/addons/meeting_minutes"],
      entrypoint_kinds: ["workspace_launcher"],
      component_id: null,
      section: "Editorial",
      icon: "",
      status: "active",
    },
    {
      id: "translator.workspace",
      addon_id: "translator",
      kind: "workspace",
      title: "Translation Studio",
      description:
        "Translate long-form material with structured blocks, references, and review context.",
      routes: ["/addons/translator/projects"],
      entrypoint_kinds: ["workspace_launcher"],
      component_id: null,
      section: "Editorial",
      icon: "",
      status: "active",
    },
    {
      id: "doc_processor.workspace",
      addon_id: "doc_processor",
      kind: "workspace",
      title: "Document Processor",
      description:
        "Prepare source documents, inspect structure, and create dependable exports.",
      routes: ["/addons/doc_processor"],
      entrypoint_kinds: ["workspace_launcher"],
      component_id: null,
      section: "Editorial",
      icon: "",
      status: "active",
    },
    {
      id: "subtitle_studio.workspace",
      addon_id: "subtitle_studio",
      kind: "workspace",
      title: "Subtitle Studio",
      description:
        "Coordinate transcription, correction, timing, and delivery in one focused room.",
      routes: ["/addons/subtitle_studio"],
      entrypoint_kinds: ["workspace_launcher"],
      component_id: null,
      section: "Media",
      icon: "",
      status: "degraded",
    },
    {
      id: "stt_dataset.workspace",
      addon_id: "stt_dataset",
      kind: "workspace",
      title: "Speech Dataset",
      description:
        "Review aligned segments and turn source audio into a reusable speech corpus.",
      routes: ["/addons/stt_dataset"],
      entrypoint_kinds: ["workspace_launcher"],
      component_id: null,
      section: "Media",
      icon: "",
      status: "active",
    },
    {
      id: "email.workspace",
      addon_id: "email",
      kind: "workspace",
      title: "Correspondence",
      description:
        "Review incoming mail and prepare measured, context-aware responses.",
      routes: ["/addons/email"],
      entrypoint_kinds: ["workspace_launcher"],
      component_id: null,
      section: "Operations",
      icon: "",
      status: "active",
    },
  ],
  ui_components: [],
};

const now = new Date();
const logSources: FrontendLogSource[] = [
  {
    id: "app.legacy",
    name: "Application",
    category: "core",
    description: "Requests, startup, and application-level events.",
    exists: true,
    readable: true,
    writable: true,
    size: 148_430,
    modified_at: now.toISOString(),
    warning: null,
    clearable: true,
    downloadable: true,
    max_bytes: 5_000_000,
    backup_count: 3,
    metadata: {},
    api_url: "/api/logs/app.legacy",
  },
  {
    id: "worker.runtime",
    name: "Workflow worker",
    category: "runtime",
    description: "Durable workflow execution and queue activity.",
    exists: true,
    readable: true,
    writable: true,
    size: 82_944,
    modified_at: new Date(now.valueOf() - 180_000).toISOString(),
    warning: null,
    clearable: true,
    downloadable: true,
    max_bytes: 5_000_000,
    backup_count: 3,
    metadata: {},
    api_url: "/api/logs/worker.runtime",
  },
  {
    id: "frontend.build",
    name: "Frontend builds",
    category: "build",
    description: "Core and addon frontend build output.",
    exists: true,
    readable: true,
    writable: true,
    size: 18_230,
    modified_at: new Date(now.valueOf() - 540_000).toISOString(),
    warning: null,
    clearable: true,
    downloadable: true,
    max_bytes: 2_000_000,
    backup_count: 2,
    metadata: {},
    api_url: "/api/logs/frontend.build",
  },
  {
    id: "addon.translator",
    name: "Translation Studio",
    category: "addon",
    description: "Translator jobs and document processing.",
    exists: false,
    readable: false,
    writable: true,
    size: 0,
    modified_at: null,
    warning: "Log file has not been created yet.",
    clearable: true,
    downloadable: false,
    max_bytes: 2_000_000,
    backup_count: 2,
    metadata: { addon_id: "translator" },
    api_url: "/api/logs/addon.translator",
  },
];

const sampleLogs: Record<string, string> = {
  "app.legacy": [
    "2026-07-25 21:32:04 - INFO - Application startup completed in 1.42 seconds",
    '{"timestamp":"2026-07-25T21:32:06Z","level":"info","message":"Frontend bootstrap served","route":"/workspaces","duration_ms":18}',
    "2026-07-25 21:32:12 - DEBUG - Addon contribution registry returned 18 active records",
    "2026-07-25 21:33:41 - WARNING - Optional subtitle worker has not reported readiness",
    "2026-07-25 21:34:09 - INFO - Authenticated request completed",
    '{"timestamp":"2026-07-25T21:34:11Z","level":"error","message":"Preview renderer timed out","component":"artifact-preview","retryable":true}',
    "Traceback context retained for diagnostics; request continued safely.",
  ].join("\n"),
  "worker.runtime": [
    "2026-07-25 21:30:01 - INFO - Worker heartbeat accepted",
    "2026-07-25 21:31:19 - INFO - Run 184 moved from queued to active",
    '{"timestamp":"2026-07-25T21:31:22Z","level":"debug","message":"Step input materialized","run_id":184,"step":"research"}',
    "2026-07-25 21:31:47 - INFO - Run 184 awaiting review",
  ].join("\n"),
  "frontend.build": [
    "2026-07-25 21:20:12 - INFO - Core workspace dependencies verified",
    "2026-07-25 21:20:14 - INFO - Svelte type check completed",
    "2026-07-25 21:20:16 - INFO - Core frontend emitted hashed local assets",
  ].join("\n"),
  "addon.translator": "",
};

let profile: FrontendProfile = {
  email: "mara.editorial@example.org",
  is_admin: true,
  preferences:
    "Prefer concise, evidence-led answers. State meaningful assumptions and use SI units. Avoid unnecessary ceremony.",
  style_reference:
    "Measured and warm. Use direct verbs, short paragraphs, and restrained headings. Let the structure carry the emphasis rather than bolding every important phrase.",
  security: {
    envelope_version: 2,
    active_key_id: "mk-3a4bc82e91f0d673",
    key_backend: "restricted-file",
    persistent: true,
    automation_ready: true,
    recovery_export_supported: true,
    note:
      "New protected values use the persistent installation key. Password changes and background work do not change that key.",
  },
};

const chatDirectory: FrontendChatDirectory = {
  items: [
    {
      id: "autumn-brief",
      title: "A quieter autumn direction",
      created_at: new Date(now.valueOf() - 86_400_000).toISOString(),
      updated_at: now.toISOString(),
      message_count: 7,
    },
    {
      id: "source-review",
      title: "Review source notes",
      created_at: new Date(now.valueOf() - 172_800_000).toISOString(),
      updated_at: new Date(now.valueOf() - 54_000_000).toISOString(),
      message_count: 18,
    },
    {
      id: "launch-copy",
      title: "Launch copy structure",
      created_at: new Date(now.valueOf() - 259_200_000).toISOString(),
      updated_at: new Date(now.valueOf() - 172_800_000).toISOString(),
      message_count: 11,
    },
  ],
  next_cursor: null,
};

const chatHistory: FrontendChatHistoryPage = {
  session: {
    id: "autumn-brief",
    title: "A quieter autumn direction",
    created_at: chatDirectory.items[0]!.created_at,
    updated_at: now.toISOString(),
    message_count: 7,
    newest_message_id: 7,
    revision: "7:fixture",
    assistant_scope: { mode: "default", capability_ids: [] },
    interactions: [],
    stats: {
      cost: 0.0184,
      tokens: { prompt: 4_118, completion: 1_924, total: 6_042 },
    },
  },
  items: [
    {
      id: "message:1",
      message_id: 1,
      sequence: 0,
      created_at: new Date(now.valueOf() - 420_000).toISOString(),
      role: "user",
      text: "Could we make the interface feel warmer and more editorial, without turning it into a themed novelty?",
      attachments: [],
    },
    {
      id: "event:palette",
      message_id: 2,
      sequence: 1,
      created_at: new Date(now.valueOf() - 410_000).toISOString(),
      role: "event",
      event: {
        id: "event:palette",
        kind: "tool_call.completed",
        status: "completed",
        title: "Interface references reviewed",
        summary: "12 surfaces compared",
        subject: { type: "tool_call", id: "palette-review" },
        payload: {
          references: 12,
          contrast_checks: 8,
          runtime_references: {
            run_id: "run-editorial-brief",
            artifact_ids: ["artifact-brief"],
          },
        },
      },
    },
    {
      id: "message:3",
      message_id: 3,
      sequence: 2,
      created_at: new Date(now.valueOf() - 390_000).toISOString(),
      role: "assistant",
      text: "Yes. The strongest direction is restraint: parchment rather than cream, clay as a signal rather than a fill, and serif typography only where it establishes hierarchy.",
      html: "<p>Yes. The strongest direction is <strong>restraint</strong>: parchment rather than cream, clay as a signal rather than a fill, and serif typography only where it establishes hierarchy.</p><p>The square rules should stay. They give the interface a precise, archival character without adding visual weight.</p>",
      tts_audio_url: null,
    },
    {
      id: "message:4",
      message_id: 4,
      sequence: 3,
      created_at: new Date(now.valueOf() - 120_000).toISOString(),
      role: "user",
      text: "Good. Keep the controls quiet too—revealed when useful, not all competing at once.",
      attachments: [],
    },
  ],
  next_cursor: null,
  has_more: false,
  debug: false,
};

const chatModels: FrontendChatModel[] = [
  {
    id: "editorial-pro",
    name: "Editorial Pro",
    provider: "Carlina",
    category: "powerful",
    supports_vision: true,
    supports_pdf: true,
    supports_audio_in: false,
    supports_reasoning: true,
    is_preset_powerful: true,
  },
  {
    id: "quick-draft",
    name: "Quick Draft",
    provider: "Carlina",
    category: "fast",
    supports_vision: false,
    supports_pdf: false,
    supports_audio_in: false,
    supports_reasoning: false,
  },
];

const chatCapabilities: FrontendCapability[] = [
  {
    id: "web-research",
    title: "Web research",
    group: "Research",
    description: "Find and inspect current primary sources.",
  },
  {
    id: "document-workbench",
    title: "Document workbench",
    group: "Editorial",
    description: "Read, compare, and prepare document artifacts.",
  },
];

const chatCommands: FrontendSlashCommand[] = [
  {
    command: "research",
    description: "Start a focused source review",
    insert_text: "/research ",
  },
];

const workspaceRuns: FrontendRunSummary[] = [
  {
    id: "run-editorial-brief",
    title: "Editorial interface brief",
    summary:
      "Comparing the migrated chat, navigation, and workspace surfaces against the restrained autumn direction.",
    kind: "ad_hoc",
    status: "running",
    active: true,
    needs_attention: false,
    current_step_label: "Prepare component recommendations",
    progress: { completed: 4, total: 6, percent: 67 },
    artifact_count: 2,
    pending_approval_count: 0,
    created_at: new Date(now.valueOf() - 540_000).toISOString(),
    updated_at: new Date(now.valueOf() - 42_000).toISOString(),
    completed_at: null,
    open_url: "/runs/run-editorial-brief",
  },
  {
    id: "run-reference-review",
    title: "Reference capture review",
    summary:
      "The comparison is complete and the final visual notes are ready for approval.",
    kind: "template",
    status: "waiting_for_review",
    active: true,
    needs_attention: true,
    current_step_label: "Approve visual direction",
    progress: { completed: 5, total: 5, percent: 100 },
    artifact_count: 1,
    pending_approval_count: 1,
    created_at: new Date(now.valueOf() - 86_400_000).toISOString(),
    updated_at: new Date(now.valueOf() - 8_400_000).toISOString(),
    completed_at: null,
    open_url: "/runs/run-reference-review",
  },
  {
    id: "run-token-audit",
    title: "Semantic token audit",
    summary: "Contrast and component-state checks passed.",
    kind: "template",
    status: "completed",
    active: false,
    needs_attention: false,
    current_step_label: null,
    progress: { completed: 8, total: 8, percent: 100 },
    artifact_count: 1,
    pending_approval_count: 0,
    created_at: new Date(now.valueOf() - 172_800_000).toISOString(),
    updated_at: new Date(now.valueOf() - 90_000_000).toISOString(),
    completed_at: new Date(now.valueOf() - 90_000_000).toISOString(),
    open_url: "/runs/run-token-audit",
  },
];

const workspaceArtifacts: FrontendArtifactSummary[] = [
  {
    id: "artifact-brief",
    run_id: "run-editorial-brief",
    label: "Interface direction brief.md",
    type: "document",
    mime_type: "text/markdown",
    size_bytes: 18_432,
    review_status: "pending_review",
    preview_kind: "markdown",
    version_number: 3,
    source_addon_id: "document_workbench",
    created_at: new Date(now.valueOf() - 360_000).toISOString(),
    updated_at: new Date(now.valueOf() - 55_000).toISOString(),
    download_url: "/api/runtime/artifacts/artifact-brief/download",
  },
  {
    id: "artifact-token-map",
    run_id: "run-editorial-brief",
    label: "Semantic token map.json",
    type: "structured_data",
    mime_type: "application/json",
    size_bytes: 9_216,
    review_status: "draft",
    preview_kind: "structured",
    version_number: 2,
    source_addon_id: null,
    created_at: new Date(now.valueOf() - 320_000).toISOString(),
    updated_at: new Date(now.valueOf() - 95_000).toISOString(),
    download_url: "/api/runtime/artifacts/artifact-token-map/download",
  },
  {
    id: "artifact-reference",
    run_id: "run-reference-review",
    label: "Desktop reference.png",
    type: "image",
    mime_type: "image/png",
    size_bytes: 284_300,
    review_status: "approved",
    preview_kind: "image",
    version_number: 1,
    source_addon_id: null,
    created_at: new Date(now.valueOf() - 86_000_000).toISOString(),
    updated_at: new Date(now.valueOf() - 8_500_000).toISOString(),
    download_url: "/api/runtime/artifacts/artifact-reference/download",
  },
];

const runStudioSteps: FrontendRunStudio["timeline"] = [
  {
    id: "step-scope",
    key: "scope",
    title: "Frame the interface question",
    purpose: "Turn the migration brief into explicit review criteria.",
    type: "planning",
    status: "completed",
    status_label: "Completed",
    sequence: 1,
    role: "Product editor",
    parent_id: null,
    dependency_ids: [],
    duration_ms: 48_000,
    progress: 100,
    needs_attention: false,
    artifact_count: 0,
    created_at: new Date(now.valueOf() - 540_000).toISOString(),
    started_at: new Date(now.valueOf() - 530_000).toISOString(),
    completed_at: new Date(now.valueOf() - 482_000).toISOString(),
    updated_at: new Date(now.valueOf() - 482_000).toISOString(),
  },
  {
    id: "step-audit",
    key: "audit",
    title: "Audit the current workspace",
    purpose: "Identify nested interactions, raw payloads, and visual friction.",
    type: "analysis",
    status: "completed",
    status_label: "Completed",
    sequence: 2,
    role: "Interface researcher",
    parent_id: null,
    dependency_ids: ["step-scope"],
    duration_ms: 132_000,
    progress: 100,
    needs_attention: false,
    artifact_count: 1,
    created_at: new Date(now.valueOf() - 480_000).toISOString(),
    started_at: new Date(now.valueOf() - 470_000).toISOString(),
    completed_at: new Date(now.valueOf() - 338_000).toISOString(),
    updated_at: new Date(now.valueOf() - 338_000).toISOString(),
  },
  {
    id: "step-references",
    key: "references",
    title: "Compare the approved references",
    purpose: "Separate the product's editorial character from generic dashboard patterns.",
    type: "research",
    status: "completed",
    status_label: "Completed",
    sequence: 3,
    role: "Visual researcher",
    parent_id: null,
    dependency_ids: ["step-scope"],
    duration_ms: 156_000,
    progress: 100,
    needs_attention: false,
    artifact_count: 0,
    created_at: new Date(now.valueOf() - 475_000).toISOString(),
    started_at: new Date(now.valueOf() - 460_000).toISOString(),
    completed_at: new Date(now.valueOf() - 304_000).toISOString(),
    updated_at: new Date(now.valueOf() - 304_000).toISOString(),
  },
  {
    id: "step-tokens",
    key: "tokens",
    title: "Refine the autumn token system",
    purpose: "Use color as a muted semantic hint while retaining square structure.",
    type: "design",
    status: "completed",
    status_label: "Completed",
    sequence: 4,
    role: "Design systems editor",
    parent_id: null,
    dependency_ids: ["step-audit", "step-references"],
    duration_ms: 98_000,
    progress: 100,
    needs_attention: false,
    artifact_count: 1,
    created_at: new Date(now.valueOf() - 300_000).toISOString(),
    started_at: new Date(now.valueOf() - 294_000).toISOString(),
    completed_at: new Date(now.valueOf() - 196_000).toISOString(),
    updated_at: new Date(now.valueOf() - 196_000).toISOString(),
  },
  {
    id: "step-recommendations",
    key: "recommendations",
    title: "Prepare component recommendations",
    purpose: "Describe the new sidebar, composer, inspector, and Run Studio hierarchy.",
    type: "generation",
    status: "running",
    status_label: "Running",
    sequence: 5,
    role: "Product editor",
    parent_id: null,
    dependency_ids: ["step-tokens"],
    duration_ms: 154_000,
    progress: 68,
    needs_attention: false,
    artifact_count: 1,
    created_at: new Date(now.valueOf() - 190_000).toISOString(),
    started_at: new Date(now.valueOf() - 182_000).toISOString(),
    completed_at: null,
    updated_at: new Date(now.valueOf() - 42_000).toISOString(),
  },
  {
    id: "step-review",
    key: "review",
    title: "Editorial review",
    purpose: "Verify the recommendation against accessibility and performance budgets.",
    type: "review",
    status: "planned",
    status_label: "Planned",
    sequence: 6,
    role: "Reviewer",
    parent_id: null,
    dependency_ids: ["step-recommendations"],
    duration_ms: null,
    progress: 0,
    needs_attention: false,
    artifact_count: 0,
    created_at: new Date(now.valueOf() - 180_000).toISOString(),
    started_at: null,
    completed_at: null,
    updated_at: new Date(now.valueOf() - 42_000).toISOString(),
  },
];

const runStudioFixture: FrontendRunStudio = {
  revision: "run-studio-fixture-1",
  server_time: now.toISOString(),
  run: {
    id: "run-editorial-brief",
    title: "Editorial interface brief",
    summary:
      "Comparing the migrated workspace against a restrained, readable autumn direction.",
    kind: "ad_hoc",
    workflow_id: "frontend_editorial_review",
    source_addon_id: null,
    status: "running",
    status_label: "Running",
    needs_attention: true,
    progress: { completed: 4, failed: 0, active: 1, total: 6, percent: 67 },
    current_step_id: "step-recommendations",
    current_step_label: "Prepare component recommendations",
    created_at: new Date(now.valueOf() - 540_000).toISOString(),
    started_at: new Date(now.valueOf() - 530_000).toISOString(),
    updated_at: new Date(now.valueOf() - 42_000).toISOString(),
    completed_at: null,
    duration_ms: 488_000,
    cost: {
      amount: 0.0842,
      currency: "USD",
      prompt_tokens: 18_420,
      completion_tokens: 6_280,
      total_tokens: 24_700,
      models: [{ id: "editorial-reasoner", calls: 5 }],
    },
    controls: [
      {
        id: "pause",
        label: "Pause",
        method: "POST",
        url: "/api/runtime/runs/run-editorial-brief/pause",
        tone: "neutral",
      },
      {
        id: "cancel",
        label: "Cancel",
        method: "POST",
        url: "/api/runtime/runs/run-editorial-brief/cancel",
        tone: "danger",
      },
    ],
    counts: {
      steps: 6,
      artifacts: 2,
      sources: 2,
      evidence: 2,
      approvals: 1,
      pending_approvals: 1,
      interactions: 0,
    },
  },
  documents: {
    understanding: {
      summary:
        "The product should feel editorial and composed, with color used as a quiet signal.",
      sections: [
        {
          id: "assumptions",
          title: "Assumptions",
          summary: "2 recorded items",
          kind: "list",
          value: [
            "Square structure remains part of the product identity.",
            "Primary runtime information should not require opening raw data.",
          ],
        },
      ],
    },
    plan: {
      summary: "Audit the current surfaces, align tokens, and publish component recommendations.",
      sections: [
        {
          id: "goals",
          title: "Goals",
          summary: "Readable, quick, and expandable",
          kind: "list",
          value: [
            "One-click access to primary context",
            "Equivalent graph and keyboard list",
            "Readable artifacts and provenance",
          ],
        },
        {
          id: "expected-artifacts",
          title: "Expected artifacts",
          summary: "Interface brief and token map",
          kind: "list",
          value: ["Interface direction brief", "Semantic token map"],
        },
      ],
    },
    state: {
      summary: "Four steps are complete; component recommendations are being drafted.",
      sections: [
        {
          id: "current-focus",
          title: "Current focus",
          summary: "Run Studio hierarchy",
          kind: "structured",
          value: {
            surface: "Run Studio",
            next_decision: "Approve the visual direction",
          },
        },
      ],
    },
  },
  graph: {
    nodes: runStudioSteps,
    edges: [
      { id: "scope-audit", source: "step-scope", target: "step-audit" },
      { id: "scope-references", source: "step-scope", target: "step-references" },
      { id: "audit-tokens", source: "step-audit", target: "step-tokens" },
      { id: "references-tokens", source: "step-references", target: "step-tokens" },
      { id: "tokens-recommendations", source: "step-tokens", target: "step-recommendations" },
      { id: "recommendations-review", source: "step-recommendations", target: "step-review" },
    ],
    large: false,
    truncated: false,
    layout_direction: "RIGHT",
  },
  timeline: runStudioSteps,
  artifacts: workspaceArtifacts.filter(
    (artifact) => artifact.run_id === "run-editorial-brief",
  ),
  sources: [
    {
      id: "source-prototype",
      step_id: "step-references",
      title: "Approved Phase 0 prototype",
      summary: "The reference direction for muted surfaces and square structural frames.",
      type: "design_reference",
      provider: "Repository",
      url: null,
      created_at: new Date(now.valueOf() - 410_000).toISOString(),
    },
    {
      id: "source-budgets",
      step_id: "step-audit",
      title: "Frontend performance and accessibility budgets",
      summary: "Accepted loading, responsiveness, keyboard, and contrast gates.",
      type: "requirements",
      provider: "Repository",
      url: null,
      created_at: new Date(now.valueOf() - 400_000).toISOString(),
    },
  ],
  evidence: [
    {
      id: "evidence-one-click",
      step_id: "step-audit",
      artifact_id: "artifact-brief",
      claim: "Primary context should be reachable without nested expanders.",
      quote: "No tab requires a second expander to show its primary content.",
      citation: { phase: 4, gate: "context inspector" },
      relevance_score: 0.98,
      novelty_score: 0.46,
      used_in_artifact: true,
      source: { id: "source-budgets", title: "Frontend budgets", url: null },
      created_at: new Date(now.valueOf() - 230_000).toISOString(),
    },
    {
      id: "evidence-graph-list",
      step_id: "step-recommendations",
      artifact_id: "artifact-brief",
      claim: "The graph requires an equivalent keyboard-accessible list.",
      quote: "Graph content has an equivalent ordered list or timeline.",
      citation: { phase: 5, gate: "accessibility" },
      relevance_score: 0.96,
      novelty_score: 0.52,
      used_in_artifact: true,
      source: { id: "source-budgets", title: "Frontend budgets", url: null },
      created_at: new Date(now.valueOf() - 120_000).toISOString(),
    },
  ],
  approvals: [
    {
      id: "approval-direction",
      status: "pending",
      subject_type: "workflow_step",
      subject_id: "step-review",
      tool_id: "publish_interface_brief",
      effects: ["Publish to staging", "Notify reviewers"],
      preview: {
        destination: "Frontend modernization ledger",
        artifact: "Interface direction brief",
      },
      resolution_note: "",
      created_at: new Date(now.valueOf() - 55_000).toISOString(),
      expires_at: null,
      resolved_at: null,
      can_resolve: true,
      resolve_url: "/api/runtime/approvals/approval-direction",
    },
  ],
  interactions: [],
  selected_step_id: "step-recommendations",
  developer_url: "/api/frontend/runs/run-editorial-brief/developer",
};

const runStepDetails = new Map<string, FrontendRunStepDetail>(
  runStudioSteps.map((step) => [
    step.id,
    {
      step: {
        ...step,
        instructions:
          step.id === "step-recommendations"
            ? "Prepare concise component recommendations grounded in the approved visual language and measured constraints."
            : step.purpose,
        capabilities: ["document-workbench"],
        skills: ["editorial-analysis"],
        tools: step.status === "running" ? ["artifact_writer"] : [],
        providers: [],
        can_rerun: ["completed", "failed", "cancelled", "skipped"].includes(step.status),
        rerun_url: `/api/runtime/steps/${step.id}/rerun`,
      },
      input: {
        summary: "The approved interface brief and upstream findings.",
        fields: [
          {
            key: "audience",
            label: "Audience",
            description: "The readers this recommendation must serve.",
            type: "string",
            format: null,
            required: true,
            summary: "Product and engineering team",
            value: "Product and engineering team",
          },
          {
            key: "direction",
            label: "Editorial direction",
            description: "The accepted visual constraints.",
            type: "object",
            format: null,
            required: true,
            summary: "Muted autumn color, square structure",
            value: {
              color: "Muted autumn hints",
              borders: "Square",
              density: "Editorial",
            },
          },
        ],
        resources: [
          {
            id: "resource-plan",
            label: "Frontend update plan",
            type: "document",
            provider: "Repository",
            permissions: ["read"],
          },
        ],
        parent_outputs:
          step.dependency_ids.length > 0
            ? [
                {
                  step_id: step.dependency_ids[0]!,
                  title:
                    runStudioSteps.find((item) => item.id === step.dependency_ids[0])
                      ?.title ?? "Upstream step",
                  summary: "The upstream review completed successfully.",
                  value: {
                    recommendation: "Keep the interface direct and semantically restrained.",
                  },
                },
              ]
            : [],
      },
      output: {
        summary:
          step.status === "planned"
            ? "This step has not produced an output yet."
            : "A readable recommendation is available for review.",
        fields:
          step.status === "planned"
            ? []
            : [
                {
                  key: "recommendation",
                  label: "Recommendation",
                  description: "",
                  type: "string",
                  format: null,
                  required: true,
                  summary: "Use direct tabs and semantic content.",
                  value:
                    "Use direct tabs, readable runtime sections, and reserve raw payloads for Developer.",
                },
              ],
        artifacts:
          step.id === "step-recommendations" ? [workspaceArtifacts[0]!] : [],
      },
      activity: [
        {
          id: `${step.id}-created`,
          kind: "step_created",
          title: "Step prepared",
          summary: "Inputs and dependencies were resolved.",
          status: "completed",
          progress: 100,
          occurred_at: step.created_at,
        },
        ...(step.status === "running"
          ? [
              {
                id: `${step.id}-progress`,
                kind: "progress",
                title: "Drafting recommendations",
                summary: "The component hierarchy is being assembled.",
                status: "running",
                progress: 68,
                occurred_at: step.updated_at,
              },
            ]
          : []),
      ],
      problems: {
        has_problem: false,
        summary: "",
        guidance: "",
        log_links: [],
      },
      developer_url: `/api/frontend/runs/run-editorial-brief/developer?step_id=${step.id}`,
    },
  ]),
);

const workspaceSummary: FrontendWorkspaceSummary = {
  session: {
    id: "autumn-brief",
    title: "A quieter autumn direction",
  },
  revision: "phase4-fixture-1",
  updated_at: new Date(now.valueOf() - 42_000).toISOString(),
  server_time: now.toISOString(),
  counts: {
    runs: 3,
    active_runs: 2,
    artifacts: 3,
    artifact_reviews: 2,
    session_memory: 4,
    personal_memory: 2,
    context_items: 2,
    active_context_items: 1,
    archived_context_items: 1,
    excluded_context_items: 0,
    attention: 1,
  },
  focus_run: workspaceRuns[0]!,
  recent_runs: workspaceRuns.slice(0, 3),
  recent_artifacts: workspaceArtifacts,
  attention: [
    {
      id: "artifact-review",
      kind: "review",
      title: "2 artifacts are ready for review",
      summary: "Preview the editorial brief and record a decision.",
      target_tab: "artifacts",
    },
  ],
};

const workspaceMemory: FrontendMemoryPage = {
  items: [
    {
      id: "session:audience",
      scope: "session",
      title: "Interface direction",
      summary: "Restrained, editorial, and recognizably autumnal.",
      value: {
        tone: "Restrained and editorial",
        color_usage: "Hints rather than saturated fills",
        structural_edges: "Square",
      },
      value_kind: "structured",
      category: "workspace",
      importance: "high",
      tier: null,
      updated_at: new Date(now.valueOf() - 42_000).toISOString(),
    },
    {
      id: "session:performance",
      scope: "session",
      title: "Performance intent",
      summary: "Load optional detail only when requested.",
      value:
        "Keep the writing surface interactive while runtime, memory, and artifact details load independently.",
      value_kind: "text",
      category: "workspace",
      importance: "normal",
      tier: null,
      updated_at: new Date(now.valueOf() - 95_000).toISOString(),
    },
    {
      id: "personal:7",
      scope: "personal",
      title: "Editorial preference",
      summary: "Let structure carry emphasis.",
      value:
        "Prefer concise, evidence-led answers and let the structure carry emphasis.",
      value_kind: "text",
      category: "editorial",
      importance: "normal",
      tier: "tier1",
      updated_at: new Date(now.valueOf() - 86_400_000).toISOString(),
    },
  ],
  total: 3,
  next_cursor: null,
  scope: "all",
  query: "",
};

let workspaceContextItems: FrontendContextItem[] = [
  {
    id: "ctx-research-brief",
    evidence_id: "ev-research-brief",
    title: "Autumn interface research brief",
    kind: "document",
    lifecycle_state: "active",
    materialization: "full",
    priority: 70,
    is_pinned: true,
    token_estimate: 4_850,
    char_count: 19_210,
    byte_count: 19_210,
    mime_type: "text/markdown",
    content_hash: "f".repeat(64),
    version: 1,
    created_at: new Date(now.valueOf() - 180_000).toISOString(),
    updated_at: new Date(now.valueOf() - 42_000).toISOString(),
  },
  {
    id: "ctx-tool-landscape",
    evidence_id: "ev-tool-landscape",
    title: "Tool landscape results",
    kind: "tool_result",
    lifecycle_state: "archived",
    materialization: "reference",
    priority: 50,
    is_pinned: false,
    token_estimate: 12_400,
    char_count: 49_600,
    byte_count: 49_600,
    mime_type: "application/json",
    content_hash: "a".repeat(64),
    version: 2,
    created_at: new Date(now.valueOf() - 240_000).toISOString(),
    updated_at: new Date(now.valueOf() - 95_000).toISOString(),
  },
];

const workspaceContextBodies: Record<string, string> = {
  "ctx-research-brief":
    "# Autumn interface research\n\nUse restrained editorial structure, square edges, and optional detail loaded only when requested.",
  "ctx-tool-landscape":
    '{"sources":[{"title":"Archived tool result","status":"retained"}],"note":"Exact evidence remains addressable."}',
};

const artifactDetail: FrontendArtifactDetail = {
  artifact: {
    ...workspaceArtifacts[0]!,
    metadata: {
      purpose: "Phase 4 interface recommendation",
      language: "English",
      editorial_status: "Ready for review",
    },
    review: {
      status: "pending_review",
      note: "",
      reviewed_at: null,
      reviewed_by_user_id: null,
      can_review: true,
    },
    external_url: null,
    content_url: "/api/runtime/artifacts/artifact-brief/content?max_chars=100000",
  },
  versions: [
    {
      id: "artifact-brief-v3",
      version_number: 3,
      label: "Interface direction brief.md",
      type: "document",
      mime_type: "text/markdown",
      size_bytes: 18_432,
      change_summary: "Toned down color fills and retained square borders.",
      source_count: 2,
      evidence_count: 2,
      created_at: new Date(now.valueOf() - 55_000).toISOString(),
      current: true,
    },
    {
      id: "artifact-brief-v2",
      version_number: 2,
      label: "Interface direction brief.md",
      type: "document",
      mime_type: "text/markdown",
      size_bytes: 16_220,
      change_summary: "Refined the chat and inspector hierarchy.",
      source_count: 2,
      evidence_count: 1,
      created_at: new Date(now.valueOf() - 240_000).toISOString(),
      current: false,
    },
  ],
  versions_truncated: false,
  sources: [
    {
      id: "source-prototype",
      title: "Approved Phase 0 prototype",
      summary: "Synthetic reference for the editorial shell and inspector.",
      type: "design_reference",
      provider: "Repository",
      url: null,
      created_at: new Date(now.valueOf() - 400_000).toISOString(),
    },
    {
      id: "source-baseline",
      title: "Legacy workspace baseline",
      summary: "Documents the nested expanders and raw JSON presentation.",
      type: "baseline",
      provider: "Repository",
      url: null,
      created_at: new Date(now.valueOf() - 410_000).toISOString(),
    },
  ],
  evidence: [
    {
      id: "evidence-direct-tabs",
      claim: "Primary context should be available after one tab selection.",
      quote: "No tab requires a second expander to show its primary content.",
      citation: {
        document: "docs/frontend-modernization/README.md",
        section: "UI principles",
      },
      relevance_score: 0.98,
      novelty_score: 0.42,
      used_in_artifact: true,
      source: {
        id: "source-baseline",
        title: "Legacy workspace baseline",
        url: null,
      },
      created_at: new Date(now.valueOf() - 120_000).toISOString(),
    },
  ],
};

const artifactContent: FrontendArtifactContent = {
  artifact: { id: "artifact-brief" },
  content: {
    available: true,
    content:
      "# Interface direction\n\nThe context inspector should feel like a quiet editorial desk: direct tabs, restrained color signals, and square structural rules.\n\n## Recommendation\n\nKeep content readable by default and reserve raw runtime data for developer mode.",
    rendered_html:
      "<h1>Interface direction</h1><p>The context inspector should feel like a quiet editorial desk: direct tabs, restrained color signals, and square structural rules.</p><h2>Recommendation</h2><p>Keep content readable by default and reserve raw runtime data for developer mode.</p>",
    render_mode: "markdown",
    truncated: false,
  },
};

let automationTemplates: FrontendAutomationTemplate[] = [
  {
    id: "template-editorial-brief",
    title: "Editorial research brief",
    description:
      "Collect current sources, draft a restrained briefing, and hold publication for review.",
    owner_id: 7,
    scope: "personal",
    editable: true,
    is_active: true,
    active_version: 2,
    version_count: 2,
    step_count: 3,
    definition_error: null,
    created_at: new Date(now.valueOf() - 2_592_000_000).toISOString(),
    updated_at: new Date(now.valueOf() - 86_000).toISOString(),
    definition: {
      version: 1,
      name: "Editorial research brief",
      description:
        "Collect current sources, draft a restrained briefing, and hold publication for review.",
      steps: [
        {
          key: "research",
          title: "Gather current sources",
          type: "research",
          sequence: 1,
          depends_on: [],
          assigned_model_role: "researcher",
          instructions:
            "Find current primary sources and retain a concise evidence trail.",
          capability_ids: ["web_search.research"],
          skill_ids: ["web_search.research"],
          tool_ids: ["web_search.text"],
          provider_requirements: [],
        },
        {
          key: "draft",
          title: "Draft the briefing",
          type: "write",
          sequence: 2,
          depends_on: ["research"],
          assigned_model_role: "editor",
          instructions:
            "Turn the evidence into a compact editorial briefing with citations.",
          capability_ids: [],
          skill_ids: [],
          tool_ids: [],
          provider_requirements: [],
        },
        {
          key: "review",
          title: "Review before release",
          type: "review",
          sequence: 3,
          depends_on: ["draft"],
          assigned_model_role: "reviewer",
          instructions:
            "Check claims, tone, and source coverage before approving the output.",
          capability_ids: [],
          skill_ids: [],
          tool_ids: [],
          provider_requirements: [],
        },
      ],
    },
    versions: [
      {
        version: 1,
        name: "Editorial research brief",
        description: "Initial two-step editorial workflow.",
        step_count: 2,
        created_at: new Date(now.valueOf() - 2_592_000_000).toISOString(),
        created_by_user_id: 7,
        change_summary: "Initial version",
        active: false,
      },
      {
        version: 2,
        name: "Editorial research brief",
        description: "Added a deliberate review boundary.",
        step_count: 3,
        created_at: new Date(now.valueOf() - 86_000).toISOString(),
        created_by_user_id: 7,
        change_summary: "Added editorial review",
        active: true,
      },
    ],
  },
];

const automationWorkflows: FrontendAutomationWorkflow[] = [
  {
    id: "web_to_doc.research_write_publish",
    name: "Research, write, publish",
    description:
      "A cross-addon research-to-document workflow with explicit review and publishing gates.",
    addon_id: "web_to_doc",
    status: { state: "active", reasons: [] },
    available: true,
    dry_run_supported: true,
    execution_supported: true,
    definition_error: null,
    steps: [
      {
        key: "research",
        title: "Research sources",
        type: "research",
        depends_on: [],
        assigned_model_role: "researcher",
        instructions: "Find and evaluate current sources.",
        capability_ids: ["web_search.research"],
        skill_ids: ["web_search.research"],
        tool_ids: ["web_search.text"],
        provider_requirements: [],
      },
      {
        key: "compose",
        title: "Compose document",
        type: "write",
        depends_on: ["research"],
        assigned_model_role: "writer",
        instructions: "Compile a cited document for review.",
        capability_ids: ["web_to_doc.compiler"],
        skill_ids: [],
        tool_ids: ["web_to_doc.compile_web_research"],
        provider_requirements: [],
      },
      {
        key: "review",
        title: "Approve release",
        type: "approval",
        depends_on: ["compose"],
        assigned_model_role: "reviewer",
        instructions: "Require a user decision before publishing.",
        capability_ids: [],
        skill_ids: [],
        tool_ids: [],
        provider_requirements: [],
      },
    ],
    requirements: {
      addons: ["web_search", "scraping", "web_to_doc", "doc_processor"],
      tools: [
        "web_search.text",
        "scraping.get_web_content",
        "web_to_doc.compile_web_research",
      ],
      tool_views: [],
      helpers: ["local_explorer.folder_picker"],
      providers: [],
      services: [],
      workers: [],
      auth_scopes: [],
      resource_scopes: [],
    },
    approval_gates: [
      "user.confirm_final_document",
      "user.confirm_external_publish",
    ],
    expected_jobs: [
      { id: "source_research", label: "Find and evaluate sources", type: "" },
      { id: "document_compile", label: "Compile research document", type: "" },
      { id: "review_gate", label: "User review and approval", type: "" },
    ],
    expected_artifacts: [
      { id: "source_notes", label: "Cited source notes", type: "markdown" },
      {
        id: "compiled_document",
        label: "Compiled research document",
        type: "document",
      },
    ],
    links: [
      {
        id: "launch",
        label: "Open Web Research Compiler",
        href: "/addons/web_to_doc",
        kind: "launch",
      },
      {
        id: "jobs.runtime",
        label: "Runtime jobs",
        href: null,
        kind: "log",
      },
    ],
    launch_url: "/addons/web_to_doc",
  },
  {
    id: "email.email_to_todos",
    name: "Email to to-dos",
    description:
      "Search unread mail, identify concrete tasks, and prepare a reviewable to-do list.",
    addon_id: "email",
    status: { state: "degraded", reasons: ["No executable step definition was declared."] },
    available: true,
    dry_run_supported: true,
    execution_supported: false,
    definition_error: null,
    steps: [],
    requirements: {
      addons: [],
      tools: ["email.search_emails", "email.get_thread"],
      tool_views: [],
      helpers: [],
      providers: [],
      services: [],
      workers: [],
      auth_scopes: [],
      resource_scopes: [],
    },
    approval_gates: ["user.confirm_todo_creation"],
    expected_jobs: [],
    expected_artifacts: [
      { id: "todo_list", label: "To-do list", type: "document" },
    ],
    links: [],
    launch_url: null,
  },
];

function automationDashboard(): FrontendAutomationDashboard {
  const templates = automationTemplates.map(
    ({ definition: _definition, versions: _versions, ...summary }) => summary,
  );
  return {
    templates,
    workflows: automationWorkflows,
    summary: {
      templates: templates.length,
      personal_templates: templates.filter((item) => item.scope === "personal")
        .length,
      addon_workflows: automationWorkflows.length,
      executable_workflows: automationWorkflows.filter(
        (item) => item.execution_supported,
      ).length,
      attention: automationWorkflows.filter(
        (item) =>
          item.status.state === "degraded" || Boolean(item.definition_error),
      ).length,
    },
  };
}

const automationReadiness: FrontendAutomationReadiness = {
  workflow: automationWorkflows[0]!,
  available: true,
  can_dry_run: true,
  execution_supported: true,
  message: "Workflow dry-run validation passed.",
  blocking_reasons: [],
  step_count: 3,
  requirements: [
    {
      id: "addons",
      label: "Addons",
      total: 4,
      ready: 4,
      items: automationWorkflows[0]!.requirements.addons.map((id) => ({
        id,
        registered: true,
        state: "active",
        ready: true,
        reasons: [],
      })),
    },
    {
      id: "tools",
      label: "Tools",
      total: 3,
      ready: 3,
      items: automationWorkflows[0]!.requirements.tools.map((id) => ({
        id,
        registered: true,
        state: "active",
        ready: true,
        reasons: [],
      })),
    },
  ],
  approval_gates: automationWorkflows[0]!.approval_gates,
  expected_jobs: automationWorkflows[0]!.expected_jobs,
  expected_artifacts: automationWorkflows[0]!.expected_artifacts,
  links: automationWorkflows[0]!.links,
};

let operationsDashboardFixture: FrontendOperationsDashboard = {
  frontend_api_version: "1.0",
  generated_at: now.toISOString(),
  permissions: { administer_system: true },
  available_sections: [
    "overview",
    "approvals",
    "jobs",
    "workers",
    "schedules",
    "containers",
  ],
  summary: {
    pending_approvals: 1,
    active_jobs: 2,
    failed_jobs: 1,
    attention_jobs: 1,
    jobs_total: 18,
    worker_processes: 4,
    worker_addons: 2,
    unhealthy_workers: 0,
    schedules: 2,
    containers: 0,
    attention: 2,
  },
  counts: {
    approvals: { pending: 1, approved: 7, rejected: 1 },
    jobs: { running: 1, queued: 1, completed: 15, failed: 1 },
  },
  approvals: [
    {
      id: "approval-publish",
      title: "Autumn editorial briefing",
      summary: "May affect External publish and Public content.",
      status: "pending",
      status_label: "Pending",
      subject_label: "Workflow run",
      tool_label: "WordPress publish",
      effects: ["External publish", "Public content"],
      preview: {
        destination: "Editorial review site",
        document: "Autumn editorial briefing",
        visibility: "Public after approval",
      },
      resolution_note: "",
      created_at: new Date(now.valueOf() - 310_000).toISOString(),
      expires_at: new Date(now.valueOf() + 3_600_000).toISOString(),
      resolved_at: null,
      can_approve: true,
      can_reject: true,
      can_cancel: true,
      resolve_url: "/api/runtime/approvals/approval-publish",
      run: {
        id: "run-editorial-brief",
        title: "Autumn editorial briefing",
        url: "/runs/run-editorial-brief",
      },
      log_links: [
        {
          id: "worker.runtime",
          name: "Workflow worker",
          category: "runtime",
          url: "/logs?source=worker.runtime",
        },
      ],
    },
  ],
  jobs: [
    {
      id: "job-research",
      title: "Evaluate primary sources",
      summary: "Reviewing seven current sources for the editorial briefing.",
      source_label: "Web search",
      status: "running",
      status_label: "Running",
      progress: 64,
      active: true,
      needs_attention: false,
      queued_at: new Date(now.valueOf() - 420_000).toISOString(),
      started_at: new Date(now.valueOf() - 390_000).toISOString(),
      completed_at: null,
      updated_at: new Date(now.valueOf() - 12_000).toISOString(),
      run: {
        id: "run-editorial-brief",
        title: "Autumn editorial briefing",
        url: "/runs/run-editorial-brief",
      },
      log_links: [],
    },
    {
      id: "job-sync",
      title: "Synchronize correspondence",
      summary: "Waiting behind the current mailbox synchronization.",
      source_label: "Email",
      status: "queued",
      status_label: "Queued",
      progress: 0,
      active: true,
      needs_attention: false,
      queued_at: new Date(now.valueOf() - 80_000).toISOString(),
      started_at: null,
      completed_at: null,
      updated_at: new Date(now.valueOf() - 80_000).toISOString(),
      run: null,
      log_links: [],
    },
    {
      id: "job-export",
      title: "Prepare subtitle export",
      summary: "The source media could not be opened by the export worker.",
      source_label: "Subtitle Studio",
      status: "failed",
      status_label: "Failed",
      progress: 23,
      active: false,
      needs_attention: true,
      queued_at: new Date(now.valueOf() - 7_200_000).toISOString(),
      started_at: new Date(now.valueOf() - 7_180_000).toISOString(),
      completed_at: new Date(now.valueOf() - 7_100_000).toISOString(),
      updated_at: new Date(now.valueOf() - 7_100_000).toISOString(),
      run: null,
      log_links: [],
    },
  ],
  workers: [
    {
      id: "translator",
      name: "Translation Studio",
      status: "running",
      status_label: "Running",
      configured: 3,
      running: 3,
      desired: 3,
      healthy: true,
      entrypoint: "worker.py",
      environment: "Managed process · Pixi",
      pids: [
        { value: 3184, terminate_url: "/api/addons/translator/workers/3184/terminate" },
        { value: 4180, terminate_url: "/api/addons/translator/workers/4180/terminate" },
        { value: 5192, terminate_url: "/api/addons/translator/workers/5192/terminate" },
      ],
      stop_url: "/api/addons/translator/workers/stop",
      restart_url: "/api/addons/translator/workers/restart",
      log_link: {
        id: "worker.translator",
        name: "Translation worker",
        category: "worker",
        url: "/logs?source=worker.translator",
      },
    },
    {
      id: "email",
      name: "Correspondence",
      status: "running",
      status_label: "Running",
      configured: 1,
      running: 1,
      desired: 1,
      healthy: true,
      entrypoint: "worker.py",
      environment: "Application",
      pids: [
        { value: 6220, terminate_url: "/api/addons/email/workers/6220/terminate" },
      ],
      stop_url: "/api/addons/email/workers/stop",
      restart_url: "/api/addons/email/workers/restart",
      log_link: null,
    },
  ],
  schedules: [
    {
      id: "email:email_background_sync",
      job_id: "email_background_sync",
      title: "Background correspondence sync",
      addon_id: "email",
      addon_name: "Correspondence",
      status: "scheduled",
      status_label: "Scheduled",
      trigger: "interval[0:05:00]",
      next_run_at: new Date(now.valueOf() + 180_000).toISOString(),
      paused: false,
      pending: false,
      pause_url: "/api/addons/email/jobs/email_background_sync/pause",
      resume_url: "/api/addons/email/jobs/email_background_sync/resume",
      trigger_url: "/api/addons/email/jobs/email_background_sync/trigger",
      log_link: null,
    },
    {
      id: "news_pipeline:news_fetch",
      job_id: "news_fetch",
      title: "Fetch current news",
      addon_id: "news_pipeline",
      addon_name: "News pipeline",
      status: "scheduled",
      status_label: "Scheduled",
      trigger: "cron[hour='*/2']",
      next_run_at: new Date(now.valueOf() + 3_600_000).toISOString(),
      paused: false,
      pending: false,
      pause_url: "/api/addons/news_pipeline/jobs/news_fetch/pause",
      resume_url: "/api/addons/news_pipeline/jobs/news_fetch/resume",
      trigger_url: "/api/addons/news_pipeline/jobs/news_fetch/trigger",
      log_link: null,
    },
  ],
  docker: {
    available: false,
    status: "unavailable",
    message:
      "Docker Engine is unavailable on this host. Application workers and schedules continue independently.",
    containers: [],
    log_links: [],
  },
  limits: { approvals: 80, jobs: 80 },
};

function settingsModel(
  id: number,
  modelId: string,
  name: string,
  providerId: number,
  providerName: string,
  overrides: Partial<FrontendModelRecord> = {},
): FrontendModelRecord {
  return {
    db_id: id,
    id: modelId,
    name,
    provider_id: providerId,
    provider_name: providerName,
    scope: "global",
    scope_label: "Shared",
    active: true,
    manual: false,
    type: "chat",
    context_window: 128_000,
    capabilities: {
      vision: true,
      pdf: true,
      reasoning: false,
      audio: false,
      reasoning_levels: [],
    },
    embedding: {
      default_dimensions: null,
      supports_mrl: false,
      prefix_style: "none",
      has_custom_query_prefix: false,
      has_custom_passage_prefix: false,
    },
    costs: { input: 2, cached_input: 0.5, output: 8 },
    permissions: {
      edit: true,
      delete: false,
      toggle_active: true,
      toggle_capabilities: true,
    },
    actions: {
      update_url: `/api/llm/models/${id}`,
      delete_url: `/api/llm/models/${id}`,
      toggle_active_url: `/api/llm/models/${id}/toggle-active`,
      toggle_vision_url: `/api/llm/models/${id}/toggle-vision`,
      toggle_pdf_url: `/api/llm/models/${id}/toggle-pdf`,
      toggle_reasoning_url: `/api/llm/models/${id}/toggle-reasoning`,
      toggle_audio_url: `/api/llm/models/${id}/toggle-audio`,
    },
    updated_at: new Date(now.valueOf() - 7_200_000).toISOString(),
    ...overrides,
  };
}

const openAiProvider = {
  id: 1,
  key: "openai",
  name: "OpenAI",
  scope: "global",
  scope_label: "Shared provider",
  kind: "native",
  kind_label: "Native provider",
  discovery: "automatic",
  discovery_label: "Automatic",
  status: "ready",
  status_label: "Ready",
  connection_count: 1,
  active_connection_count: 1,
  model_count: 2,
  active_model_count: 2,
  requires_endpoint: false,
  supports_custom_models: false,
  supports_discovery: true,
  permissions: {
    add_connection: true,
    add_model: false,
    delete_provider: false,
    sync_models: true,
  },
  actions: {
    detail_url: "/api/frontend/settings/intelligence/providers/1",
    connections_url: "/api/llm/providers/1/keys",
    models_url: "/api/llm/managed-models?provider_id=1",
    delete_url: "/api/llm/providers/1",
    sync_url: "/api/llm/sync-models",
  },
} as const;

const editorialProvider = {
  id: 2,
  key: "custom_u7_editorial",
  name: "Editorial vLLM",
  scope: "personal",
  scope_label: "Personal endpoint",
  kind: "openai_compatible",
  kind_label: "OpenAI-compatible endpoint",
  discovery: "manual",
  discovery_label: "Manual",
  status: "ready",
  status_label: "Ready",
  connection_count: 1,
  active_connection_count: 1,
  model_count: 1,
  active_model_count: 1,
  requires_endpoint: true,
  supports_custom_models: true,
  supports_discovery: true,
  permissions: {
    add_connection: true,
    add_model: true,
    delete_provider: true,
    sync_models: true,
  },
  actions: {
    detail_url: "/api/frontend/settings/intelligence/providers/2",
    connections_url: "/api/llm/providers/2/keys",
    models_url: "/api/llm/managed-models?provider_id=2",
    delete_url: "/api/llm/providers/2",
    sync_url: "/api/llm/sync-models",
  },
} as const;

const openAiModel = settingsModel(
  10,
  "openai/gpt-4.1",
  "GPT-4.1",
  1,
  "OpenAI",
);
const embeddingModel = settingsModel(
  11,
  "openai/text-embedding-3-large",
  "Text Embedding 3 Large",
  1,
  "OpenAI",
  {
    type: "embedding",
    context_window: 8_191,
    capabilities: {
      vision: false,
      pdf: false,
      reasoning: false,
      audio: false,
      reasoning_levels: [],
    },
    embedding: {
      default_dimensions: 3_072,
      supports_mrl: true,
      prefix_style: "none",
      has_custom_query_prefix: false,
      has_custom_passage_prefix: false,
    },
    costs: { input: 0.13, cached_input: null, output: null },
  },
);
const editorialModel = settingsModel(
  20,
  "custom_u7_editorial/quiet-editor-32b",
  "Quiet Editor 32B",
  2,
  "Editorial vLLM",
  {
    scope: "personal",
    scope_label: "Personal",
    manual: true,
    context_window: 64_000,
    capabilities: {
      vision: false,
      pdf: false,
      reasoning: true,
      audio: false,
      reasoning_levels: ["low", "medium", "high"],
    },
    costs: { input: null, cached_input: null, output: null },
    permissions: {
      edit: true,
      delete: true,
      toggle_active: true,
      toggle_capabilities: true,
    },
  },
);

let intelligenceDashboardFixture: FrontendIntelligenceSettingsDashboard = {
  frontend_api_version: "1.0",
  generated_at: now.toISOString(),
  permissions: { manage_global: true, share_credentials: true },
  summary: {
    providers: 2,
    connected_providers: 2,
    connections: 2,
    active_models: 3,
    routing_models: 2,
    delegations: 1,
  },
  providers: [openAiProvider, editorialProvider],
  routing: {
    defaults: {
      fast: editorialModel.id,
      medium: openAiModel.id,
      powerful: openAiModel.id,
    },
    models: [openAiModel, editorialModel],
    can_manage_defaults: true,
  },
  image_detail: {
    configs: { [openAiModel.id]: "high" },
    allowed_values: [
      "auto",
      "high",
      "low",
      "medium",
      "omit",
      "ultra_high",
    ],
  },
  links: {
    sharing: "/api/frontend/settings/sharing",
    preferences: "/api/frontend/settings/intelligence/preferences",
    create_connection: "/api/llm/keys",
    create_endpoint: "/api/llm/openai-compatible-providers",
    create_model: "/api/llm/models",
  },
  limits: {
    connections_per_provider: 100,
    models_per_provider: 200,
    routing_models: 400,
  },
};

const providerDetailFixtures = new Map<number, FrontendProviderSettingsDetail>([
  [
    1,
    {
      frontend_api_version: "1.0",
      provider: openAiProvider,
      connections: [
        {
          id: 1,
          name: "Shared OpenAI",
          scope: "global",
          scope_label: "Shared",
          active: true,
          endpoint_label: null,
          endpoint_configured: false,
          api_version: null,
          created_at: new Date(now.valueOf() - 86_400_000).toISOString(),
          permissions: { update: true, delete: true, activate: true },
          actions: {
            update_url: "/api/llm/keys/1",
            delete_url: "/api/llm/keys/1",
            activate_url: "/api/llm/keys/1/set-active",
          },
        },
      ],
      models: [openAiModel, embeddingModel],
      model_total: 2,
      limits: { connections: 100, models: 200 },
    },
  ],
  [
    2,
    {
      frontend_api_version: "1.0",
      provider: editorialProvider,
      connections: [
        {
          id: 2,
          name: "Local editorial endpoint",
          scope: "personal",
          scope_label: "Personal",
          active: true,
          endpoint_label: "http://127.0.0.1:8000/v1",
          endpoint_configured: true,
          api_version: null,
          created_at: new Date(now.valueOf() - 43_200_000).toISOString(),
          permissions: { update: true, delete: true, activate: true },
          actions: {
            update_url: "/api/llm/keys/2",
            delete_url: "/api/llm/keys/2",
            activate_url: "/api/llm/keys/2/set-active",
          },
        },
      ],
      models: [editorialModel],
      model_total: 1,
      limits: { connections: 100, models: 200 },
    },
  ],
]);

let sharingDashboardFixture: FrontendCredentialSharingDashboard = {
  frontend_api_version: "1.0",
  generated_at: now.toISOString(),
  summary: {
    providers: 2,
    configured_providers: 2,
    credentials: 2,
    delegations: 1,
    recipients: 3,
  },
  providers: [
    {
      id: "wordpress",
      name: "WordPress",
      configured: true,
      credentials: [
        {
          id: 31,
          provider_id: "wordpress",
          label: "Editorial site",
          active: true,
          source: "owned",
          source_label: "Owned credential",
          created_at: new Date(now.valueOf() - 172_800_000).toISOString(),
        },
      ],
    },
    {
      id: "googledrive",
      name: "Google Drive",
      configured: true,
      credentials: [
        {
          id: null,
          provider_id: "googledrive",
          label: "Google Drive account",
          active: true,
          source: "settings",
          source_label: "Configured account",
          created_at: null,
        },
      ],
    },
  ],
  delegations: [
    {
      provider_id: "wordpress",
      provider_name: "WordPress",
      credential_id: 31,
      credential_name: "Editorial site",
      delegatee_id: 12,
      delegatee_email: "editor@example.org",
    },
  ],
  recipients: [
    { id: 12, email: "editor@example.org" },
    { id: 13, email: "research@example.org" },
    { id: 14, email: "producer@example.org" },
  ],
  actions: {
    create_url: "/api/credential-delegations",
    remove_url: "/api/credential-delegations",
  },
  limits: { providers: 100, recipients: 200, delegations: 200 },
};

let guidanceDashboardFixture: FrontendAssistantGuidanceDashboard = {
  frontend_api_version: "1.0",
  generated_at: now.toISOString(),
  summary: {
    organization_enabled: true,
    customized_prompts: 1,
    total_prompts: 3,
  },
  organization: {
    enabled: true,
    profile: [
      "We are an independent editorial and research organization.",
      "Write with precision, warmth, and respect for source material.",
      "Prefer durable evidence over speed and make uncertainty visible.",
    ].join(" "),
    truncated: false,
  },
  prompts: [
    {
      id: "chief_assistant",
      name: "Chief assistant",
      description:
        "Sets the coordinating voice, planning posture, and delegation standards for complex work.",
      value:
        "Coordinate complex work as a calm editorial lead. Establish the intended outcome, delegate bounded research when useful, and return one coherent result.",
      default_value:
        "Coordinate complex work carefully, using delegation when it materially improves the result.",
      customized: true,
      truncated: false,
    },
    {
      id: "junior_assistant_mandate",
      name: "Junior assistant mandate",
      description:
        "Defines the boundaries and reporting discipline for delegated assistant work.",
      value:
        "Complete the assigned bounded task, preserve evidence, and report concise findings to the coordinating assistant.",
      default_value:
        "Complete the assigned bounded task, preserve evidence, and report concise findings to the coordinating assistant.",
      customized: false,
      truncated: false,
    },
    {
      id: "direct_assistant_default",
      name: "Direct assistant",
      description:
        "Provides the default system guidance for direct conversations that do not use a specialist mandate.",
      value:
        "Help the user directly with clear reasoning, practical action, and an honest account of uncertainty.",
      default_value:
        "Help the user directly with clear reasoning, practical action, and an honest account of uncertainty.",
      customized: false,
      truncated: false,
    },
  ],
  actions: {
    update_url: "/api/frontend/settings/guidance",
  },
  limits: {
    organization_profile_chars: 40_000,
    prompt_chars: 100_000,
  },
};

let assistantPolicyFixture: FrontendAssistantPolicyDashboard = {
  frontend_api_version: "1.0",
  generated_at: now.toISOString(),
  summary: {
    approval_mode: "review",
    tool_budget: 30,
    user_hot_limit: 80,
    archive_recall_default: false,
    configuration_needs_review: false,
  },
  runtime: {
    approval_mode: "review",
    max_tool_calls: 30,
    effective_max_tool_calls: 30,
    tool_budget_supported: true,
  },
  memory: {
    user_hot_limit: 80,
    organization_hot_limit: 120,
    include_archive_by_default: false,
    fallback_categories: [
      { key: "likes_dislikes", label: "Likes & Dislikes" },
      { key: "health_accessibility", label: "Health & Accessibility" },
      { key: "professional", label: "Professional" },
      { key: "family", label: "Family" },
      { key: "pastimes", label: "Pastimes" },
      { key: "communication_style", label: "Communication Style" },
      { key: "projects", label: "Projects" },
      { key: "constraints", label: "Constraints" },
      { key: "identity", label: "Identity" },
      { key: "organization_policy", label: "Organization Policy" },
      { key: "other", label: "Other" },
    ],
  },
  defaults: {
    runtime: {
      approval_mode: "review",
      max_tool_calls: 30,
    },
    memory: {
      user_hot_limit: 80,
      organization_hot_limit: 120,
      include_archive_by_default: false,
    },
  },
  limits: {
    max_tool_calls: { minimum: 1, maximum: 50 },
    hot_memory: { minimum: 10, maximum: 500 },
  },
  actions: {
    update_url: "/api/frontend/settings/policy",
  },
};

let addonDirectoryFixture: FrontendAddonManagerItem[] = [
  {
    id: "document_workbench",
    name: "Document Workbench",
    version: "2.4.0",
    author: "Carlina Project",
    description:
      "Prepare source documents, inspect structure, and create dependable exports.",
    roles: ["automation", "provider"],
    categories: ["Editorial"],
    provider_domains: ["documents"],
    addon_type: ["automation"],
    dependencies: ["local_explorer"],
    is_active: true,
    status: { state: "active", reasons: [] },
    frontend_tier: "compiled_workspace",
    frontend_build: {
      declared: true,
      status: "built",
      rebuild_needed: false,
      source_available: true,
      reasons: [],
      warnings: [],
      output_paths: ["static/dist/workspace.js"],
      missing_outputs: [],
      stamp_current: true,
    },
    worker_state: {
      status: "running",
      configured: 1,
      running: 1,
      pids: [4318],
      workers: [],
      log_source_id: "worker.document_workbench",
      desired: 1,
      entrypoint: "worker.py",
      execution_environment: "Application",
    },
    scheduled_job_state: {
      status: "idle",
      declared: 0,
      registered: 0,
      paused: 0,
      jobs: [],
      declared_jobs: [],
      log_source_id: null,
    },
    has_settings: true,
    has_prompts: false,
    delegatable: false,
    log_links: [],
    inferred_hub: {
      relation: "standalone",
      hub_id: null,
      hub_label: null,
      spoke_count: 0,
    },
  },
];

function addonInventorySummary(
  addon: FrontendAddonManagerItem,
): FrontendAddonManagerSummary {
  return {
    detail_level: "summary",
    detail_url: `/api/addons/${encodeURIComponent(addon.id)}/detail`,
    id: addon.id,
    name: addon.name,
    version: addon.version,
    installed_version: addon.installed_version,
    builtin_reconciliation_required: addon.builtin_reconciliation_required,
    author: addon.author,
    description: addon.description,
    roles: [...addon.roles],
    categories: [...addon.categories],
    kind: addon.kind,
    distribution: addon.distribution,
    effective_code_access: addon.effective_code_access,
    connection_count: addon.connection_count ?? 0,
    consumer_binding_count: addon.consumer_binding_count ?? 0,
    provider_binding_count: addon.provider_binding_count ?? 0,
    provider_domains: [...addon.provider_domains],
    addon_type: [...addon.addon_type],
    dependencies: [...addon.dependencies],
    search_terms: [
      addon.id,
      addon.name,
      addon.description,
      ...addon.roles,
      ...addon.categories,
      ...addon.provider_domains,
      ...addon.dependencies,
    ],
    is_active: addon.is_active,
    status: addon.status,
    frontend_tier: addon.frontend_tier,
    frontend_status: addon.frontend_build.status ?? null,
    frontend_rebuild_needed: addon.frontend_build.rebuild_needed === true,
    worker_running: addon.worker_state.running,
    has_settings: addon.has_settings,
    has_prompts: addon.has_prompts,
    delegatable: addon.delegatable,
    source_kind: addon.source_kind,
    trust_tier: addon.trust_tier,
    lifecycle_state: addon.lifecycle_state,
    lifecycle_generation: addon.lifecycle_generation,
    lifecycle_error: addon.lifecycle_error,
    code_available: addon.code_available,
    can_remove_code: addon.can_remove_code,
    can_purge_data: addon.can_purge_data,
    inferred_hub: addon.inferred_hub,
  };
}

let addonSettingsFixture: FrontendAddonSettingsDashboard = {
  frontend_api_version: "1.0",
  addon: {
    id: "document_workbench",
    name: "Document Workbench",
    description: "Focused document preparation preferences.",
  },
  permissions: { manage_global: true },
  fields: [
    {
      id: "default_model",
      name: "Default model",
      description: "The model used for document interpretation.",
      kind: "model_select",
      scope: "user",
      scope_label: "Your account",
      section: "Intelligence",
      placeholder: "",
      required: false,
      sensitive: false,
      configured: true,
      editable: true,
      value: "editorial-pro",
      options: [],
      options_source: "llm_models",
      minimum: null,
      maximum: null,
      step: null,
      rows: 5,
      requires_restart: false,
      mapping: null,
    },
    {
      id: "service_token",
      name: "Service token",
      description: "Optional write-only token for a remote conversion service.",
      kind: "password",
      scope: "user",
      scope_label: "Your account",
      section: "Connections",
      placeholder: "",
      required: false,
      sensitive: true,
      configured: true,
      editable: true,
      value: null,
      options: [],
      options_source: null,
      minimum: null,
      maximum: null,
      step: null,
      rows: 5,
      requires_restart: false,
      mapping: null,
    },
    {
      id: "maximum_pages",
      name: "Maximum pages",
      description: "A shared guardrail for one document-processing request.",
      kind: "number",
      scope: "global",
      scope_label: "Shared installation",
      section: "Runtime",
      placeholder: "",
      required: true,
      sensitive: false,
      configured: true,
      editable: true,
      value: 250,
      options: [],
      options_source: null,
      minimum: 1,
      maximum: 2_000,
      step: 1,
      rows: 5,
      requires_restart: true,
      mapping: null,
    },
  ],
  connections: null,
  actions: {
    update_url:
      "/api/frontend/settings/addons/document_workbench",
  },
};

let administrationDashboardFixture: FrontendAdministrationDashboard = {
  frontend_api_version: "1.0",
  generated_at: now.toISOString(),
  summary: {
    members: 3,
    administrators: 1,
    backups: 2,
    rag_documents: 184,
    rag_needs_review: false,
  },
  general: {
    application_url: "https://assistant.example.test",
    application_url_configured: true,
    site_policy: {
      markdown:
        "# Working agreement\n\nThis private installation retains only the information its members choose to keep.",
      published: true,
      preview_url: "/terms",
      characters: 102,
      truncated: false,
    },
    session_security: {
      source: "environment",
      label: "Persistent environment secret",
      persistent: true,
      recommended: true,
      browser_rotation_available: false,
      note: "The launcher loads a persistent secret without exposing its value.",
    },
    limits: { policy_characters: 100_000 },
  },
  retrieval: {
    configuration: {
      model_id: "provider/embedding",
      dimensions: 768,
      task_type: "search",
      top_k: 6,
      chunk_size: 5_000,
      chunk_overlap: 500,
    },
    models: [
      {
        id: "provider/embedding",
        name: "Editorial Embedding",
        provider: "provider",
        provider_label: "Local provider",
        supports_mrl: true,
        default_dimensions: 1_536,
        prefix_style: "none",
      },
    ],
    index: { documents: 184, chunks: 1_462 },
    validation: {
      model_available: true,
      dimensions: true,
      task_type: true,
      top_k: true,
      chunk_size: true,
      chunk_overlap: true,
    },
    configuration_needs_review: false,
    task_types: [
      {
        id: "search",
        label: "Search",
        description: "Balanced retrieval for general working knowledge.",
      },
      {
        id: "qa",
        label: "Question answering",
        description: "Optimize passages for direct evidence-backed answers.",
      },
    ],
    limits: {
      dimensions: { minimum: 128, maximum: 3_072 },
      top_k: { minimum: 1, maximum: 20 },
      chunk_size: { minimum: 500, maximum: 15_000 },
      chunk_overlap: { minimum: 0, maximum: 2_000 },
    },
    actions: {
      update_url:
        "/api/frontend/settings/administration/retrieval",
      preflight_url:
        "/api/frontend/settings/administration/retrieval/preflight",
      rebuild_url:
        "/api/frontend/settings/administration/retrieval/rebuild",
    },
  },
  continuity: {
    database: { available: true, size_bytes: 48_320_512 },
    backups: [
      {
        filename: "iarf-ai-20260725-204500.db",
        size_bytes: 47_982_592,
        created_at: new Date(now.valueOf() - 86_400_000).toISOString(),
      },
      {
        filename: "iarf-ai-20260719-083000.db",
        size_bytes: 46_612_480,
        created_at: new Date(now.valueOf() - 604_800_000).toISOString(),
      },
    ],
    summary: {
      backup_count: 2,
      latest_backup_at: new Date(
        now.valueOf() - 86_400_000,
      ).toISOString(),
    },
    migration_policy: {
      plaintext_export_available: false,
      browser_reencryption_available: false,
      note:
        "Plaintext bulk export remains unavailable from the browser.",
    },
    actions: {
      create_backup_url:
        "/api/frontend/settings/administration/backups",
      restore_backup_url:
        "/api/frontend/settings/administration/restore",
    },
  },
  members: {
    summary: {
      total: 3,
      administrators: 1,
      members: 2,
      truncated: false,
    },
    members: [
      {
        id: 7,
        email: "mara.editorial@example.org",
        is_admin: true,
        is_current: true,
        can_delete: false,
        can_change_role: false,
      },
      {
        id: 12,
        email: "reviewer@example.org",
        is_admin: false,
        is_current: false,
        can_delete: true,
        can_change_role: true,
      },
      {
        id: 18,
        email: "research@example.org",
        is_admin: false,
        is_current: false,
        can_delete: true,
        can_change_role: true,
      },
    ],
    limits: {
      members: 500,
      email_characters: 100,
      password_characters: { minimum: 12, maximum: 128 },
    },
    actions: {
      collection_url:
        "/api/frontend/settings/administration/members",
    },
  },
  actions: {
    general_update_url:
      "/api/frontend/settings/administration/general",
    usage_url:
      "/api/frontend/settings/administration/usage",
  },
};

const usageDashboardFixture: FrontendUsageDashboard = {
  frontend_api_version: "1.0",
  generated_at: now.toISOString(),
  range: {
    start_date: "2026-07-12",
    end_date: "2026-07-25",
    days: 14,
  },
  summary: {
    cost: 8.42,
    tokens: 624_300,
    requests: 186,
    active_members: 3,
  },
  daily: [
    {
      date: "2026-07-24",
      source: "chat",
      source_label: "Chat",
      cost: 0.84,
      tokens: 68_200,
      requests: 18,
    },
    {
      date: "2026-07-25",
      source: "runs",
      source_label: "Runs",
      cost: 1.26,
      tokens: 91_400,
      requests: 24,
    },
  ],
  by_user: [
    {
      id: 7,
      email: "mara.editorial@example.org",
      cost: 4.72,
      tokens: 342_000,
      requests: 108,
    },
    {
      id: 12,
      email: "reviewer@example.org",
      cost: 2.18,
      tokens: 171_300,
      requests: 52,
    },
  ],
  limits: {
    maximum_days: 90,
    maximum_daily_rows: 1_000,
    maximum_members: 500,
  },
};

const nativeFetch = window.fetch.bind(window);
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = new URL(
    input instanceof Request ? input.url : String(input),
    window.location.origin,
  );
  if (
    url.pathname ===
    "/api/frontend/settings/administration/retrieval/rebuild"
  ) {
    const stream = [
      { type: "progress", progress: 12, message: "Reading indexed sources…" },
      { type: "progress", progress: 68, message: "Rebuilding passages…" },
      { type: "complete", progress: 100, message: "Retrieval index rebuilt." },
    ]
      .map((event) => `data: ${JSON.stringify(event)}\n\n`)
      .join("");
    return new Response(stream, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });
  }
  return nativeFetch(input, init);
};

const api: FrontendApiClient = {
  async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    await new Promise((resolve) => window.setTimeout(resolve, 140));
    const url = new URL(path, window.location.origin);
    if (url.pathname === "/api/addons/inventory") {
      const items = addonDirectoryFixture.map(addonInventorySummary);
      return {
        frontend_api_version: "1.0",
        items,
        count: items.length,
        cache: null,
      } satisfies FrontendAddonInventoryResponse as T;
    }
    if (url.pathname === "/api/addons/all") {
      return addonDirectoryFixture as T;
    }
    const addonDetailMatch = url.pathname.match(
      /^\/api\/addons\/([^/]+)\/detail$/,
    );
    if (addonDetailMatch) {
      const addonId = decodeURIComponent(addonDetailMatch[1] ?? "");
      const addon = addonDirectoryFixture.find((item) => item.id === addonId);
      if (!addon) throw new Error("The selected addon was not found.");
      return { ...addon, detail_level: "full" } satisfies FrontendAddonManagerItem as T;
    }
    if (url.pathname === "/api/addon-connections") {
      return { frontend_api_version: "1.0", items: [] } as T;
    }
    if (url.pathname === "/api/addons/operations") {
      return { items: [], count: 0 } as T;
    }
    const capabilityResolutionMatch = url.pathname.match(
      /^\/api\/addons\/([^/]+)\/capability-resolution$/,
    );
    if (capabilityResolutionMatch) {
      return {
        consumer_addon_id: decodeURIComponent(
          capabilityResolutionMatch[1] ?? "",
        ),
        activatable: true,
        inactive_provider_count: 0,
        resolutions: [],
        setup_requests: [],
      } as T;
    }
    if (url.pathname === "/api/addons/doctor") {
      return {
        frontend_api_version: "1.0",
        summary: { errors: 0, warnings: 0, ok: 1, info: 0 },
        checks: [
          {
            id: "dev-fixture",
            title: "Development fixture",
            status: "ok",
            summary: "The visual fixture is available.",
          },
        ],
        runtime: {
          container_kind: "development_fixture",
          container_available: true,
          container_detail: "Container state is simulated by the visual fixture.",
        },
      } as T;
    }
    if (url.pathname === "/api/addons/catalog") {
      return {
        frontend_api_version: "1.0",
        configured: false,
        channel: "stable",
        packages: [],
      } as T;
    }
    if (
      url.pathname ===
      "/api/frontend/settings/addons/document_workbench"
    ) {
      if (init.method === "PUT") {
        const payload = JSON.parse(String(init.body ?? "{}")) as {
          values?: Record<string, unknown>;
        };
        const values = payload.values ?? {};
        addonSettingsFixture = {
          ...addonSettingsFixture,
          fields: addonSettingsFixture.fields.map((field) => {
            if (!(field.id in values)) return field;
            if (field.sensitive) {
              return {
                ...field,
                configured: Boolean(values[field.id]),
                value: null,
              };
            }
            return {
              ...field,
              configured: true,
              value: values[field.id],
            };
          }),
        };
        return {
          success: true,
          message: "Addon settings saved.",
          restart_required: Object.keys(values).includes("maximum_pages"),
          settings: addonSettingsFixture,
        } as T;
      }
      return addonSettingsFixture as T;
    }
    if (
      url.pathname === "/api/frontend/settings/administration" &&
      (!init.method || init.method === "GET")
    ) {
      administrationDashboardFixture = {
        ...administrationDashboardFixture,
        generated_at: new Date().toISOString(),
      };
      return administrationDashboardFixture as T;
    }
    if (
      url.pathname ===
        "/api/frontend/settings/administration/general" &&
      init.method === "PUT"
    ) {
      const payload = JSON.parse(String(init.body ?? "{}")) as {
        application_url?: string;
        site_policy_markdown?: string;
      };
      const markdown =
        payload.site_policy_markdown ??
        administrationDashboardFixture.general.site_policy.markdown;
      administrationDashboardFixture = {
        ...administrationDashboardFixture,
        general: {
          ...administrationDashboardFixture.general,
          application_url:
            payload.application_url ??
            administrationDashboardFixture.general.application_url,
          application_url_configured: Boolean(
            payload.application_url ??
              administrationDashboardFixture.general.application_url,
          ),
          site_policy: {
            ...administrationDashboardFixture.general.site_policy,
            markdown,
            published: Boolean(markdown.trim()),
            characters: markdown.length,
          },
        },
      };
      return {
        success: true,
        message: "Application identity and policy updated.",
        general: administrationDashboardFixture.general,
      } as T;
    }
    if (
      url.pathname ===
        "/api/frontend/settings/administration/retrieval/preflight" &&
      init.method === "POST"
    ) {
      const payload = JSON.parse(String(init.body ?? "{}")) as {
        model_id?: string;
        dimensions?: number;
      };
      return {
        success: true,
        message: "Embedding preflight passed.",
        model_id:
          payload.model_id ??
          administrationDashboardFixture.retrieval.configuration.model_id,
        document_dimensions: payload.dimensions ?? 768,
        query_dimensions: payload.dimensions ?? 768,
        preflight_token: "dev-preflight",
        expires_in_seconds: 600,
      } as T;
    }
    if (
      url.pathname ===
        "/api/frontend/settings/administration/retrieval" &&
      init.method === "PUT"
    ) {
      const payload = JSON.parse(
        String(init.body ?? "{}"),
      ) as Partial<
        FrontendAdministrationDashboard["retrieval"]["configuration"]
      >;
      administrationDashboardFixture = {
        ...administrationDashboardFixture,
        retrieval: {
          ...administrationDashboardFixture.retrieval,
          configuration: {
            ...administrationDashboardFixture.retrieval.configuration,
            ...payload,
          },
          configuration_needs_review: false,
        },
      };
      return {
        success: true,
        message: "Retrieval settings saved.",
        retrieval: administrationDashboardFixture.retrieval,
        requires_rebuild: false,
      } as T;
    }
    if (
      url.pathname ===
      "/api/frontend/settings/administration/usage"
    ) {
      return {
        ...usageDashboardFixture,
        generated_at: new Date().toISOString(),
      } as T;
    }
    if (
      url.pathname ===
        "/api/frontend/settings/administration/backups" &&
      init.method === "POST"
    ) {
      const backup = {
        filename: `iarf-ai-${Date.now()}.db`,
        size_bytes:
          administrationDashboardFixture.continuity.database.size_bytes,
        created_at: new Date().toISOString(),
      };
      administrationDashboardFixture = {
        ...administrationDashboardFixture,
        continuity: {
          ...administrationDashboardFixture.continuity,
          backups: [
            backup,
            ...administrationDashboardFixture.continuity.backups,
          ],
          summary: {
            backup_count:
              administrationDashboardFixture.continuity.summary.backup_count +
              1,
            latest_backup_at: backup.created_at,
          },
        },
      };
      return {
        success: true,
        message: "Integrity-checked backup created.",
        backup,
        continuity: administrationDashboardFixture.continuity,
      } as T;
    }
    if (
      url.pathname ===
        "/api/frontend/settings/administration/restore" &&
      init.method === "POST"
    ) {
      return {
        success: true,
        message: "Fixture backup restored.",
        continuity: administrationDashboardFixture.continuity,
        requires_restart: true,
      } as T;
    }
    if (
      url.pathname ===
        "/api/frontend/settings/administration/members" &&
      init.method === "POST"
    ) {
      const payload = JSON.parse(String(init.body ?? "{}")) as {
        email?: string;
        is_admin?: boolean;
      };
      const member = {
        id: Math.max(
          ...administrationDashboardFixture.members.members.map(
            (item) => item.id,
          ),
        ) + 1,
        email: payload.email ?? "new-member@example.org",
        is_admin: Boolean(payload.is_admin),
        is_current: false,
        can_delete: true,
        can_change_role: true,
      };
      const members = [
        ...administrationDashboardFixture.members.members,
        member,
      ];
      administrationDashboardFixture = {
        ...administrationDashboardFixture,
        members: {
          ...administrationDashboardFixture.members,
          members,
          summary: {
            ...administrationDashboardFixture.members.summary,
            total: members.length,
            administrators: members.filter((item) => item.is_admin).length,
            members: members.filter((item) => !item.is_admin).length,
          },
        },
      };
      return {
        success: true,
        message: "Member created.",
        directory: administrationDashboardFixture.members,
      } as T;
    }
    if (
      url.pathname.startsWith(
        "/api/frontend/settings/administration/members/",
      ) &&
      (init.method === "PUT" || init.method === "DELETE")
    ) {
      const userId = Number(url.pathname.split("/").at(-1));
      if (init.method === "DELETE") {
        administrationDashboardFixture = {
          ...administrationDashboardFixture,
          members: {
            ...administrationDashboardFixture.members,
            members:
              administrationDashboardFixture.members.members.filter(
                (member) => member.id !== userId,
              ),
          },
        };
      } else {
        const payload = JSON.parse(String(init.body ?? "{}")) as {
          email?: string;
          is_admin?: boolean;
        };
        administrationDashboardFixture = {
          ...administrationDashboardFixture,
          members: {
            ...administrationDashboardFixture.members,
            members:
              administrationDashboardFixture.members.members.map(
                (member) =>
                  member.id === userId
                    ? {
                        ...member,
                        email: payload.email ?? member.email,
                        is_admin:
                          payload.is_admin ?? member.is_admin,
                      }
                    : member,
              ),
          },
        };
      }
      const members = administrationDashboardFixture.members.members;
      administrationDashboardFixture = {
        ...administrationDashboardFixture,
        members: {
          ...administrationDashboardFixture.members,
          summary: {
            ...administrationDashboardFixture.members.summary,
            total: members.length,
            administrators: members.filter((item) => item.is_admin).length,
            members: members.filter((item) => !item.is_admin).length,
          },
        },
      };
      return {
        success: true,
        message:
          init.method === "DELETE"
            ? "Member removed."
            : "Member updated.",
        directory: administrationDashboardFixture.members,
      } as T;
    }
    if (url.pathname === "/api/frontend/settings/policy") {
      if (init.method === "PUT") {
        const payload = JSON.parse(
          String(init.body ?? "{}"),
        ) as FrontendAssistantPolicyUpdate;
        assistantPolicyFixture = {
          ...assistantPolicyFixture,
          generated_at: new Date().toISOString(),
          runtime: {
            approval_mode: payload.runtime.approval_mode,
            max_tool_calls: payload.runtime.max_tool_calls,
            effective_max_tool_calls: payload.runtime.max_tool_calls,
            tool_budget_supported: true,
          },
          memory: {
            ...assistantPolicyFixture.memory,
            user_hot_limit: payload.memory.user_hot_limit,
            organization_hot_limit:
              payload.memory.organization_hot_limit,
            include_archive_by_default:
              payload.memory.include_archive_by_default,
          },
          summary: {
            approval_mode: payload.runtime.approval_mode,
            tool_budget: payload.runtime.max_tool_calls,
            user_hot_limit: payload.memory.user_hot_limit,
            archive_recall_default:
              payload.memory.include_archive_by_default,
            configuration_needs_review: false,
          },
        };
        return {
          success: true,
          message: "Assistant policy updated.",
          policy: assistantPolicyFixture,
        } as T;
      }
      assistantPolicyFixture = {
        ...assistantPolicyFixture,
        generated_at: new Date().toISOString(),
      };
      return assistantPolicyFixture as T;
    }
    if (url.pathname === "/api/frontend/settings/guidance") {
      if (init.method === "PUT") {
        const payload = JSON.parse(
          String(init.body ?? "{}"),
        ) as FrontendAssistantGuidanceUpdate;
        const prompts = guidanceDashboardFixture.prompts.map((prompt) => {
          const draft = payload.prompts[prompt.id]?.trim() ?? "";
          const usesDefault =
            !draft || draft === prompt.default_value.trim();
          return {
            ...prompt,
            value: usesDefault ? prompt.default_value : draft,
            customized: !usesDefault,
          };
        });
        guidanceDashboardFixture = {
          ...guidanceDashboardFixture,
          generated_at: new Date().toISOString(),
          organization: {
            ...guidanceDashboardFixture.organization,
            enabled: payload.organization.enabled,
            profile: payload.organization.profile.trim(),
          },
          prompts,
          summary: {
            ...guidanceDashboardFixture.summary,
            organization_enabled: payload.organization.enabled,
            customized_prompts: prompts.filter(
              (prompt) => prompt.customized,
            ).length,
          },
        };
        return {
          success: true,
          message: "Assistant guidance updated.",
          guidance: guidanceDashboardFixture,
        } as T;
      }
      guidanceDashboardFixture = {
        ...guidanceDashboardFixture,
        generated_at: new Date().toISOString(),
      };
      return guidanceDashboardFixture as T;
    }
    if (url.pathname === "/api/frontend/settings/intelligence") {
      intelligenceDashboardFixture = {
        ...intelligenceDashboardFixture,
        generated_at: new Date().toISOString(),
      };
      return intelligenceDashboardFixture as T;
    }
    if (
      url.pathname.startsWith(
        "/api/frontend/settings/intelligence/providers/",
      )
    ) {
      const providerId = Number(url.pathname.split("/").at(-1));
      const provider = providerDetailFixtures.get(providerId);
      if (!provider) throw new Error("The selected provider was not found.");
      return provider as T;
    }
    if (
      url.pathname === "/api/frontend/settings/intelligence/preferences" &&
      init.method === "PUT"
    ) {
      const payload = JSON.parse(String(init.body ?? "{}")) as {
        image_detail_configs?: Record<string, string>;
        default_models?: {
          fast?: string;
          medium?: string;
          powerful?: string;
        };
      };
      if (payload.image_detail_configs) {
        intelligenceDashboardFixture = {
          ...intelligenceDashboardFixture,
          image_detail: {
            ...intelligenceDashboardFixture.image_detail,
            configs: { ...payload.image_detail_configs },
          },
        };
      }
      if (payload.default_models) {
        intelligenceDashboardFixture = {
          ...intelligenceDashboardFixture,
          routing: {
            ...intelligenceDashboardFixture.routing,
            defaults: {
              ...intelligenceDashboardFixture.routing.defaults,
              ...payload.default_models,
            },
          },
        };
      }
      return { success: true, message: "Model preferences saved." } as T;
    }
    if (url.pathname === "/api/frontend/settings/sharing") {
      sharingDashboardFixture = {
        ...sharingDashboardFixture,
        generated_at: new Date().toISOString(),
      };
      return sharingDashboardFixture as T;
    }
    if (
      url.pathname === "/api/credential-delegations" &&
      init.method === "POST"
    ) {
      return {
        success: true,
        message: "Fixture access permissions were updated.",
      } as T;
    }
    if (
      url.pathname === "/api/credential-delegations" &&
      init.method === "DELETE"
    ) {
      const payload = JSON.parse(String(init.body ?? "{}")) as {
        delegatee_id?: number;
        provider_name?: string;
      };
      sharingDashboardFixture = {
        ...sharingDashboardFixture,
        delegations: sharingDashboardFixture.delegations.filter(
          (delegation) =>
            delegation.delegatee_id !== payload.delegatee_id ||
            delegation.provider_id !== payload.provider_name,
        ),
        summary: {
          ...sharingDashboardFixture.summary,
          delegations: Math.max(
            0,
            sharingDashboardFixture.summary.delegations - 1,
          ),
        },
      };
      return { success: true, message: "Fixture access was removed." } as T;
    }
    if (
      url.pathname.startsWith("/api/llm/") &&
      init.method &&
      init.method !== "GET"
    ) {
      return {
        success: true,
        message: "Fixture model settings were updated.",
      } as T;
    }
    if (url.pathname === "/api/frontend/runtime/operations") {
      operationsDashboardFixture = {
        ...operationsDashboardFixture,
        generated_at: new Date().toISOString(),
      };
      return operationsDashboardFixture as T;
    }
    if (
      url.pathname.startsWith("/api/runtime/approvals/") &&
      init.method === "POST"
    ) {
      const approvalId = decodeURIComponent(
        url.pathname.replace("/api/runtime/approvals/", ""),
      );
      const payload = JSON.parse(String(init.body ?? "{}")) as {
        status?: string;
      };
      operationsDashboardFixture = {
        ...operationsDashboardFixture,
        approvals: operationsDashboardFixture.approvals.map((approval) =>
          approval.id === approvalId
            ? {
                ...approval,
                status: payload.status ?? approval.status,
                status_label:
                  (payload.status ?? approval.status).replace(/^\w/, (value) =>
                    value.toUpperCase(),
                  ),
                resolved_at: new Date().toISOString(),
                can_approve: false,
                can_reject: false,
                can_cancel: false,
              }
            : approval,
        ),
        summary: {
          ...operationsDashboardFixture.summary,
          pending_approvals: 0,
          attention: Math.max(
            0,
            operationsDashboardFixture.summary.attention - 1,
          ),
        },
        counts: {
          ...operationsDashboardFixture.counts,
          approvals: {
            ...operationsDashboardFixture.counts.approvals,
            pending: 0,
            [payload.status ?? "approved"]: 1,
          },
        },
      };
      return { success: true, message: "Approval updated." } as T;
    }
    if (
      init.method === "POST" &&
      (url.pathname.startsWith("/api/addons/") ||
        url.pathname.startsWith("/api/system/docker/") ||
        url.pathname === "/api/system/restart")
    ) {
      return {
        success: true,
        message: "The runtime operation was accepted.",
      } as T;
    }
    if (url.pathname === "/api/frontend/automations") {
      return automationDashboard() as T;
    }
    if (
      url.pathname.startsWith("/api/frontend/automations/templates/")
    ) {
      const templateId = decodeURIComponent(
        url.pathname.replace("/api/frontend/automations/templates/", ""),
      );
      const template = automationTemplates.find(
        (item) => item.id === templateId,
      );
      if (!template) throw new Error("The selected template was not found.");
      return template as T;
    }
    if (
      url.pathname.startsWith("/api/frontend/automations/workflows/") &&
      url.pathname.endsWith("/readiness")
    ) {
      const workflowId = decodeURIComponent(
        url.pathname
          .replace("/api/frontend/automations/workflows/", "")
          .replace("/readiness", ""),
      );
      const workflow = automationWorkflows.find(
        (item) => item.id === workflowId,
      );
      if (!workflow) throw new Error("The selected workflow was not found.");
      return {
        ...automationReadiness,
        workflow,
        execution_supported: workflow.execution_supported,
        step_count: workflow.steps.length,
        message: workflow.execution_supported
          ? "Workflow dry-run validation passed."
          : "Dependencies are available; this workflow uses a workspace handoff.",
      } as T;
    }
    if (
      url.pathname === "/api/runtime/workflow-templates" &&
      init.method === "POST"
    ) {
      const payload = JSON.parse(String(init.body ?? "{}")) as {
        title?: string;
        description?: string;
        is_active?: boolean;
        definition?: FrontendAutomationTemplate["definition"];
        change_summary?: string;
      };
      const id = `template-${Date.now()}`;
      const definition =
        payload.definition ?? automationTemplates[0]!.definition;
      const created: FrontendAutomationTemplate = {
        id,
        title: payload.title ?? definition.name,
        description: payload.description ?? definition.description,
        owner_id: 7,
        scope: "personal",
        editable: true,
        is_active: payload.is_active ?? true,
        active_version: 1,
        version_count: 1,
        step_count: definition.steps.length,
        definition_error: null,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        definition,
        versions: [
          {
            version: 1,
            name: definition.name,
            description: definition.description,
            step_count: definition.steps.length,
            created_at: new Date().toISOString(),
            created_by_user_id: 7,
            change_summary: payload.change_summary ?? "Initial version",
            active: true,
          },
        ],
      };
      automationTemplates = [created, ...automationTemplates];
      return { id } as T;
    }
    if (url.pathname.startsWith("/api/runtime/workflow-templates/")) {
      const suffix = url.pathname.replace(
        "/api/runtime/workflow-templates/",
        "",
      );
      const [encodedTemplateId, action] = suffix.split("/");
      const templateId = decodeURIComponent(encodedTemplateId ?? "");
      const template = automationTemplates.find(
        (item) => item.id === templateId,
      );
      if (!template) throw new Error("The selected template was not found.");
      if (action === "runs" && init.method === "POST") {
        return {
          id: "run-editorial-brief",
          display_title: template.title,
          kind: "template",
          workflow_id: `user_template:${template.id}`,
          status: "ready",
          step_ids: template.definition.steps.map((step) => step.key),
        } as T;
      }
      if (init.method === "DELETE") {
        automationTemplates = automationTemplates.filter(
          (item) => item.id !== templateId,
        );
        return { success: true, id: templateId } as T;
      }
      if (init.method === "PATCH" || init.method === "POST") {
        const payload = JSON.parse(String(init.body ?? "{}")) as {
          title?: string;
          description?: string;
          is_active?: boolean;
          active_version?: number;
          definition?: FrontendAutomationTemplate["definition"];
          change_summary?: string;
        };
        if (payload.title !== undefined) template.title = payload.title;
        if (payload.description !== undefined) {
          template.description = payload.description;
        }
        if (payload.is_active !== undefined) {
          template.is_active = payload.is_active;
        }
        if (payload.definition) {
          const nextVersion = template.version_count + 1;
          template.definition = payload.definition;
          template.step_count = payload.definition.steps.length;
          template.version_count = nextVersion;
          template.active_version = nextVersion;
          template.versions.forEach((version) => (version.active = false));
          template.versions.push({
            version: nextVersion,
            name: payload.definition.name,
            description: payload.definition.description,
            step_count: payload.definition.steps.length,
            created_at: new Date().toISOString(),
            created_by_user_id: 7,
            change_summary:
              payload.change_summary ?? `Version ${nextVersion}`,
            active: true,
          });
        } else if (payload.active_version !== undefined) {
          template.active_version = payload.active_version;
          template.versions.forEach(
            (version) =>
              (version.active = version.version === payload.active_version),
          );
        }
        template.updated_at = new Date().toISOString();
        return template as T;
      }
    }
    if (
      url.pathname === "/api/runtime/runs" &&
      init.method === "POST"
    ) {
      const payload = JSON.parse(String(init.body ?? "{}")) as {
        title?: string;
        workflow_id?: string;
      };
      return {
        id: "run-editorial-brief",
        display_title: payload.title ?? "Automation run",
        kind: "template",
        workflow_id: payload.workflow_id ?? null,
        status: "ready",
        step_ids: automationWorkflows[0]!.steps.map((step) => step.key),
      } as T;
    }
    if (url.pathname === "/api/frontend/runs") {
      return {
        items: workspaceRuns.map((run) => ({
          ...run,
          status_label: run.status
            .replace(/[_-]+/g, " ")
            .replace(/^\w/, (value) => value.toUpperCase()),
        })),
        total: workspaceRuns.length,
      } satisfies FrontendRunDirectory as T;
    }
    if (
      url.pathname ===
      "/api/frontend/runs/run-editorial-brief/studio"
    ) {
      return runStudioFixture as T;
    }
    if (
      url.pathname.startsWith(
        "/api/frontend/runs/run-editorial-brief/steps/",
      )
    ) {
      const stepId = decodeURIComponent(
        url.pathname.split("/").at(-1) ?? "",
      );
      const detail = runStepDetails.get(stepId);
      if (!detail) throw new Error("The selected step was not found.");
      return detail as T;
    }
    if (
      url.pathname ===
      "/api/frontend/runs/run-editorial-brief/artifacts/artifact-brief"
    ) {
      return artifactDetail as T;
    }
    if (
      url.pathname ===
      "/api/frontend/runs/run-editorial-brief/developer"
    ) {
      return {
        run: {
          id: runStudioFixture.run.id,
          status: runStudioFixture.run.status,
          plan: {
            steps: runStudioFixture.graph.nodes.map((step) => ({
              id: step.id,
              depends_on_step_ids: step.dependency_ids,
            })),
          },
        },
        steps: runStudioFixture.graph.nodes,
        jobs: [
          {
            id: "job-recommendations",
            status: "running",
            progress: 68,
          },
        ],
        notice:
          "Sensitive credential-shaped fields remain redacted from this developer payload.",
      } as T;
    }
    if (
      url.pathname.startsWith(
        "/api/runtime/runs/run-editorial-brief/",
      ) &&
      init.method === "POST"
    ) {
      const action = url.pathname.split("/").at(-1);
      if (action === "pause") {
        runStudioFixture.run.status = "paused";
        runStudioFixture.run.status_label = "Paused";
        runStudioFixture.run.controls = [
          {
            id: "resume",
            label: "Resume run",
            method: "POST",
            url: "/api/runtime/runs/run-editorial-brief/resume",
            tone: "primary",
          },
          {
            id: "cancel",
            label: "Cancel",
            method: "POST",
            url: "/api/runtime/runs/run-editorial-brief/cancel",
            tone: "danger",
          },
        ];
      } else if (action === "resume") {
        runStudioFixture.run.status = "running";
        runStudioFixture.run.status_label = "Running";
      } else if (action === "cancel") {
        runStudioFixture.run.status = "cancelled";
        runStudioFixture.run.status_label = "Cancelled";
        runStudioFixture.run.controls = [];
      }
      return { success: true } as T;
    }
    if (
      url.pathname === "/api/runtime/approvals/approval-direction" &&
      init.method === "POST"
    ) {
      const body = JSON.parse(String(init.body ?? "{}")) as {
        status?: string;
      };
      runStudioFixture.approvals[0]!.status = body.status ?? "approved";
      runStudioFixture.approvals[0]!.can_resolve = false;
      runStudioFixture.run.counts.pending_approvals = 0;
      return runStudioFixture.approvals[0] as T;
    }
    if (
      url.pathname.startsWith("/api/runtime/steps/") &&
      url.pathname.endsWith("/rerun") &&
      init.method === "POST"
    ) {
      return { success: true } as T;
    }
    if (url.pathname === "/api/frontend/chats") {
      return chatDirectory as T;
    }
    if (
      url.pathname ===
      "/api/frontend/chats/autumn-brief/history"
    ) {
      return {
        ...chatHistory,
        debug: url.searchParams.get("debug") === "1",
      } as T;
    }
    if (
      url.pathname ===
      "/api/frontend/chats/autumn-brief/workspace"
    ) {
      return workspaceSummary as T;
    }
    if (
      url.pathname ===
      "/api/frontend/chats/autumn-brief/runs"
    ) {
      const view = url.searchParams.get("view") ?? "all";
      const filtered = workspaceRuns.filter((run) => {
        if (view === "active") return run.active;
        if (view === "attention") return run.needs_attention;
        if (view === "completed") return !run.active;
        return true;
      });
      return {
        items: filtered,
        total: filtered.length,
        next_cursor: null,
      } satisfies FrontendRunPage as T;
    }
    if (
      url.pathname ===
      "/api/frontend/chats/autumn-brief/memory"
    ) {
      const scope = url.searchParams.get("scope") ?? "all";
      const query = (url.searchParams.get("q") ?? "").toLowerCase();
      const filtered = workspaceMemory.items.filter(
        (item) =>
          (scope === "all" || item.scope === scope) &&
          (!query ||
            `${item.title} ${item.summary} ${String(item.value)}`
              .toLowerCase()
              .includes(query)),
      );
      return {
        ...workspaceMemory,
        items: filtered,
        total: filtered.length,
        scope,
        query,
      } as T;
    }
    if (
      url.pathname ===
      "/api/frontend/chats/autumn-brief/context-items"
    ) {
      const lifecycleState = url.searchParams.get("state") ?? "all";
      const query = (url.searchParams.get("q") ?? "").toLowerCase();
      const filtered = workspaceContextItems.filter(
        (item) =>
          (lifecycleState === "all" ||
            item.lifecycle_state === lifecycleState) &&
          (!query ||
            `${item.id} ${item.evidence_id} ${item.title} ${item.kind}`
              .toLowerCase()
              .includes(query)),
      );
      return {
        items: filtered,
        total: filtered.length,
        next_cursor: null,
        state: lifecycleState as FrontendContextPage["state"],
        materialization: "all",
        query,
        counts: {
          all: workspaceContextItems.length,
          active: workspaceContextItems.filter(
            (item) => item.lifecycle_state === "active",
          ).length,
          archived: workspaceContextItems.filter(
            (item) => item.lifecycle_state === "archived",
          ).length,
          excluded: workspaceContextItems.filter(
            (item) => item.lifecycle_state === "excluded",
          ).length,
        },
        search_truncated: false,
      } satisfies FrontendContextPage as T;
    }
    if (
      url.pathname.startsWith(
        "/api/frontend/chats/autumn-brief/context-items/",
      )
    ) {
      const itemId = decodeURIComponent(url.pathname.split("/").at(-1) ?? "");
      const item = workspaceContextItems.find(
        (candidate) => candidate.id === itemId,
      );
      if (!item) throw new Error("Context item not found.");
      if (init.method === "PATCH") {
        const body = JSON.parse(String(init.body ?? "{}")) as {
          action?: string;
        };
        const action = body.action ?? "reference";
        const updated: FrontendContextItem = {
          ...item,
          lifecycle_state:
            action === "archive"
              ? "archived"
              : action === "exclude"
                ? "excluded"
                : action === "restore" ||
                    action === "full" ||
                    action === "reference"
                  ? "active"
                  : item.lifecycle_state,
          materialization:
            action === "full"
              ? "full"
              : action === "archive" ||
                  action === "exclude" ||
                  action === "restore" ||
                  action === "reference"
                ? "reference"
                : item.materialization,
          is_pinned:
            action === "pin"
              ? true
              : action === "unpin"
                ? false
                : item.is_pinned,
          version: item.version + 1,
          updated_at: new Date().toISOString(),
        };
        workspaceContextItems = workspaceContextItems.map((candidate) =>
          candidate.id === itemId ? updated : candidate
        );
        return {
          status: "updated",
          action,
          item: updated,
        } satisfies FrontendContextUpdate as T;
      }
      const body = workspaceContextBodies[itemId] ?? "";
      const startOffset = Number(url.searchParams.get("start_offset") ?? "0");
      const maxChars = Number(url.searchParams.get("max_chars") ?? "20000");
      const content = body.slice(startOffset, startOffset + maxChars);
      const endOffset = startOffset + content.length;
      return {
        item,
        content,
        start_offset: startOffset,
        end_offset: endOffset,
        has_more: endOffset < body.length,
        next_offset: endOffset < body.length ? endOffset : null,
        integrity_verified: true,
        evidence: {
          id: item.evidence_id,
          kind: item.kind,
          role: "tool",
          mime_type: item.mime_type,
          char_count: body.length,
          byte_count: body.length,
          token_estimate: item.token_estimate,
          content_hash: item.content_hash,
        },
      } satisfies FrontendContextDetail as T;
    }
    if (
      url.pathname ===
      "/api/frontend/chats/autumn-brief/artifacts"
    ) {
      const status = url.searchParams.get("status") ?? "all";
      const query = (url.searchParams.get("q") ?? "").toLowerCase();
      const filtered = workspaceArtifacts.filter(
        (artifact) =>
          (status === "all" || artifact.review_status === status) &&
          (!query ||
            `${artifact.label} ${artifact.type} ${artifact.mime_type}`
              .toLowerCase()
              .includes(query)),
      );
      return {
        items: filtered,
        total: filtered.length,
        next_cursor: null,
        search_truncated: false,
      } satisfies FrontendArtifactPage as T;
    }
    if (
      url.pathname ===
      "/api/frontend/chats/autumn-brief/artifacts/artifact-brief"
    ) {
      return artifactDetail as T;
    }
    if (
      url.pathname ===
      "/api/runtime/artifacts/artifact-brief/content"
    ) {
      return artifactContent as T;
    }
    if (
      url.pathname ===
        "/api/runtime/artifacts/artifact-brief/review" &&
      init.method === "PATCH"
    ) {
      const payload = JSON.parse(String(init.body ?? "{}")) as {
        review_status?: string;
        review_note?: string;
      };
      artifactDetail.artifact.review_status =
        payload.review_status ?? "approved";
      artifactDetail.artifact.review = {
        ...artifactDetail.artifact.review,
        status: payload.review_status ?? "approved",
        note: payload.review_note ?? "",
        can_review: false,
      };
      workspaceArtifacts[0]!.review_status =
        payload.review_status ?? "approved";
      return artifactDetail.artifact as T;
    }
    if (url.pathname === "/api/llm/models") {
      return chatModels as T;
    }
    if (url.pathname === "/api/capabilities") {
      return chatCapabilities as T;
    }
    if (url.pathname === "/api/slash-commands") {
      return chatCommands as T;
    }
    if (url.pathname === "/api/log-sources") {
      return { sources: logSources } as T;
    }
    if (url.pathname.startsWith("/api/logs/")) {
      const id = decodeURIComponent(
        url.pathname.replace("/api/logs/", "").replace("/download", ""),
      );
      if (init.method === "DELETE") {
        sampleLogs[id] = "";
        const source = logSources.find((item) => item.id === id);
        if (source) source.size = 0;
        return { success: true, message: "Log cleared." } as T;
      }
      const source = logSources.find((item) => item.id === id) ?? logSources[0];
      return {
        text: sampleLogs[id] ?? "",
        next_cursor: 0,
        source,
        warning: source.warning,
      } satisfies FrontendLogChunk as T;
    }
    if (url.pathname === "/api/frontend/profile") {
      if (init.method === "PUT") {
        const payload = JSON.parse(String(init.body)) as {
          preferences: string;
          style_reference: string;
        };
        profile = { ...profile, ...payload };
        return { success: true, profile } as T;
      }
      return profile as T;
    }
    throw new Error(`The visual fixture has no response for ${url.pathname}.`);
  },
};

const target = document.getElementById("dev-root");
if (target) {
  mountCoreApplication(target, {
    api,
    bootstrap,
    initialPath: `${window.location.pathname}${window.location.search}`,
  });
}
