<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import type {
    FrontendApiClient,
    FrontendAutomationDashboard,
    FrontendAutomationReadiness,
    FrontendAutomationRunCreated,
    FrontendAutomationTemplate,
    FrontendAutomationTemplateSummary,
    FrontendAutomationWorkflow,
    FrontendBootstrap,
    FrontendRunDirectory,
  } from "@carlina/sdk";
  import { shouldUseClientNavigation } from "@carlina/sdk";
  import StatusMark from "@carlina/runtime-ui/status-mark";
  import {
    Badge,
    Button,
    Dialog,
    EmptyState,
    Icon,
    PageHeader,
    SearchField,
  } from "@carlina/ui";

  import {
    automationRequirementCount,
    humanizeAutomation,
    templateSearchText,
    workflowSearchText,
    type AutomationTemplateSaveInput,
  } from "../lib/automations.ts";
  import TemplateEditor from "./automations/TemplateEditor.svelte";
  import WorkflowGraphPanel from "./automations/WorkflowGraphPanel.svelte";

  type AutomationView = "templates" | "catalogue" | "runs" | "editor";
  type RunTarget =
    | { kind: "template"; item: FrontendAutomationTemplateSummary }
    | { kind: "workflow"; item: FrontendAutomationWorkflow };

  interface Props {
    api: FrontendApiClient;
    bootstrap: FrontendBootstrap;
    route: string;
    navigate: (
      href: string,
      clientRoute?: boolean,
      options?: { replace?: boolean },
    ) => "client" | "document";
  }

  let { api, route, navigate }: Props = $props();

  let dashboard = $state<FrontendAutomationDashboard | null>(null);
  let dashboardLoading = $state(true);
  let dashboardError = $state("");
  let runs = $state<FrontendRunDirectory | null>(null);
  let runsLoading = $state(true);
  let runsError = $state("");
  let templateDetail = $state<FrontendAutomationTemplate | null>(null);
  let templateLoading = $state(false);
  let templateError = $state("");
  let query = $state("");
  let selectedStepKey = $state<string | null>(null);
  let readiness = $state<FrontendAutomationReadiness | null>(null);
  let readinessLoading = $state(false);
  let readinessError = $state("");
  let feedback = $state("");
  let feedbackTone = $state<"success" | "danger">("success");
  let savingTemplate = $state(false);
  let deletingTemplate = $state(false);
  let deleteTarget = $state<FrontendAutomationTemplateSummary | null>(null);
  let deleteConfirmation = $state("");
  let runTarget = $state<RunTarget | null>(null);
  let runTitle = $state("");
  let runImmediately = $state(false);
  let creatingRun = $state(false);
  let dashboardController: AbortController | null = null;
  let runsController: AbortController | null = null;
  let templateController: AbortController | null = null;
  let readinessController: AbortController | null = null;

  const routeUrl = $derived(new URL(route, "https://iarf.local"));
  const requestedView = $derived(parseView(routeUrl.searchParams.get("view")));
  const requestedTemplateId = $derived(
    routeUrl.searchParams.get("template") || null,
  );
  const requestedWorkflowId = $derived(
    routeUrl.searchParams.get("workflow") || null,
  );
  const normalizedQuery = $derived(query.trim().toLocaleLowerCase());
  const filteredTemplates = $derived(
    (dashboard?.templates ?? []).filter(
      (item) =>
        !normalizedQuery || templateSearchText(item).includes(normalizedQuery),
    ),
  );
  const filteredWorkflows = $derived(
    (dashboard?.workflows ?? []).filter(
      (item) =>
        !normalizedQuery || workflowSearchText(item).includes(normalizedQuery),
    ),
  );
  const selectedTemplateSummary = $derived(
    (requestedTemplateId
      ? filteredTemplates.find((item) => item.id === requestedTemplateId)
      : null) ??
      filteredTemplates[0] ??
      null,
  );
  const selectedWorkflow = $derived(
    (requestedWorkflowId
      ? filteredWorkflows.find((item) => item.id === requestedWorkflowId)
      : null) ??
      filteredWorkflows[0] ??
      null,
  );
  const selectedWorkflowDefinition = $derived(
    selectedWorkflow
      ? {
          version: 1,
          name: selectedWorkflow.name,
          description: selectedWorkflow.description,
          steps: selectedWorkflow.steps,
        }
      : null,
  );
  const immediateStartAvailable = $derived(
    !runTarget ||
      runTarget.kind === "template" ||
      (
        readiness?.workflow.id === runTarget.item.id &&
        readiness.can_dry_run &&
        readiness.execution_supported
      ),
  );

  onMount(() => {
    void Promise.all([loadDashboard(), loadRuns()]);
  });

  onDestroy(() => {
    dashboardController?.abort();
    runsController?.abort();
    templateController?.abort();
    readinessController?.abort();
  });

  $effect(() => {
    const templateId =
      requestedView === "editor"
        ? requestedTemplateId
        : requestedView === "templates"
          ? selectedTemplateSummary?.id ?? null
          : null;
    if (!templateId) {
      templateController?.abort();
      templateDetail = null;
      templateError = "";
      selectedStepKey = null;
      return;
    }
    if (templateDetail?.id !== templateId) {
      void loadTemplate(templateId);
    }
  });

  $effect(() => {
    const workflowId = selectedWorkflow?.id ?? null;
    readinessController?.abort();
    readiness = null;
    readinessError = "";
    selectedStepKey = selectedWorkflow?.steps[0]?.key ?? null;
    workflowId;
  });

  function parseView(value: string | null): AutomationView {
    if (value === "catalogue" || value === "runs" || value === "editor") {
      return value;
    }
    return "templates";
  }

  function automationRoute(
    view: Exclude<AutomationView, "editor">,
    selection?: { template?: string; workflow?: string },
  ): string {
    const url = new URL("/automations", "https://iarf.local");
    url.searchParams.set("view", view);
    if (selection?.template) {
      url.searchParams.set("template", selection.template);
    }
    if (selection?.workflow) {
      url.searchParams.set("workflow", selection.workflow);
    }
    return `${url.pathname}${url.search}`;
  }

  function editorRoute(templateId?: string): string {
    const url = new URL("/automations", "https://iarf.local");
    url.searchParams.set("view", "editor");
    if (templateId) url.searchParams.set("template", templateId);
    return `${url.pathname}${url.search}`;
  }

  function changeView(view: Exclude<AutomationView, "editor">): void {
    query = "";
    navigate(automationRoute(view), true);
  }

  function showFeedback(
    message: string,
    tone: "success" | "danger" = "success",
  ): void {
    feedback = message;
    feedbackTone = tone;
    window.setTimeout(() => {
      if (feedback === message) feedback = "";
    }, 6000);
  }

  async function loadDashboard(silent = false): Promise<void> {
    dashboardController?.abort();
    const controller = new AbortController();
    dashboardController = controller;
    if (!silent) dashboardLoading = true;
    dashboardError = "";
    try {
      dashboard = await api.request<FrontendAutomationDashboard>(
        "/api/frontend/automations",
        { signal: controller.signal },
      );
    } catch (reason) {
      if (controller.signal.aborted) return;
      dashboardError =
        reason instanceof Error
          ? reason.message
          : "The automation library could not be loaded.";
    } finally {
      if (dashboardController === controller) {
        dashboardController = null;
        if (!silent) dashboardLoading = false;
      }
    }
  }

  async function loadRuns(silent = false): Promise<void> {
    runsController?.abort();
    const controller = new AbortController();
    runsController = controller;
    if (!silent) runsLoading = true;
    runsError = "";
    try {
      runs = await api.request<FrontendRunDirectory>(
        "/api/frontend/runs?limit=24",
        { signal: controller.signal },
      );
    } catch (reason) {
      if (controller.signal.aborted) return;
      runsError =
        reason instanceof Error
          ? reason.message
          : "Recent automation runs could not be loaded.";
    } finally {
      if (runsController === controller) {
        runsController = null;
        if (!silent) runsLoading = false;
      }
    }
  }

  async function loadTemplate(templateId: string): Promise<void> {
    templateController?.abort();
    const controller = new AbortController();
    templateController = controller;
    templateLoading = true;
    templateError = "";
    try {
      const payload = await api.request<FrontendAutomationTemplate>(
        `/api/frontend/automations/templates/${encodeURIComponent(templateId)}`,
        { signal: controller.signal },
      );
      templateDetail = payload;
      selectedStepKey = payload.definition.steps[0]?.key ?? null;
    } catch (reason) {
      if (controller.signal.aborted) return;
      templateDetail = null;
      templateError =
        reason instanceof Error
          ? reason.message
          : "The selected template could not be loaded.";
    } finally {
      if (templateController === controller) {
        templateController = null;
        templateLoading = false;
      }
    }
  }

  async function saveTemplate(
    payload: AutomationTemplateSaveInput,
  ): Promise<void> {
    if (savingTemplate) return;
    savingTemplate = true;
    templateError = "";
    try {
      const templateId = requestedTemplateId;
      const result = await api.request<{ id: string }>(
        templateId
          ? `/api/runtime/workflow-templates/${encodeURIComponent(templateId)}`
          : "/api/runtime/workflow-templates",
        {
          method: templateId ? "PATCH" : "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        },
      );
      templateDetail = null;
      await loadDashboard(true);
      showFeedback(
        templateId
          ? "A new template version was saved."
          : "Automation template created.",
      );
      navigate(
        automationRoute("templates", { template: result.id }),
        true,
      );
    } catch (reason) {
      templateError =
        reason instanceof Error
          ? reason.message
          : "The automation template could not be saved.";
    } finally {
      savingTemplate = false;
    }
  }

  async function activateVersion(version: number): Promise<void> {
    if (!templateDetail?.editable || savingTemplate) return;
    savingTemplate = true;
    try {
      await api.request(
        `/api/runtime/workflow-templates/${encodeURIComponent(templateDetail.id)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ active_version: version }),
        },
      );
      await Promise.all([
        loadTemplate(templateDetail.id),
        loadDashboard(true),
      ]);
      showFeedback(`Version ${version} is now active.`);
    } catch (reason) {
      showFeedback(
        reason instanceof Error
          ? reason.message
          : "The active version could not be changed.",
        "danger",
      );
    } finally {
      savingTemplate = false;
    }
  }

  function requestDelete(item: FrontendAutomationTemplateSummary): void {
    deleteTarget = item;
    deleteConfirmation = "";
  }

  async function deleteTemplate(): Promise<void> {
    if (
      !deleteTarget ||
      deleteConfirmation.trim() !== deleteTarget.title ||
      deletingTemplate
    ) {
      return;
    }
    deletingTemplate = true;
    try {
      await api.request(
        `/api/runtime/workflow-templates/${encodeURIComponent(deleteTarget.id)}`,
        { method: "DELETE" },
      );
      deleteTarget = null;
      deleteConfirmation = "";
      templateDetail = null;
      await loadDashboard(true);
      showFeedback("Automation template deleted.");
      navigate(automationRoute("templates"), true);
    } catch (reason) {
      showFeedback(
        reason instanceof Error
          ? reason.message
          : "The template could not be deleted.",
        "danger",
      );
    } finally {
      deletingTemplate = false;
    }
  }

  function requestRun(target: RunTarget): void {
    runTarget = target;
    runTitle =
      target.kind === "template"
        ? target.item.title
        : target.item.name;
    runImmediately = false;
  }

  async function createRun(): Promise<void> {
    if (!runTarget || creatingRun || !runTitle.trim()) return;
    creatingRun = true;
    try {
      const isTemplate = runTarget.kind === "template";
      const startRequested =
        runImmediately && immediateStartAvailable;
      const result = await api.request<FrontendAutomationRunCreated>(
        isTemplate
          ? `/api/runtime/workflow-templates/${encodeURIComponent(runTarget.item.id)}/runs`
          : "/api/runtime/runs",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            title: runTitle.trim(),
            status: startRequested ? "running" : "ready",
            start: startRequested,
            ...(!isTemplate
              ? {
                  kind: "template",
                  workflow_id: runTarget.item.id,
                }
              : {}),
          }),
        },
      );
      runTarget = null;
      await loadRuns(true);
      navigate(`/runs/${encodeURIComponent(result.id)}`, true);
    } catch (reason) {
      showFeedback(
        reason instanceof Error
          ? reason.message
          : "The automation run could not be created.",
        "danger",
      );
    } finally {
      creatingRun = false;
    }
  }

  async function validateWorkflow(
    workflow: FrontendAutomationWorkflow,
  ): Promise<void> {
    readinessController?.abort();
    const controller = new AbortController();
    readinessController = controller;
    readinessLoading = true;
    readinessError = "";
    readiness = null;
    try {
      readiness = await api.request<FrontendAutomationReadiness>(
        `/api/frontend/automations/workflows/${encodeURIComponent(workflow.id)}/readiness`,
        { signal: controller.signal },
      );
    } catch (reason) {
      if (controller.signal.aborted) return;
      readinessError =
        reason instanceof Error
          ? reason.message
          : "Workflow readiness could not be checked.";
    } finally {
      if (readinessController === controller) {
        readinessController = null;
        readinessLoading = false;
      }
    }
  }

  function openInternalLink(href: string, event: MouseEvent): void {
    if (!href.startsWith("/") || !shouldUseClientNavigation(event)) return;
    event.preventDefault();
    navigate(href, true);
  }

  function openRun(runId: string): void {
    navigate(`/runs/${encodeURIComponent(runId)}`, true);
  }

  function formatDate(value: string | null): string {
    if (!value) return "Not recorded";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(date);
  }

  function statusTone(
    state: string,
  ): "neutral" | "success" | "warning" | "danger" {
    const normalized = state.toLocaleLowerCase();
    if (["active", "ready", "completed", "running"].includes(normalized)) {
      return "success";
    }
    if (["degraded", "paused", "waiting_for_approval"].includes(normalized)) {
      return "warning";
    }
    if (["failed", "missing_dependency", "misconfigured"].includes(normalized)) {
      return "danger";
    }
    return "neutral";
  }
</script>

<svelte:head>
  <meta
    name="description"
    content="Design, validate, and launch reusable Carlina workflow automations."
  />
</svelte:head>

{#if requestedView === "editor"}
  {#if requestedTemplateId && templateError}
    <section class="route-error" role="alert">
      <Icon name="activity" size={24} />
      <h1>Template could not open</h1>
      <p>{templateError}</p>
      <Button variant="outline" onclick={() => navigate(automationRoute("templates"), true)}>
        Back to templates
      </Button>
    </section>
  {:else if requestedTemplateId && (templateLoading || templateDetail?.id !== requestedTemplateId)}
    <section class="editor-loading" aria-busy="true">
      <span></span>
      <span></span>
      <p>Opening the visual template builder…</p>
    </section>
  {:else}
    {#key requestedTemplateId ?? "new-template"}
      <TemplateEditor
        template={requestedTemplateId ? templateDetail : null}
        saving={savingTemplate}
        error={templateError}
        onsave={saveTemplate}
        oncancel={() =>
          navigate(
            automationRoute(
              "templates",
              requestedTemplateId
                ? { template: requestedTemplateId }
                : undefined,
            ),
            true,
          )}
      />
    {/key}
  {/if}
{:else}
  <div class="automations-page">
    <PageHeader
      eyebrow="Workspaces / Repeatable work"
      title="Automations"
      description="Shape reusable workflows, inspect addon recipes, and hand durable runs to Run Studio—without exposing the underlying state as raw JSON."
    >
      {#snippet actions()}
        <a
          class="header-link"
          href="/workspaces"
          onclick={(event) => openInternalLink("/workspaces", event)}
        >
          Workspace directory
          <Icon name="arrow-up-right" size={14} />
        </a>
        <Button onclick={() => navigate(editorRoute(), true)}>
          <Icon name="plus" size={15} />
          New template
        </Button>
      {/snippet}
    </PageHeader>

    {#if feedback}
      <div
        class:page-feedback--danger={feedbackTone === "danger"}
        class="page-feedback"
        role="status"
        aria-live="polite"
      >
        <Icon name={feedbackTone === "danger" ? "activity" : "check"} size={16} />
        <span>{feedback}</span>
      </div>
    {/if}

    <section class="automation-summary" aria-label="Automation summary">
      <button
        class:active={requestedView === "templates"}
        onclick={() => changeView("templates")}
      >
        <span>Saved templates</span>
        <strong>{dashboard?.summary.templates ?? "—"}</strong>
        <small>{dashboard?.summary.personal_templates ?? "—"} personal</small>
      </button>
      <button
        class:active={requestedView === "catalogue"}
        onclick={() => changeView("catalogue")}
      >
        <span>Addon workflows</span>
        <strong>{dashboard?.summary.addon_workflows ?? "—"}</strong>
        <small>{dashboard?.summary.executable_workflows ?? "—"} executable</small>
      </button>
      <button
        class:active={requestedView === "runs"}
        onclick={() => changeView("runs")}
      >
        <span>Recent runs</span>
        <strong>{runs?.total ?? "—"}</strong>
        <small>{runs?.items.filter((run) => run.active).length ?? "—"} active in view</small>
      </button>
      <button
        class:active={dashboard?.summary.attention ? true : false}
        onclick={() => changeView("catalogue")}
      >
        <span>Needs attention</span>
        <strong>{dashboard?.summary.attention ?? "—"}</strong>
        <small>definition or dependency state</small>
      </button>
    </section>

    <nav class="automation-tabs" aria-label="Automation views">
      {#each [
        { id: "templates", label: "Templates" },
        { id: "catalogue", label: "Addon catalogue" },
        { id: "runs", label: "Recent runs" },
      ] as item}
        <button
          type="button"
          class:active={requestedView === item.id}
          aria-current={requestedView === item.id ? "page" : undefined}
          onclick={() => changeView(item.id as Exclude<AutomationView, "editor">)}
        >
          {item.label}
        </button>
      {/each}
    </nav>

    {#if requestedView === "templates"}
      <section class="directory-toolbar" aria-label="Template filters">
        <SearchField
          id="automation-template-search"
          value={query}
          label="Filter templates"
          placeholder="Search name, purpose, or scope…"
          oninput={(value) => (query = value)}
        />
        <p>
          <strong>{filteredTemplates.length}</strong>
          {filteredTemplates.length === 1 ? "template" : "templates"}
        </p>
      </section>

      {#if dashboardLoading && !dashboard}
        <div class="directory-loading" aria-busy="true">
          <span></span><span></span><span></span>
        </div>
      {:else if dashboardError}
        <section class="route-error" role="alert">
          <Icon name="activity" size={24} />
          <h2>Templates could not be loaded</h2>
          <p>{dashboardError}</p>
          <Button variant="outline" onclick={() => loadDashboard()}>
            Try again
          </Button>
        </section>
      {:else if filteredTemplates.length && selectedTemplateSummary}
        <div class="library-layout">
          <aside class="template-directory" aria-label="Automation templates">
            <header>
              <p>Template library</p>
              <span>{filteredTemplates.length.toString().padStart(2, "0")}</span>
            </header>
            <div>
              {#each filteredTemplates as item, index (item.id)}
                <button
                  type="button"
                  class:selected={item.id === selectedTemplateSummary.id}
                  aria-pressed={item.id === selectedTemplateSummary.id}
                  onclick={() =>
                    navigate(
                      automationRoute("templates", { template: item.id }),
                      true,
                    )}
                >
                  <span class="row-number">{String(index + 1).padStart(2, "0")}</span>
                  <span class="row-copy">
                    <span>
                      <strong>{item.title}</strong>
                      {#if item.definition_error}
                        <i title="Definition needs attention">!</i>
                      {/if}
                    </span>
                    <small>
                      {item.step_count} {item.step_count === 1 ? "step" : "steps"}
                      · v{item.active_version ?? "—"} · {item.scope}
                    </small>
                  </span>
                  <span
                    class:status-rule--active={item.is_active && !item.definition_error}
                    class:status-rule--attention={!!item.definition_error}
                    class="status-rule"
                  ></span>
                </button>
              {/each}
            </div>
          </aside>

          <section class="template-detail" aria-live="polite">
            {#if templateError}
              <div class="detail-error" role="alert">
                <Icon name="activity" size={22} />
                <h2>Template unavailable</h2>
                <p>{templateError}</p>
              </div>
            {:else if templateLoading || templateDetail?.id !== selectedTemplateSummary.id}
              <div class="detail-loading" aria-busy="true">
                <span></span>
                <span></span>
                <p>Reading the active workflow version…</p>
              </div>
            {:else if !templateDetail}
              <div class="detail-error" role="alert">
                <Icon name="activity" size={22} />
                <h2>Template unavailable</h2>
                <p>The selected template could not be read.</p>
              </div>
            {:else}
              <header class="detail-header">
                <div>
                  <p>{humanizeAutomation(templateDetail.scope)} template</p>
                  <h2>{templateDetail.title}</h2>
                  <span>{templateDetail.description || "No purpose has been recorded yet."}</span>
                </div>
                <div>
                  <Badge tone={templateDetail.is_active ? "success" : "neutral"}>
                    {templateDetail.is_active ? "Available" : "Inactive"}
                  </Badge>
                  <Badge tone={templateDetail.definition_error ? "danger" : "neutral"}>
                    {templateDetail.definition_error
                      ? "Definition issue"
                      : `Version ${templateDetail.active_version ?? "—"}`}
                  </Badge>
                </div>
              </header>

              {#if templateDetail.definition_error}
                <div class="detail-warning" role="alert">
                  <Icon name="activity" size={17} />
                  <span>{templateDetail.definition_error}</span>
                </div>
              {/if}

              <div class="detail-actions">
                <Button
                  onclick={() =>
                    requestRun({ kind: "template", item: templateDetail! })}
                  disabled={!templateDetail.is_active || !!templateDetail.definition_error}
                >
                  <Icon name="activity" size={15} />
                  Create run
                </Button>
                {#if templateDetail.editable}
                  <Button
                    variant="outline"
                    onclick={() => navigate(editorRoute(templateDetail!.id), true)}
                  >
                    <Icon name="edit" size={14} />
                    Edit template
                  </Button>
                  <Button
                    variant="quiet"
                    onclick={() => requestDelete(templateDetail!)}
                  >
                    Delete
                  </Button>
                {/if}
              </div>

              <dl class="template-metrics">
                <div>
                  <dt>Steps</dt>
                  <dd>{templateDetail.step_count}</dd>
                </div>
                <div>
                  <dt>Versions</dt>
                  <dd>{templateDetail.version_count}</dd>
                </div>
                <div>
                  <dt>Updated</dt>
                  <dd>{formatDate(templateDetail.updated_at)}</dd>
                </div>
              </dl>

              {#if templateDetail.definition.steps.length}
                <WorkflowGraphPanel
                  definition={templateDetail.definition}
                  selectedKey={selectedStepKey}
                  onselect={(key) => (selectedStepKey = key)}
                  eyebrow="Active version"
                  title="Dependency map"
                />

                <section class="step-reading">
                  <header>
                    <div>
                      <p>Workflow notes</p>
                      <h3>Steps and execution bindings</h3>
                    </div>
                    <span>{templateDetail.definition.steps.length.toString().padStart(2, "0")}</span>
                  </header>
                  <ol>
                    {#each templateDetail.definition.steps as step, index (step.key)}
                      <li class:selected={step.key === selectedStepKey}>
                        <button
                          type="button"
                          onclick={() => (selectedStepKey = step.key)}
                        >
                          <span>{String(index + 1).padStart(2, "0")}</span>
                          <span>
                            <strong>{step.title}</strong>
                            <small>
                              {humanizeAutomation(step.type)}
                              {#if step.assigned_model_role}
                                · {humanizeAutomation(step.assigned_model_role)}
                              {/if}
                            </small>
                          </span>
                        </button>
                        <div>
                          <p>{step.instructions || "No additional instructions."}</p>
                          {#if step.depends_on.length}
                            <span>After {step.depends_on.join(", ")}</span>
                          {/if}
                          {#if step.capability_ids.length || step.skill_ids.length || step.tool_ids.length}
                            <span>
                              Uses {[...step.capability_ids, ...step.skill_ids, ...step.tool_ids].join(", ")}
                            </span>
                          {/if}
                        </div>
                      </li>
                    {/each}
                  </ol>
                </section>
              {/if}

              <section class="version-history">
                <header>
                  <div>
                    <p>Recoverable history</p>
                    <h3>Template versions</h3>
                  </div>
                  <span>{templateDetail.version_count.toString().padStart(2, "0")}</span>
                </header>
                <ol>
                  {#each [...templateDetail.versions].reverse() as version (version.version)}
                    <li>
                      <span class:active={version.active}>{version.version}</span>
                      <div>
                        <strong>{version.change_summary || `Version ${version.version}`}</strong>
                        <small>
                          {version.step_count} {version.step_count === 1 ? "step" : "steps"}
                          · {formatDate(version.created_at)}
                        </small>
                      </div>
                      {#if version.active}
                        <Badge tone="success">Active</Badge>
                      {:else if templateDetail.editable}
                        <Button
                          size="sm"
                          variant="quiet"
                          onclick={() => activateVersion(version.version)}
                          disabled={savingTemplate}
                        >
                          Make active
                        </Button>
                      {/if}
                    </li>
                  {/each}
                </ol>
              </section>
            {/if}
          </section>
        </div>
      {:else}
        <EmptyState
          icon="activity"
          title={query ? "No matching templates" : "No automation templates yet"}
          description={query
            ? "Try a broader phrase or clear the filter."
            : "Create a visual template to make a repeatable workflow available to Run Studio."}
        >
          {#snippet actions()}
            {#if query}
              <Button variant="outline" onclick={() => (query = "")}>Clear filter</Button>
            {:else}
              <Button onclick={() => navigate(editorRoute(), true)}>Create template</Button>
            {/if}
          {/snippet}
        </EmptyState>
      {/if}
    {:else if requestedView === "catalogue"}
      <section class="directory-toolbar" aria-label="Workflow filters">
        <SearchField
          id="automation-workflow-search"
          value={query}
          label="Filter addon workflows"
          placeholder="Search workflow, addon, output, or requirement…"
          oninput={(value) => (query = value)}
        />
        <p>
          <strong>{filteredWorkflows.length}</strong>
          {filteredWorkflows.length === 1 ? "workflow" : "workflows"}
        </p>
      </section>

      {#if dashboardLoading && !dashboard}
        <div class="directory-loading" aria-busy="true">
          <span></span><span></span><span></span>
        </div>
      {:else if dashboardError}
        <section class="route-error" role="alert">
          <Icon name="activity" size={24} />
          <h2>Addon workflows could not be loaded</h2>
          <p>{dashboardError}</p>
          <Button variant="outline" onclick={() => loadDashboard()}>Try again</Button>
        </section>
      {:else if filteredWorkflows.length && selectedWorkflow}
        <div class="library-layout">
          <aside class="template-directory" aria-label="Addon workflows">
            <header>
              <p>Addon catalogue</p>
              <span>{filteredWorkflows.length.toString().padStart(2, "0")}</span>
            </header>
            <div>
              {#each filteredWorkflows as item, index (item.id)}
                <button
                  type="button"
                  class:selected={item.id === selectedWorkflow.id}
                  aria-pressed={item.id === selectedWorkflow.id}
                  onclick={() =>
                    navigate(
                      automationRoute("catalogue", { workflow: item.id }),
                      true,
                    )}
                >
                  <span class="row-number">{String(index + 1).padStart(2, "0")}</span>
                  <span class="row-copy">
                    <span>
                      <strong>{item.name}</strong>
                      {#if item.definition_error}<i title="Definition issue">!</i>{/if}
                    </span>
                    <small>
                      {humanizeAutomation(item.addon_id)}
                      · {item.execution_supported ? `${item.steps.length} steps` : "handoff"}
                    </small>
                  </span>
                  <span
                    class:status-rule--active={item.available && !item.definition_error}
                    class:status-rule--attention={item.status.state === "degraded" || !!item.definition_error}
                    class="status-rule"
                  ></span>
                </button>
              {/each}
            </div>
          </aside>

          <section class="template-detail workflow-detail" aria-live="polite">
            <header class="detail-header">
              <div>
                <p>{humanizeAutomation(selectedWorkflow.addon_id)} / Addon workflow</p>
                <h2>{selectedWorkflow.name}</h2>
                <span>{selectedWorkflow.description || "No workflow description was declared."}</span>
              </div>
              <div>
                <Badge tone={statusTone(selectedWorkflow.status.state)}>
                  {humanizeAutomation(selectedWorkflow.status.state)}
                </Badge>
                <Badge tone={selectedWorkflow.execution_supported ? "success" : "neutral"}>
                  {selectedWorkflow.execution_supported ? "Executable" : "Workspace handoff"}
                </Badge>
              </div>
            </header>

            <div class="detail-actions">
              <Button
                variant="outline"
                onclick={() => validateWorkflow(selectedWorkflow!)}
                disabled={readinessLoading}
              >
                <Icon name="check" size={15} />
                {readinessLoading ? "Checking…" : "Check readiness"}
              </Button>
              {#if selectedWorkflow.execution_supported}
                <Button
                  onclick={() =>
                    requestRun({ kind: "workflow", item: selectedWorkflow! })}
                  disabled={!selectedWorkflow.available || !!selectedWorkflow.definition_error}
                >
                  <Icon name="activity" size={15} />
                  Create run
                </Button>
              {/if}
              {#if selectedWorkflow.launch_url}
                <a
                  class="button-link"
                  href={selectedWorkflow.launch_url}
                  onclick={(event) =>
                    openInternalLink(selectedWorkflow!.launch_url!, event)}
                >
                  Open workspace
                  <Icon name="arrow-up-right" size={14} />
                </a>
              {/if}
            </div>

            {#if selectedWorkflow.status.reasons.length || selectedWorkflow.definition_error}
              <div class="detail-warning" role="status">
                <Icon name="activity" size={17} />
                <span>
                  {selectedWorkflow.definition_error ||
                    selectedWorkflow.status.reasons.join(" ")}
                </span>
              </div>
            {/if}

            <div class="workflow-reading-grid">
              <section>
                <p class="section-eyebrow">01 / Intended work</p>
                <h3>Jobs and outputs</h3>
                {#if selectedWorkflow.expected_jobs.length}
                  <div class="record-list">
                    {#each selectedWorkflow.expected_jobs as item, index (item.id)}
                      <article>
                        <span>{String(index + 1).padStart(2, "0")}</span>
                        <div>
                          <strong>{item.label}</strong>
                          <small>{item.type ? humanizeAutomation(item.type) : "Expected job"}</small>
                        </div>
                      </article>
                    {/each}
                  </div>
                {:else}
                  <p class="muted-copy">No background jobs were declared.</p>
                {/if}
                {#if selectedWorkflow.expected_artifacts.length}
                  <h4>Expected outputs</h4>
                  <div class="chip-list">
                    {#each selectedWorkflow.expected_artifacts as item (item.id)}
                      <span>{item.label}{item.type ? ` · ${humanizeAutomation(item.type)}` : ""}</span>
                    {/each}
                  </div>
                {/if}
              </section>

              <section>
                <p class="section-eyebrow">02 / Safety boundary</p>
                <h3>Requirements and approvals</h3>
                <dl class="workflow-metrics">
                  <div>
                    <dt>Requirements</dt>
                    <dd>{automationRequirementCount(selectedWorkflow)}</dd>
                  </div>
                  <div>
                    <dt>Approval gates</dt>
                    <dd>{selectedWorkflow.approval_gates.length}</dd>
                  </div>
                  <div>
                    <dt>Dry-run contract</dt>
                    <dd>{selectedWorkflow.dry_run_supported ? "Declared" : "Not declared"}</dd>
                  </div>
                </dl>
                {#if selectedWorkflow.approval_gates.length}
                  <div class="guardrail-list">
                    {#each selectedWorkflow.approval_gates as gate (gate)}
                      <span><Icon name="shield" size={13} />{humanizeAutomation(gate)}</span>
                    {/each}
                  </div>
                {/if}
                {#each Object.entries(selectedWorkflow.requirements) as [group, values] (group)}
                  {#if values.length}
                    <div class="requirement-line">
                      <strong>{humanizeAutomation(group)}</strong>
                      <span>{values.map(humanizeAutomation).join(", ")}</span>
                    </div>
                  {/if}
                {/each}
              </section>
            </div>

            <section class="readiness-panel">
              <header>
                <div>
                  <p>Read-only validation</p>
                  <h3>Runtime readiness</h3>
                </div>
                {#if readiness}
                  <Badge tone={readiness.blocking_reasons.length ? "warning" : "success"}>
                    {readiness.blocking_reasons.length ? "Blocked" : "Ready"}
                  </Badge>
                {/if}
              </header>
              {#if readinessLoading}
                <div class="readiness-loading" aria-busy="true">
                  Checking declared addons, tools, providers, workers, and scopes…
                </div>
              {:else if readinessError}
                <div class="detail-warning" role="alert">
                  <Icon name="activity" size={16} />
                  <span>{readinessError}</span>
                </div>
              {:else if readiness}
                <p class="readiness-message">{readiness.message}</p>
                {#if readiness.blocking_reasons.length}
                  <ul class="blocking-list">
                    {#each readiness.blocking_reasons as reason (reason)}
                      <li>{reason}</li>
                    {/each}
                  </ul>
                {/if}
                {#if readiness.requirements.length}
                  <div class="readiness-groups">
                    {#each readiness.requirements as group (group.id)}
                      <article>
                        <header>
                          <strong>{group.label}</strong>
                          <span>{group.ready}/{group.total} ready</span>
                        </header>
                        <ul>
                          {#each group.items as item (item.id)}
                            <li>
                              <i class:ready={item.ready}></i>
                              <span>
                                <strong>{humanizeAutomation(item.id)}</strong>
                                <small>
                                  {item.ready
                                    ? humanizeAutomation(item.state)
                                    : item.reasons.join(" ") ||
                                      humanizeAutomation(item.state)}
                                </small>
                              </span>
                            </li>
                          {/each}
                        </ul>
                      </article>
                    {/each}
                  </div>
                {/if}
              {:else}
                <div class="readiness-empty">
                  <Icon name="shield" size={22} />
                  <p>
                    Run a read-only check to see whether every declared dependency is
                    available. It creates no run, job, approval, artifact, or log record.
                  </p>
                </div>
              {/if}
            </section>

            {#if selectedWorkflowDefinition?.steps.length}
              <WorkflowGraphPanel
                definition={selectedWorkflowDefinition}
                selectedKey={selectedStepKey}
                onselect={(key) => (selectedStepKey = key)}
                eyebrow="Declared execution"
                title="Workflow preview"
              />
            {/if}

            {#if selectedWorkflow.links.length}
              <section class="workflow-links">
                <header>
                  <p>Handoffs</p>
                  <h3>Related destinations</h3>
                </header>
                <div>
                  {#each selectedWorkflow.links as link (`${link.kind}:${link.id}`)}
                    {#if link.href}
                      <a
                        href={link.href}
                        onclick={(event) => openInternalLink(link.href!, event)}
                      >
                        <span>
                          <small>{humanizeAutomation(link.kind)}</small>
                          <strong>{link.label}</strong>
                        </span>
                        <Icon name="arrow-up-right" size={14} />
                      </a>
                    {:else}
                      <span class="link-record">
                        <span>
                          <small>{humanizeAutomation(link.kind)}</small>
                          <strong>{link.label}</strong>
                        </span>
                      </span>
                    {/if}
                  {/each}
                </div>
              </section>
            {/if}
          </section>
        </div>
      {:else}
        <EmptyState
          icon="search"
          title={query ? "No matching addon workflows" : "No addon workflows are registered"}
          description={query
            ? "Try a broader name, addon, output, or requirement."
            : "Active addons can contribute workflow recipes to this catalogue."}
        >
          {#snippet actions()}
            {#if query}
              <Button variant="outline" onclick={() => (query = "")}>Clear filter</Button>
            {/if}
          {/snippet}
        </EmptyState>
      {/if}
    {:else}
      <section class="runs-header">
        <div>
          <p>Runtime handoff</p>
          <h2>Recent automation runs</h2>
          <span>
            Templates and executable addon workflows become durable runs here,
            then open in Run Studio for graph, evidence, approvals, and artifacts.
          </span>
        </div>
        <Button variant="outline" onclick={() => loadRuns()} disabled={runsLoading}>
          <Icon name="refresh" size={14} />
          {runsLoading ? "Refreshing…" : "Refresh"}
        </Button>
      </section>

      {#if runsLoading && !runs}
        <div class="directory-loading" aria-busy="true">
          <span></span><span></span><span></span>
        </div>
      {:else if runsError}
        <section class="route-error" role="alert">
          <Icon name="activity" size={24} />
          <h2>Recent runs could not be loaded</h2>
          <p>{runsError}</p>
          <Button variant="outline" onclick={() => loadRuns()}>Try again</Button>
        </section>
      {:else if runs?.items.length}
        <div class="run-list">
          {#each runs.items as run, index (run.id)}
            <button type="button" onclick={() => openRun(run.id)}>
              <span class="run-number">{String(index + 1).padStart(2, "0")}</span>
              <header>
                <StatusMark status={run.status} label={run.status_label} compact />
                <small>{humanizeAutomation(run.kind)}</small>
              </header>
              <div>
                <h3>{run.title}</h3>
                <p>{run.summary || run.current_step_label || "No run summary yet."}</p>
              </div>
              <dl>
                <div>
                  <dt>Progress</dt>
                  <dd>{run.progress.completed}/{run.progress.total} steps</dd>
                </div>
                <div>
                  <dt>Artifacts</dt>
                  <dd>{run.artifact_count}</dd>
                </div>
                <div>
                  <dt>Updated</dt>
                  <dd>{formatDate(run.updated_at)}</dd>
                </div>
              </dl>
              <footer>
                <span>{run.current_step_label || (run.active ? "Preparing run" : "Run finished")}</span>
                <Icon name="arrow-right" size={15} />
              </footer>
            </button>
          {/each}
        </div>
      {:else}
        <EmptyState
          icon="activity"
          title="No automation runs yet"
          description="Create a run from a saved template or executable addon workflow. It will open directly in Run Studio."
        >
          {#snippet actions()}
            <Button onclick={() => changeView("templates")}>Browse templates</Button>
          {/snippet}
        </EmptyState>
      {/if}
    {/if}
  </div>
{/if}

<Dialog
  open={!!runTarget}
  title={runTarget
    ? `Create a run from ${runTarget.kind === "template" ? runTarget.item.title : runTarget.item.name}`
    : "Create automation run"}
  description="Choose whether to review the compiled plan first or start execution immediately."
  onclose={() => (runTarget = null)}
>
  <div class="run-dialog">
    <label>
      <span>Run title</span>
      <input
        type="text"
        value={runTitle}
        maxlength="255"
        oninput={(event) => (runTitle = event.currentTarget.value)}
      />
    </label>
    <label class="run-mode">
      <input
        type="checkbox"
        checked={runImmediately}
        disabled={!immediateStartAvailable}
        onchange={(event) => (runImmediately = event.currentTarget.checked)}
      />
      <span>
        <strong>Start immediately</strong>
        <small>
          {runTarget?.kind === "workflow" && !immediateStartAvailable
            ? "Check runtime readiness first, or create the run in Ready state for review."
            : "Leave this off to inspect the compiled dependency graph in Run Studio before starting."}
        </small>
      </span>
    </label>
    <p>
      {runImmediately
        ? "The workflow orchestrator will begin eligible steps after the run is created."
        : "The run will be created in Ready state. No step starts until you approve it in Run Studio."}
    </p>
  </div>
  {#snippet footer()}
    <Button variant="quiet" onclick={() => (runTarget = null)} disabled={creatingRun}>
      Cancel
    </Button>
    <Button onclick={createRun} disabled={creatingRun || !runTitle.trim()}>
      {creatingRun ? "Creating…" : runImmediately ? "Create and start" : "Create for review"}
    </Button>
  {/snippet}
</Dialog>

<Dialog
  open={!!deleteTarget}
  title={deleteTarget ? `Delete ${deleteTarget.title}?` : "Delete automation template?"}
  description="This permanently removes the reusable template and its version history. Existing runs remain available."
  size="sm"
  onclose={() => (deleteTarget = null)}
>
  <div class="delete-dialog">
    <p>
      Type <strong>{deleteTarget?.title}</strong> to confirm.
    </p>
    <label>
      <span>Template name</span>
      <input
        value={deleteConfirmation}
        autocomplete="off"
        oninput={(event) => (deleteConfirmation = event.currentTarget.value)}
      />
    </label>
  </div>
  {#snippet footer()}
    <Button variant="quiet" onclick={() => (deleteTarget = null)} disabled={deletingTemplate}>
      Cancel
    </Button>
    <Button
      variant="danger"
      onclick={deleteTemplate}
      disabled={deletingTemplate || deleteConfirmation.trim() !== deleteTarget?.title}
    >
      {deletingTemplate ? "Deleting…" : "Delete permanently"}
    </Button>
  {/snippet}
</Dialog>

<style>
  .automations-page {
    width: min(100%, 100rem);
    margin: 0 auto;
    padding: clamp(1.2rem, 3vw, 3rem);
  }

  .header-link,
  .button-link {
    min-height: 2.5rem;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 0.42rem;
    border: 1px solid var(--color-rule-strong);
    padding: 0.58rem 0.78rem;
    background: var(--color-surface);
    color: var(--color-ink);
    font-size: 0.72rem;
    font-weight: 660;
    text-decoration: none;
  }

  .header-link:hover,
  .button-link:hover {
    background: var(--color-surface-muted);
  }

  .page-feedback {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    border: 1px solid var(--color-lichen);
    margin: 1rem 0 0;
    padding: 0.7rem 0.8rem;
    background: var(--color-lichen-muted);
    color: var(--color-lichen);
    font-size: 0.7rem;
  }

  .page-feedback--danger {
    border-color: var(--color-danger);
    background: color-mix(in srgb, var(--color-danger) 7%, var(--color-surface));
    color: var(--color-danger);
  }

  .automation-summary {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    border-top: 1px solid var(--color-rule-strong);
    border-left: 1px solid var(--color-rule);
    margin-top: 1.4rem;
  }

  .automation-summary button {
    min-width: 0;
    display: grid;
    gap: 0.18rem;
    border: 0;
    border-right: 1px solid var(--color-rule);
    border-bottom: 1px solid var(--color-rule);
    padding: 0.82rem 0.9rem;
    background: color-mix(in srgb, var(--color-surface) 72%, transparent);
    color: inherit;
    cursor: pointer;
    text-align: left;
  }

  .automation-summary button:hover,
  .automation-summary button.active {
    background: var(--color-surface);
    box-shadow: inset 0 -3px var(--color-clay);
  }

  .automation-summary span,
  .automation-summary small {
    color: var(--color-ink-muted);
    font-size: 0.56rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .automation-summary strong {
    font-family: var(--font-editorial);
    font-size: 1.65rem;
    font-weight: 500;
  }

  .automation-tabs {
    display: flex;
    border-bottom: 1px solid var(--color-rule-strong);
    margin-top: 1.2rem;
  }

  .automation-tabs button {
    border: 0;
    border-right: 1px solid var(--color-rule);
    padding: 0.75rem 1rem;
    background: transparent;
    color: var(--color-ink-muted);
    cursor: pointer;
    font-size: 0.67rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .automation-tabs button:hover,
  .automation-tabs button.active {
    background: var(--color-surface-muted);
    color: var(--color-ink);
  }

  .automation-tabs button.active {
    box-shadow: inset 0 -3px var(--color-clay);
  }

  .directory-toolbar {
    display: grid;
    grid-template-columns: minmax(16rem, 32rem) auto;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 1rem 0;
  }

  .directory-toolbar p {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.62rem;
    letter-spacing: 0.09em;
    text-transform: uppercase;
  }

  .directory-toolbar strong {
    margin-right: 0.25rem;
    color: var(--color-ink);
    font-family: var(--font-editorial);
    font-size: 1rem;
    font-weight: 560;
  }

  .library-layout {
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(17rem, 0.38fr) minmax(0, 1fr);
    align-items: start;
    border-top: 1px solid var(--color-rule-strong);
  }

  .template-directory {
    min-width: 0;
    border-right: 1px solid var(--color-rule-strong);
    border-left: 1px solid var(--color-rule);
  }

  .template-directory > header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-right: 0;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.65rem 0.75rem;
  }

  .template-directory > header p,
  .template-directory > header span {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.57rem;
    font-weight: 740;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .template-directory > header span {
    color: var(--color-clay);
    font-family: var(--font-editorial);
  }

  .template-directory > div {
    max-height: calc(100vh - 21rem);
    min-height: 26rem;
    overflow: auto;
  }

  .template-directory button {
    width: 100%;
    min-width: 0;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) 3px;
    align-items: center;
    gap: 0.65rem;
    border: 0;
    border-right: 0;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.82rem 0.72rem;
    background: color-mix(in srgb, var(--color-surface) 75%, transparent);
    color: inherit;
    cursor: pointer;
    text-align: left;
  }

  .template-directory button:hover,
  .template-directory button.selected {
    background: var(--color-surface);
  }

  .template-directory button.selected {
    box-shadow: inset 3px 0 var(--color-clay);
  }

  .row-number {
    align-self: start;
    color: var(--color-clay);
    font-family: var(--font-editorial);
    font-size: 0.65rem;
  }

  .row-copy {
    min-width: 0;
    display: grid;
    gap: 0.25rem;
  }

  .row-copy > span {
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 0.35rem;
  }

  .row-copy strong {
    overflow: hidden;
    font-family: var(--font-editorial);
    font-size: 0.92rem;
    font-weight: 550;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .row-copy i {
    width: 1rem;
    height: 1rem;
    display: grid;
    flex: 0 0 auto;
    place-items: center;
    border: 1px solid var(--color-danger);
    color: var(--color-danger);
    font-size: 0.56rem;
    font-style: normal;
    font-weight: 800;
  }

  .row-copy small {
    color: var(--color-ink-muted);
    font-size: 0.55rem;
    text-transform: capitalize;
  }

  .status-rule {
    width: 3px;
    height: 2.2rem;
    background: var(--color-rule-strong);
  }

  .status-rule--active {
    background: var(--color-lichen);
  }

  .status-rule--attention {
    background: var(--color-tobacco);
  }

  .template-detail {
    min-width: 0;
    display: grid;
    gap: 1rem;
    padding: 1.1rem 0 2rem 1.1rem;
  }

  .detail-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding-bottom: 0.9rem;
  }

  .detail-header > div:first-child {
    max-width: 50rem;
  }

  .detail-header p,
  .detail-header h2,
  .detail-header span {
    margin: 0;
  }

  .detail-header p {
    color: var(--color-clay);
    font-size: 0.59rem;
    font-weight: 760;
    letter-spacing: 0.13em;
    text-transform: uppercase;
  }

  .detail-header h2 {
    margin-top: 0.22rem;
    font-family: var(--font-editorial);
    font-size: clamp(1.7rem, 3vw, 2.7rem);
    font-weight: 480;
    letter-spacing: -0.025em;
  }

  .detail-header > div:first-child > span {
    display: block;
    margin-top: 0.42rem;
    color: var(--color-ink-muted);
    font-size: 0.75rem;
    line-height: 1.55;
  }

  .detail-header > div:last-child {
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }

  .detail-actions {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.45rem;
  }

  .detail-warning {
    display: flex;
    align-items: flex-start;
    gap: 0.5rem;
    border: 1px solid var(--color-tobacco);
    padding: 0.7rem 0.75rem;
    background: var(--color-tobacco-muted);
    color: var(--color-tobacco);
    font-size: 0.68rem;
    line-height: 1.45;
  }

  .template-metrics {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    border-top: 1px solid var(--color-rule);
    border-left: 1px solid var(--color-rule);
    margin: 0;
  }

  .template-metrics div {
    border-right: 1px solid var(--color-rule);
    border-bottom: 1px solid var(--color-rule);
    padding: 0.72rem;
  }

  .template-metrics dt,
  .template-metrics dd {
    margin: 0;
  }

  .template-metrics dt {
    color: var(--color-ink-muted);
    font-size: 0.54rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .template-metrics dd {
    margin-top: 0.22rem;
    font-family: var(--font-editorial);
    font-size: 1rem;
  }

  .step-reading,
  .version-history,
  .readiness-panel,
  .workflow-links {
    border: 1px solid var(--color-rule);
    background: var(--color-surface);
  }

  .step-reading > header,
  .version-history > header,
  .readiness-panel > header,
  .workflow-links > header {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.75rem 0.82rem;
  }

  .step-reading header p,
  .step-reading header h3,
  .step-reading header span,
  .version-history header p,
  .version-history header h3,
  .version-history header span,
  .readiness-panel header p,
  .readiness-panel header h3,
  .workflow-links header p,
  .workflow-links header h3 {
    margin: 0;
  }

  .step-reading header p,
  .version-history header p,
  .readiness-panel header p,
  .workflow-links header p {
    color: var(--color-clay);
    font-size: 0.56rem;
    font-weight: 750;
    letter-spacing: 0.11em;
    text-transform: uppercase;
  }

  .step-reading header h3,
  .version-history header h3,
  .readiness-panel header h3,
  .workflow-links header h3 {
    margin-top: 0.12rem;
    font-family: var(--font-editorial);
    font-size: 1.02rem;
    font-weight: 550;
  }

  .step-reading > ol,
  .version-history > ol {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .step-reading li {
    display: grid;
    grid-template-columns: minmax(13rem, 0.42fr) minmax(0, 1fr);
    border-bottom: 1px solid var(--color-rule);
  }

  .step-reading li:last-child {
    border-bottom: 0;
  }

  .step-reading li.selected {
    box-shadow: inset 3px 0 var(--color-clay);
  }

  .step-reading li > button {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: start;
    gap: 0.65rem;
    border: 0;
    border-right: 1px solid var(--color-rule);
    padding: 0.75rem;
    background: transparent;
    color: inherit;
    cursor: pointer;
    text-align: left;
  }

  .step-reading li > button:hover {
    background: var(--color-surface-muted);
  }

  .step-reading li > button > span:first-child {
    color: var(--color-clay);
    font-family: var(--font-editorial);
    font-size: 0.65rem;
  }

  .step-reading li > button > span:last-child {
    min-width: 0;
    display: grid;
    gap: 0.16rem;
  }

  .step-reading li strong {
    font-family: var(--font-editorial);
    font-size: 0.82rem;
    font-weight: 560;
  }

  .step-reading li small,
  .step-reading li > div span {
    color: var(--color-ink-muted);
    font-size: 0.55rem;
  }

  .step-reading li > div {
    display: grid;
    align-content: center;
    gap: 0.3rem;
    padding: 0.72rem;
  }

  .step-reading li > div p {
    margin: 0;
    font-size: 0.68rem;
    line-height: 1.5;
  }

  .version-history li {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 0.7rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.65rem 0.75rem;
  }

  .version-history li:last-child {
    border-bottom: 0;
  }

  .version-history li > span:first-child {
    width: 2rem;
    height: 2rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-rule-strong);
    color: var(--color-ink-muted);
    font-family: var(--font-editorial);
    font-size: 0.72rem;
  }

  .version-history li > span:first-child.active {
    border-color: var(--color-lichen);
    background: var(--color-lichen-muted);
    color: var(--color-lichen);
  }

  .version-history li > div {
    min-width: 0;
    display: grid;
    gap: 0.14rem;
  }

  .version-history li strong {
    font-family: var(--font-editorial);
    font-size: 0.78rem;
    font-weight: 550;
  }

  .version-history li small {
    color: var(--color-ink-muted);
    font-size: 0.55rem;
  }

  .workflow-reading-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    border-top: 1px solid var(--color-rule);
    border-left: 1px solid var(--color-rule);
  }

  .workflow-reading-grid > section {
    border-right: 1px solid var(--color-rule);
    border-bottom: 1px solid var(--color-rule);
    padding: 0.9rem;
  }

  .section-eyebrow,
  .workflow-reading-grid h3,
  .workflow-reading-grid h4 {
    margin: 0;
  }

  .section-eyebrow {
    color: var(--color-clay);
    font-size: 0.55rem;
    font-weight: 750;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .workflow-reading-grid h3 {
    margin-top: 0.16rem;
    font-family: var(--font-editorial);
    font-size: 1.15rem;
    font-weight: 530;
  }

  .workflow-reading-grid h4 {
    margin-top: 1rem;
    font-family: var(--font-editorial);
    font-size: 0.82rem;
    font-weight: 550;
  }

  .record-list {
    display: grid;
    margin-top: 0.75rem;
  }

  .record-list article {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: center;
    gap: 0.65rem;
    border-top: 1px solid var(--color-rule);
    padding: 0.55rem 0;
  }

  .record-list article > span {
    color: var(--color-clay);
    font-family: var(--font-editorial);
    font-size: 0.62rem;
  }

  .record-list article > div {
    display: grid;
    gap: 0.1rem;
  }

  .record-list strong {
    font-family: var(--font-editorial);
    font-size: 0.76rem;
    font-weight: 550;
  }

  .record-list small,
  .muted-copy {
    color: var(--color-ink-muted);
    font-size: 0.58rem;
  }

  .muted-copy {
    line-height: 1.5;
  }

  .chip-list,
  .guardrail-list {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
    margin-top: 0.5rem;
  }

  .chip-list span,
  .guardrail-list span {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    border: 1px solid var(--color-rule);
    padding: 0.35rem 0.48rem;
    background: var(--color-paper);
    color: var(--color-ink-soft);
    font-size: 0.57rem;
  }

  .workflow-metrics {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    margin: 0.75rem 0 0;
    border-top: 1px solid var(--color-rule);
    border-left: 1px solid var(--color-rule);
  }

  .workflow-metrics div {
    border-right: 1px solid var(--color-rule);
    border-bottom: 1px solid var(--color-rule);
    padding: 0.55rem;
  }

  .workflow-metrics dt,
  .workflow-metrics dd {
    margin: 0;
  }

  .workflow-metrics dt {
    color: var(--color-ink-muted);
    font-size: 0.5rem;
    text-transform: uppercase;
  }

  .workflow-metrics dd {
    margin-top: 0.18rem;
    font-family: var(--font-editorial);
    font-size: 0.76rem;
  }

  .requirement-line {
    display: grid;
    grid-template-columns: minmax(6rem, 0.34fr) minmax(0, 1fr);
    gap: 0.6rem;
    border-top: 1px solid var(--color-rule);
    margin-top: 0.65rem;
    padding-top: 0.55rem;
  }

  .requirement-line strong {
    color: var(--color-ink-muted);
    font-size: 0.55rem;
  }

  .requirement-line span {
    font-size: 0.62rem;
    line-height: 1.45;
  }

  .readiness-loading,
  .readiness-message,
  .readiness-empty {
    margin: 0;
    padding: 0.85rem;
    color: var(--color-ink-muted);
    font-size: 0.68rem;
    line-height: 1.5;
  }

  .readiness-empty {
    display: grid;
    grid-template-columns: auto minmax(0, 36rem);
    align-items: center;
    justify-content: center;
    gap: 0.65rem;
    min-height: 8rem;
  }

  .readiness-empty p {
    margin: 0;
  }

  .blocking-list {
    margin: 0 0 0.75rem 1.8rem;
    color: var(--color-tobacco);
    font-size: 0.63rem;
    line-height: 1.5;
  }

  .readiness-groups {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    border-top: 1px solid var(--color-rule);
  }

  .readiness-groups > article {
    border-right: 1px solid var(--color-rule);
    border-bottom: 1px solid var(--color-rule);
  }

  .readiness-groups article > header {
    display: flex;
    justify-content: space-between;
    gap: 0.6rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.55rem 0.65rem;
  }

  .readiness-groups article > header strong {
    font-family: var(--font-editorial);
    font-size: 0.76rem;
    font-weight: 550;
  }

  .readiness-groups article > header span {
    color: var(--color-ink-muted);
    font-size: 0.53rem;
  }

  .readiness-groups ul {
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .readiness-groups li {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: start;
    gap: 0.45rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.5rem 0.65rem;
  }

  .readiness-groups li:last-child {
    border-bottom: 0;
  }

  .readiness-groups li i {
    width: 0.5rem;
    height: 0.5rem;
    margin-top: 0.22rem;
    background: var(--color-tobacco);
  }

  .readiness-groups li i.ready {
    background: var(--color-lichen);
  }

  .readiness-groups li span {
    display: grid;
    gap: 0.1rem;
  }

  .readiness-groups li strong {
    font-size: 0.61rem;
  }

  .readiness-groups li small {
    color: var(--color-ink-muted);
    font-size: 0.54rem;
    line-height: 1.35;
  }

  .workflow-links > div {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .workflow-links a,
  .link-record {
    min-width: 0;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.7rem;
    border-right: 1px solid var(--color-rule);
    border-bottom: 1px solid var(--color-rule);
    padding: 0.65rem 0.75rem;
    color: inherit;
    text-decoration: none;
  }

  .workflow-links a:hover {
    background: var(--color-surface-muted);
  }

  .workflow-links a > span,
  .link-record > span {
    min-width: 0;
    display: grid;
    gap: 0.1rem;
  }

  .workflow-links small {
    color: var(--color-ink-muted);
    font-size: 0.5rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .workflow-links strong {
    overflow: hidden;
    font-family: var(--font-editorial);
    font-size: 0.75rem;
    font-weight: 550;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .runs-header {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule-strong);
    padding: 1.3rem 0 1rem;
  }

  .runs-header > div {
    max-width: 47rem;
  }

  .runs-header p,
  .runs-header h2,
  .runs-header span {
    margin: 0;
  }

  .runs-header p {
    color: var(--color-clay);
    font-size: 0.58rem;
    font-weight: 750;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .runs-header h2 {
    margin-top: 0.18rem;
    font-family: var(--font-editorial);
    font-size: clamp(1.6rem, 3vw, 2.5rem);
    font-weight: 500;
  }

  .runs-header span {
    display: block;
    margin-top: 0.4rem;
    color: var(--color-ink-muted);
    font-size: 0.7rem;
    line-height: 1.5;
  }

  .run-list {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    border-left: 1px solid var(--color-rule);
  }

  .run-list > button {
    position: relative;
    min-width: 0;
    min-height: 16rem;
    display: grid;
    grid-template-rows: auto minmax(0, 1fr) auto auto;
    gap: 0.85rem;
    border: 0;
    border-right: 1px solid var(--color-rule);
    border-bottom: 1px solid var(--color-rule);
    padding: 1rem;
    background: color-mix(in srgb, var(--color-surface) 76%, transparent);
    color: inherit;
    cursor: pointer;
    text-align: left;
  }

  .run-list > button:hover {
    background: var(--color-surface);
    box-shadow: inset 0 -3px var(--color-clay);
  }

  .run-number {
    position: absolute;
    top: 0.55rem;
    right: 0.65rem;
    color: var(--color-rule-strong);
    font-family: var(--font-editorial);
    font-size: 0.6rem;
  }

  .run-list header,
  .run-list footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.6rem;
  }

  .run-list header small,
  .run-list footer {
    color: var(--color-ink-muted);
    font-size: 0.56rem;
  }

  .run-list h3,
  .run-list p {
    margin: 0;
  }

  .run-list h3 {
    font-family: var(--font-editorial);
    font-size: 1.25rem;
    font-weight: 520;
  }

  .run-list p {
    margin-top: 0.35rem;
    color: var(--color-ink-muted);
    font-size: 0.68rem;
    line-height: 1.5;
  }

  .run-list dl {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    margin: 0;
    border-top: 1px solid var(--color-rule);
  }

  .run-list dl div {
    min-width: 0;
    border-right: 1px solid var(--color-rule);
    padding: 0.5rem;
  }

  .run-list dt,
  .run-list dd {
    margin: 0;
  }

  .run-list dt {
    color: var(--color-ink-muted);
    font-size: 0.49rem;
    text-transform: uppercase;
  }

  .run-list dd {
    overflow: hidden;
    margin-top: 0.14rem;
    font-family: var(--font-editorial);
    font-size: 0.66rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .directory-loading,
  .detail-loading,
  .editor-loading {
    display: grid;
    place-content: center;
    justify-items: center;
    gap: 0.65rem;
    min-height: 26rem;
  }

  .directory-loading {
    grid-template-columns: repeat(3, minmax(5rem, 12rem));
  }

  .directory-loading span,
  .detail-loading span,
  .editor-loading span {
    height: 7rem;
    border: 1px solid var(--color-rule);
    background: var(--color-surface-muted);
    animation: automation-pulse 1.2s ease-in-out infinite;
  }

  .detail-loading,
  .editor-loading {
    grid-template-columns: minmax(10rem, 30rem);
  }

  .detail-loading span,
  .editor-loading span {
    width: 100%;
    height: 3rem;
  }

  .directory-loading span:nth-child(2),
  .detail-loading span:nth-child(2),
  .editor-loading span:nth-child(2) {
    animation-delay: 130ms;
  }

  .directory-loading span:nth-child(3) {
    animation-delay: 260ms;
  }

  .detail-loading p,
  .editor-loading p {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.68rem;
  }

  .route-error,
  .detail-error {
    min-height: 20rem;
    display: grid;
    place-content: center;
    justify-items: center;
    gap: 0.5rem;
    padding: 2rem;
    text-align: center;
  }

  .route-error h1,
  .route-error h2,
  .detail-error h2 {
    margin: 0;
    font-family: var(--font-editorial);
    font-weight: 530;
  }

  .route-error p,
  .detail-error p {
    max-width: 36rem;
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.7rem;
    line-height: 1.5;
  }

  .run-dialog,
  .delete-dialog {
    display: grid;
    gap: 1rem;
  }

  .run-dialog > label:first-child,
  .delete-dialog label {
    display: grid;
    gap: 0.35rem;
  }

  .run-dialog label > span:first-child,
  .delete-dialog label > span {
    color: var(--color-ink-muted);
    font-size: 0.59rem;
    font-weight: 720;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .run-dialog input[type="text"],
  .run-dialog > label:first-child input,
  .delete-dialog input {
    width: 100%;
    border: 1px solid var(--color-rule-strong);
    border-radius: 0;
    padding: 0.68rem 0.72rem;
    background: var(--color-surface);
    color: var(--color-ink);
    font: inherit;
  }

  .run-mode {
    display: flex;
    align-items: flex-start;
    gap: 0.65rem;
    border: 1px solid var(--color-rule);
    padding: 0.75rem;
    background: var(--color-paper);
  }

  .run-mode input {
    margin-top: 0.15rem;
    accent-color: var(--color-lichen);
  }

  .run-mode span {
    display: grid;
    gap: 0.18rem;
  }

  .run-mode strong {
    font-family: var(--font-editorial);
    font-size: 0.86rem;
    font-weight: 550;
  }

  .run-mode small,
  .run-dialog > p,
  .delete-dialog p {
    color: var(--color-ink-muted);
    font-size: 0.65rem;
    line-height: 1.45;
  }

  .run-dialog > p,
  .delete-dialog p {
    margin: 0;
  }

  .delete-dialog p strong {
    color: var(--color-ink);
  }

  @keyframes automation-pulse {
    50% {
      background: var(--color-clay-muted);
    }
  }

  @media (max-width: 72rem) {
    .library-layout {
      grid-template-columns: minmax(15rem, 0.38fr) minmax(0, 1fr);
    }

    .workflow-reading-grid,
    .readiness-groups {
      grid-template-columns: 1fr;
    }
  }

  @media (max-width: 56rem) {
    .automation-summary {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .library-layout {
      grid-template-columns: 1fr;
    }

    .template-directory {
      border-right: 1px solid var(--color-rule);
    }

    .template-directory > div {
      max-height: 18rem;
      min-height: 0;
    }

    .template-detail {
      padding-left: 0;
    }

    .run-list {
      grid-template-columns: 1fr;
    }
  }

  @media (max-width: 38rem) {
    .automations-page {
      padding-inline: 0.85rem;
    }

    .directory-toolbar {
      grid-template-columns: 1fr;
      justify-items: stretch;
    }

    .automation-tabs {
      overflow-x: auto;
    }

    .automation-tabs button {
      flex: 0 0 auto;
    }

    .detail-header,
    .runs-header {
      align-items: flex-start;
      flex-direction: column;
    }

    .detail-header > div:last-child {
      flex-wrap: wrap;
    }

    .template-metrics,
    .workflow-metrics {
      grid-template-columns: 1fr;
    }

    .step-reading li {
      grid-template-columns: 1fr;
    }

    .step-reading li > button {
      border-right: 0;
      border-bottom: 1px solid var(--color-rule);
    }

    .workflow-links > div {
      grid-template-columns: 1fr;
    }

    .version-history li {
      grid-template-columns: auto minmax(0, 1fr);
    }

    .version-history li > :global(.ui-button),
    .version-history li > :global(.ui-badge) {
      grid-column: 2;
      justify-self: start;
    }
  }
</style>
