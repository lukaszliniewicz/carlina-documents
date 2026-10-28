<script lang="ts">
  import { onDestroy, type Component } from "svelte";
  import type {
    FrontendApiClient,
    FrontendArtifactDetail,
    FrontendArtifactSummary,
    FrontendBootstrap,
    FrontendRunApproval,
    FrontendRunControl,
    FrontendRunDirectory,
    FrontendRunInteraction,
    FrontendRunStepDetail,
    FrontendRunStudio,
  } from "@carlina/sdk";
  import StatusMark from "@carlina/runtime-ui/status-mark";
  import { Button, Icon } from "@carlina/ui";

  import RunArtifactsView from "../components/run/RunArtifactsView.svelte";
  import RunBrief from "../components/run/RunBrief.svelte";
  import RunSourcesView from "../components/run/RunSourcesView.svelte";
  import RunStepInspector from "../components/run/RunStepInspector.svelte";
  import RunTimeline from "../components/run/RunTimeline.svelte";
  import {
    formatRunCost,
    formatRunDate,
    formatRunDuration,
    runIdFromRoute,
    runStudioRoute,
    runTabFromRoute,
    selectedStepFromRoute,
    type RunStudioTab,
  } from "../lib/run-studio.ts";

  interface GraphProps {
    graph: FrontendRunStudio["graph"];
    selectedId: string | null;
    onselect: (stepId: string) => void;
  }

  interface Props {
    api: FrontendApiClient;
    bootstrap: FrontendBootstrap;
    route: string;
    navigate: (href: string, clientRoute?: boolean) => void;
  }

  let { api, bootstrap, route, navigate }: Props = $props();

  const runId = $derived(runIdFromRoute(route));
  const requestedTab = $derived(runTabFromRoute(route));
  const requestedStepId = $derived(selectedStepFromRoute(route));

  let directory = $state<FrontendRunDirectory | null>(null);
  let directoryLoading = $state(false);
  let directoryError = $state("");
  let studio = $state<FrontendRunStudio | null>(null);
  let studioLoading = $state(false);
  let studioError = $state("");
  let selectedStepId = $state<string | null>(null);
  let stepDetail = $state<FrontendRunStepDetail | null>(null);
  let stepLoading = $state(false);
  let stepError = $state("");
  let artifactDetail = $state<FrontendArtifactDetail | null>(null);
  let artifactLoading = $state(false);
  let artifactError = $state("");
  let developerData = $state<unknown>(null);
  let developerLoading = $state(false);
  let developerError = $state("");
  let busyId = $state<string | null>(null);
  let feedback = $state("");
  let feedbackTone = $state<"success" | "danger">("success");
  let Graph = $state<Component<GraphProps> | null>(null);
  let graphLoadError = $state("");
  let refreshTimer: number | null = null;
  let directoryController: AbortController | null = null;
  let studioController: AbortController | null = null;
  let stepController: AbortController | null = null;
  let artifactController: AbortController | null = null;
  let developerController: AbortController | null = null;

  const tab = $derived(requestedTab);
  const showStepInspector = $derived(
    tab === "graph" || tab === "timeline" || tab === "steps",
  );

  const tabs = [
    { id: "graph", label: "Graph" },
    { id: "timeline", label: "Timeline" },
    { id: "steps", label: "Steps" },
    { id: "artifacts", label: "Artifacts" },
    { id: "sources", label: "Sources" },
    { id: "developer", label: "Developer" },
  ] satisfies Array<{ id: RunStudioTab; label: string }>;

  async function loadDirectory() {
    directoryController?.abort();
    const controller = new AbortController();
    directoryController = controller;
    directoryLoading = true;
    directoryError = "";
    try {
      directory = await api.request<FrontendRunDirectory>(
        "/api/frontend/runs?limit=80",
        { signal: controller.signal },
      );
    } catch (reason) {
      if (controller.signal.aborted) return;
      directoryError =
        reason instanceof Error ? reason.message : "Runs could not be loaded.";
    } finally {
      if (directoryController === controller) {
        directoryController = null;
        directoryLoading = false;
      }
    }
  }

  async function loadStudio(id: string, silent = false) {
    studioController?.abort();
    const controller = new AbortController();
    studioController = controller;
    if (!silent) studioLoading = true;
    if (!silent) studioError = "";
    try {
      const payload = await api.request<FrontendRunStudio>(
        `/api/frontend/runs/${encodeURIComponent(id)}/studio`,
        { signal: controller.signal },
      );
      studio = payload;
      const requested =
        requestedStepId &&
        payload.graph.nodes.some((node) => node.id === requestedStepId)
          ? requestedStepId
          : null;
      const nextSelected =
        requested ??
        (selectedStepId &&
        payload.graph.nodes.some((node) => node.id === selectedStepId)
          ? selectedStepId
          : payload.selected_step_id);
      if (nextSelected !== selectedStepId) selectedStepId = nextSelected;
      if (
        stepDetail &&
        nextSelected &&
        stepDetail.step.id === nextSelected &&
        stepDetail.step.updated_at !==
          payload.graph.nodes.find((node) => node.id === nextSelected)?.updated_at
      ) {
        void loadStep(id, nextSelected, true);
      }
    } catch (reason) {
      if (controller.signal.aborted) return;
      if (!silent) {
        studioError =
          reason instanceof Error ? reason.message : "Run Studio could not open.";
      }
    } finally {
      if (studioController === controller) {
        studioController = null;
        if (!silent) studioLoading = false;
      }
    }
  }

  async function loadStep(id: string, stepId: string, silent = false) {
    stepController?.abort();
    const controller = new AbortController();
    stepController = controller;
    if (!silent) stepLoading = true;
    if (!silent) stepError = "";
    try {
      stepDetail = await api.request<FrontendRunStepDetail>(
        `/api/frontend/runs/${encodeURIComponent(id)}/steps/${encodeURIComponent(stepId)}`,
        { signal: controller.signal },
      );
    } catch (reason) {
      if (controller.signal.aborted) return;
      if (!silent) {
        stepError =
          reason instanceof Error ? reason.message : "Step detail could not be loaded.";
      }
    } finally {
      if (stepController === controller) {
        stepController = null;
        if (!silent) stepLoading = false;
      }
    }
  }

  async function loadArtifact(artifact: FrontendArtifactSummary) {
    if (!runId) return;
    artifactController?.abort();
    const controller = new AbortController();
    artifactController = controller;
    artifactLoading = true;
    artifactError = "";
    artifactDetail = null;
    try {
      artifactDetail = await api.request<FrontendArtifactDetail>(
        `/api/frontend/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifact.id)}`,
        { signal: controller.signal },
      );
    } catch (reason) {
      if (controller.signal.aborted) return;
      artifactError =
        reason instanceof Error ? reason.message : "Artifact could not be opened.";
    } finally {
      if (artifactController === controller) {
        artifactController = null;
        artifactLoading = false;
      }
    }
  }

  async function loadDeveloper() {
    if (!studio || developerLoading || developerData) return;
    developerController?.abort();
    const controller = new AbortController();
    developerController = controller;
    developerLoading = true;
    developerError = "";
    try {
      developerData = await api.request<unknown>(studio.developer_url, {
        signal: controller.signal,
      });
    } catch (reason) {
      if (controller.signal.aborted) return;
      developerError =
        reason instanceof Error
          ? reason.message
          : "Developer data could not be loaded.";
    } finally {
      if (developerController === controller) {
        developerController = null;
        developerLoading = false;
      }
    }
  }

  function setFeedback(message: string, tone: "success" | "danger" = "success") {
    feedback = message;
    feedbackTone = tone;
    window.setTimeout(() => {
      if (feedback === message) feedback = "";
    }, 5_000);
  }

  async function runAction(control: FrontendRunControl) {
    if (!runId || busyId) return;
    busyId = control.id;
    try {
      await api.request(control.url, { method: control.method });
      setFeedback(`${control.label} request accepted.`);
      await loadStudio(runId, true);
      if (selectedStepId) await loadStep(runId, selectedStepId, true);
    } catch (reason) {
      setFeedback(
        reason instanceof Error ? reason.message : `${control.label} failed.`,
        "danger",
      );
    } finally {
      busyId = null;
    }
  }

  async function approvalAction(
    approval: FrontendRunApproval,
    action: "approved" | "rejected",
  ) {
    if (!runId || busyId) return;
    busyId = approval.id;
    try {
      await api.request(approval.resolve_url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: action }),
      });
      setFeedback(`Approval ${action}.`);
      await loadStudio(runId, true);
    } catch (reason) {
      setFeedback(
        reason instanceof Error ? reason.message : "Approval could not be resolved.",
        "danger",
      );
    } finally {
      busyId = null;
    }
  }

  async function interactionAction(
    interaction: FrontendRunInteraction,
    action: "approved" | "rejected",
  ) {
    if (!runId || busyId) return;
    busyId = interaction.id;
    try {
      await api.request(interaction.resolve_url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_version: interaction.version,
          response: { decision: action },
        }),
      });
      setFeedback(action === "approved" ? "Interaction confirmed." : "Interaction declined.");
      await loadStudio(runId, true);
    } catch (reason) {
      setFeedback(
        reason instanceof Error ? reason.message : "Interaction could not be resolved.",
        "danger",
      );
    } finally {
      busyId = null;
    }
  }

  async function rerunStep(detail: FrontendRunStepDetail) {
    if (!runId || busyId) return;
    busyId = detail.step.id;
    try {
      await api.request(detail.step.rerun_url, { method: "POST" });
      setFeedback(`${detail.step.title} was queued to rerun.`);
      await Promise.all([
        loadStudio(runId, true),
        loadStep(runId, detail.step.id, true),
      ]);
    } catch (reason) {
      setFeedback(
        reason instanceof Error ? reason.message : "The step could not be rerun.",
        "danger",
      );
    } finally {
      busyId = null;
    }
  }

  function selectRun(id: string) {
    navigate(`/runs/${encodeURIComponent(id)}`, true);
  }

  function selectTab(nextTab: RunStudioTab) {
    if (!runId) return;
    navigate(runStudioRoute(runId, nextTab, selectedStepId), true);
  }

  function selectStep(stepId: string) {
    if (!runId) return;
    selectedStepId = stepId;
    navigate(runStudioRoute(runId, tab, stepId), true);
  }

  function openDeveloperForStep() {
    if (!runId) return;
    developerData = null;
    navigate(runStudioRoute(runId, "developer", selectedStepId), true);
  }

  $effect(() => {
    const id = runId;
    directoryController?.abort();
    stepController?.abort();
    artifactController?.abort();
    developerController?.abort();
    studio = null;
    stepDetail = null;
    artifactDetail = null;
    developerData = null;
    selectedStepId = null;
    if (id) {
      void loadStudio(id);
    } else {
      void loadDirectory();
    }
  });

  $effect(() => {
    const id = runId;
    const routeStep = requestedStepId;
    const fallback = studio?.selected_step_id ?? null;
    const next = routeStep ?? selectedStepId ?? fallback;
    if (id && next) {
      if (selectedStepId !== next) selectedStepId = next;
      if (stepDetail?.step.id !== next) void loadStep(id, next);
    }
  });

  $effect(() => {
    if (tab !== "graph" || Graph || graphLoadError) return;
    let current = true;
    void import("@carlina/runtime-ui/graph")
      .then((module) => {
        if (current) Graph = module.default;
      })
      .catch((reason: unknown) => {
        if (current) {
          graphLoadError =
            reason instanceof Error
              ? reason.message
              : "The graph renderer could not be loaded.";
        }
      });
    return () => {
      current = false;
    };
  });

  $effect(() => {
    if (tab === "developer") void loadDeveloper();
  });

  $effect(() => {
    if (refreshTimer !== null) {
      window.clearInterval(refreshTimer);
      refreshTimer = null;
    }
    const id = runId;
    const active =
      studio && !["completed", "failed", "cancelled"].includes(studio.run.status);
    if (id && active) {
      refreshTimer = window.setInterval(() => {
        if (!document.hidden) void loadStudio(id, true);
      }, 4_000);
    }
    return () => {
      if (refreshTimer !== null) {
        window.clearInterval(refreshTimer);
        refreshTimer = null;
      }
    };
  });

  onDestroy(() => {
    if (refreshTimer !== null) window.clearInterval(refreshTimer);
    directoryController?.abort();
    studioController?.abort();
    stepController?.abort();
    artifactController?.abort();
    developerController?.abort();
  });
</script>

<svelte:head>
  <meta
    name="description"
    content="Review workflow plans, dependencies, runtime activity, and artifacts."
  />
</svelte:head>

{#if !runId}
  <section class="run-directory">
    <header class="directory-header">
      <div>
        <p>Runtime / Run Studio</p>
        <h1>Runs, without the machinery showing.</h1>
        <span>
          Review plans, follow active work, and open durable outputs from one place.
        </span>
      </div>
      <div class="directory-actions">
        <Button variant="quiet" onclick={() => navigate("/approvals", true)}>
          Approval queue
        </Button>
        {#if bootstrap.user.is_admin}
          <Button
            variant="quiet"
            onclick={() => navigate("/runtime/processes", true)}
          >
            Operations
          </Button>
        {/if}
        <Button
          variant="outline"
          onclick={() => void loadDirectory()}
          disabled={directoryLoading}
        >
          <Icon name="refresh" size={15} />
          Refresh
        </Button>
      </div>
    </header>

    {#if directoryLoading && !directory}
      <div class="directory-loading" aria-busy="true">
        <span></span><span></span><span></span>
      </div>
    {:else if directoryError}
      <div class="route-error" role="alert">
        <Icon name="activity" size={24} />
        <h2>Runs could not be loaded</h2>
        <p>{directoryError}</p>
        <Button variant="outline" onclick={() => void loadDirectory()}>Try again</Button>
      </div>
    {:else if directory?.items.length}
      <div class="directory-summary">
        <span>{directory.total} total runs</span>
        <span>{directory.items.filter((run) => run.active).length} active in this view</span>
      </div>
      <div class="run-cards">
        {#each directory.items as run (run.id)}
          <button type="button" onclick={() => selectRun(run.id)}>
            <header>
              <StatusMark status={run.status} label={run.status_label} />
              <span>{run.kind.replace(/[_-]+/g, " ")}</span>
            </header>
            <div>
              <h2>{run.title}</h2>
              <p>{run.summary}</p>
            </div>
            <dl>
              <div>
                <dt>Progress</dt>
                <dd>{run.progress.completed} / {run.progress.total} steps</dd>
              </div>
              <div>
                <dt>Artifacts</dt>
                <dd>{run.artifact_count}</dd>
              </div>
              <div>
                <dt>Updated</dt>
                <dd>{formatRunDate(run.updated_at)}</dd>
              </div>
            </dl>
            <footer>
              <span>{run.current_step_label || "No active step"}</span>
              <Icon name="arrow-right" size={16} />
            </footer>
          </button>
        {/each}
      </div>
    {:else}
      <div class="route-error">
        <Icon name="activity" size={24} />
        <h2>No runs yet</h2>
        <p>Compiled and ad hoc workflow runs will appear here.</p>
      </div>
    {/if}
  </section>
{:else if studioLoading && !studio}
  <section class="studio-loading" aria-busy="true" aria-label="Loading Run Studio">
    <span class="studio-loading__title"></span>
    <span class="studio-loading__summary"></span>
    <div><span></span><span></span></div>
  </section>
{:else if studioError || !studio}
  <section class="route-error" role="alert">
    <Icon name="activity" size={25} />
    <h1>Run Studio could not open</h1>
    <p>{studioError || "The requested run was not found."}</p>
    <div>
      <Button variant="outline" onclick={() => navigate("/runs", true)}>
        Back to runs
      </Button>
    </div>
  </section>
{:else}
  <article class="run-studio">
    <header class="run-header">
      <button class="back-link" type="button" onclick={() => navigate("/runs", true)}>
        <Icon name="arrow-left" size={14} /> All runs
      </button>
      <div class="run-heading">
        <div>
          <p>Run Studio / {studio.run.kind.replace(/[_-]+/g, " ")}</p>
          <h1>{studio.run.title}</h1>
          <span>{studio.run.summary}</span>
        </div>
        <StatusMark status={studio.run.status} label={studio.run.status_label} />
      </div>

      <div class="run-metrics">
        <div class="progress-metric">
          <span>
            <strong>{studio.run.progress.percent}%</strong>
            <small>
              {studio.run.progress.completed} of {studio.run.progress.total} steps
            </small>
          </span>
          <div
            role="progressbar"
            aria-label="Run progress"
            aria-valuenow={studio.run.progress.percent}
            aria-valuemin="0"
            aria-valuemax="100"
          >
            <i style={`width: ${studio.run.progress.percent}%`}></i>
          </div>
        </div>
        <dl>
          <div>
            <dt>Duration</dt>
            <dd>{formatRunDuration(studio.run.duration_ms)}</dd>
          </div>
          <div>
            <dt>Model cost</dt>
            <dd>{formatRunCost(studio.run.cost.amount, studio.run.cost.currency)}</dd>
          </div>
          <div>
            <dt>Tokens</dt>
            <dd>{studio.run.cost.total_tokens.toLocaleString()}</dd>
          </div>
          <div>
            <dt>Updated</dt>
            <dd>{formatRunDate(studio.run.updated_at)}</dd>
          </div>
        </dl>
        <nav class="run-actions" aria-label="Run controls">
          {#each studio.run.controls as control (control.id)}
            <Button
              variant={control.tone === "danger" ? "danger" : control.tone === "primary" ? "primary" : "outline"}
              size="sm"
              onclick={() => void runAction(control)}
              disabled={busyId !== null}
            >
              {busyId === control.id ? "Working…" : control.label}
            </Button>
          {/each}
          <Button
            variant="quiet"
            size="sm"
            onclick={() => void loadStudio(runId, true)}
            disabled={studioLoading}
          >
            <Icon name="refresh" size={14} /> Refresh
          </Button>
        </nav>
      </div>
    </header>

    {#if feedback}
      <div
        class="action-feedback"
        class:action-feedback--danger={feedbackTone === "danger"}
        role="status"
        aria-live="polite"
      >
        <Icon name={feedbackTone === "danger" ? "activity" : "check"} size={16} />
        {feedback}
      </div>
    {/if}

    <div class="studio-body">
      <RunBrief
        documents={studio.documents}
        approvals={studio.approvals}
        interactions={studio.interactions}
        open={["draft", "ready", "waiting_for_approval"].includes(studio.run.status)}
        {busyId}
        onapproval={(approval, action) => void approvalAction(approval, action)}
        oninteraction={(interaction, action) => void interactionAction(interaction, action)}
      />

      <nav class="studio-tabs" aria-label="Run Studio views">
        {#each tabs as item (item.id)}
          <button
            id={`run-tab-${item.id}`}
            type="button"
            class:active={tab === item.id}
            aria-current={tab === item.id ? "page" : undefined}
            onclick={() => selectTab(item.id)}
          >
            {item.label}
            {#if item.id === "steps"}<span>{studio.run.counts.steps}</span>
            {:else if item.id === "artifacts"}<span>{studio.run.counts.artifacts}</span>
            {:else if item.id === "sources"}<span>{studio.run.counts.sources}</span>
            {/if}
          </button>
        {/each}
      </nav>

      <div class:studio-workspace={showStepInspector} class:studio-workspace--wide={!showStepInspector}>
        <section class="studio-main" aria-labelledby={`run-tab-${tab}`}>
          {#if tab === "graph"}
            {#if Graph}
              <Graph
                graph={studio.graph}
                selectedId={selectedStepId}
                onselect={selectStep}
              />
            {:else if graphLoadError}
              <div class="route-error" role="alert">
                <h2>Graph unavailable</h2>
                <p>{graphLoadError}</p>
                <Button variant="outline" onclick={() => selectTab("steps")}>
                  Open the step list
                </Button>
              </div>
            {:else}
              <div class="graph-loading" aria-busy="true">
                Loading the interactive graph…
              </div>
            {/if}
          {:else if tab === "timeline" || tab === "steps"}
            <RunTimeline
              steps={studio.timeline}
              selectedId={selectedStepId}
              mode={tab}
              onselect={selectStep}
            />
          {:else if tab === "artifacts"}
            <RunArtifactsView
              {api}
              artifacts={studio.artifacts}
              detail={artifactDetail}
              loading={artifactLoading}
              error={artifactError}
              onselect={(artifact) => void loadArtifact(artifact)}
              onclose={() => {
                artifactDetail = null;
                artifactError = "";
              }}
            />
          {:else if tab === "sources"}
            <RunSourcesView sources={studio.sources} evidence={studio.evidence} />
          {:else}
            <section class="developer-view">
              <header>
                <div>
                  <small>Explicit technical view</small>
                  <h2>Developer data</h2>
                  <p>
                    Raw run, step, and job records live only here. Credential-shaped
                    fields remain redacted.
                  </p>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  onclick={() => {
                    developerData = null;
                    void loadDeveloper();
                  }}
                  disabled={developerLoading}
                >
                  <Icon name="refresh" size={14} /> Reload
                </Button>
              </header>
              {#if developerLoading}
                <div class="developer-loading" aria-busy="true">Loading developer data…</div>
              {:else if developerError}
                <p class="developer-error" role="alert">{developerError}</p>
              {:else}
                <pre>{JSON.stringify(developerData, null, 2)}</pre>
              {/if}
            </section>
          {/if}
        </section>

        {#if showStepInspector}
          <RunStepInspector
            detail={stepDetail}
            loading={stepLoading}
            error={stepError}
            busy={Boolean(busyId)}
            onrerun={(detail) => void rerunStep(detail)}
            onartifact={(artifact) => {
              selectTab("artifacts");
              void loadArtifact(artifact);
            }}
            ondeveloper={openDeveloperForStep}
          />
        {/if}
      </div>
    </div>
  </article>
{/if}

<style>
  .run-directory,
  .run-studio {
    min-height: calc(100vh - var(--shell-header-height));
  }

  .run-directory {
    padding: clamp(1.2rem, 3.5vw, 3rem);
  }

  .directory-header {
    display: flex;
    align-items: end;
    justify-content: space-between;
    gap: 2rem;
    border-bottom: 1px solid var(--color-rule-strong);
    padding-bottom: 1.4rem;
  }

  .directory-header > div {
    max-width: 50rem;
  }

  .directory-actions {
    display: flex;
    flex: 0 0 auto;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 0.4rem;
  }

  .directory-header p,
  .run-heading p {
    margin: 0 0 0.55rem;
    color: var(--color-clay);
    font-size: 0.63rem;
    font-weight: 780;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .directory-header h1,
  .run-heading h1 {
    margin: 0;
    font-family: var(--font-editorial);
    font-weight: 480;
    letter-spacing: -0.025em;
  }

  .directory-header h1 {
    max-width: 42rem;
    font-size: clamp(2.15rem, 5vw, 4.6rem);
    line-height: 0.98;
  }

  .directory-header span,
  .run-heading > div > span {
    display: block;
    max-width: 43rem;
    margin-top: 0.8rem;
    color: var(--color-ink-muted);
    font-size: 0.76rem;
    line-height: 1.6;
  }

  .directory-summary {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.75rem 0;
    color: var(--color-ink-muted);
    font-size: 0.62rem;
  }

  .run-cards {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(20rem, 1fr));
    gap: 1px;
    border: 1px solid var(--color-rule);
    background: var(--color-rule);
  }

  .run-cards > button {
    min-width: 0;
    min-height: 18rem;
    display: grid;
    grid-template-rows: auto minmax(0, 1fr) auto auto;
    gap: 1rem;
    border: 0;
    padding: 1rem;
    background: var(--color-surface);
    color: inherit;
    cursor: pointer;
    text-align: left;
  }

  .run-cards > button:hover {
    background: var(--color-surface-muted);
  }

  .run-cards header,
  .run-cards footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.7rem;
  }

  .run-cards header > span:last-child {
    color: var(--color-ink-muted);
    font-size: 0.58rem;
    text-transform: capitalize;
  }

  .run-cards h2 {
    margin: 0;
    font-family: var(--font-editorial);
    font-size: 1.4rem;
    font-weight: 530;
    line-height: 1.1;
  }

  .run-cards p {
    margin: 0.5rem 0 0;
    color: var(--color-ink-muted);
    font-size: 0.69rem;
    line-height: 1.55;
  }

  .run-cards dl {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 1px;
    margin: 0;
    background: var(--color-rule);
  }

  .run-cards dl div {
    min-width: 0;
    display: grid;
    gap: 0.2rem;
    padding: 0.55rem;
    background: var(--color-surface-muted);
  }

  dt {
    color: var(--color-ink-muted);
    font-size: 0.53rem;
    text-transform: uppercase;
  }

  dd {
    overflow: hidden;
    margin: 0;
    color: var(--color-ink-soft);
    font-size: 0.59rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .run-cards footer {
    border-top: 1px solid var(--color-rule);
    padding-top: 0.8rem;
    color: var(--color-clay);
    font-size: 0.62rem;
  }

  .run-header {
    display: grid;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule-strong);
    padding: 1.15rem clamp(1rem, 3vw, 2.4rem) 1.35rem;
    background:
      linear-gradient(
        90deg,
        color-mix(in srgb, var(--color-clay-muted) 34%, transparent),
        transparent 45%
      ),
      var(--color-paper);
  }

  .back-link {
    justify-self: start;
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    border: 0;
    padding: 0;
    background: transparent;
    color: var(--color-ink-muted);
    cursor: pointer;
    font-size: 0.62rem;
  }

  .run-heading {
    display: flex;
    align-items: start;
    justify-content: space-between;
    gap: 1.5rem;
  }

  .run-heading > div {
    min-width: 0;
  }

  .run-heading h1 {
    font-size: clamp(2rem, 4.5vw, 4rem);
    line-height: 0.98;
  }

  .run-metrics {
    display: grid;
    grid-template-columns: minmax(12rem, 0.8fr) minmax(24rem, 1.4fr) auto;
    align-items: end;
    gap: 1rem;
  }

  .progress-metric {
    display: grid;
    gap: 0.48rem;
  }

  .progress-metric > span {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 0.6rem;
  }

  .progress-metric strong {
    font-family: var(--font-editorial);
    font-size: 1.25rem;
    font-weight: 540;
  }

  .progress-metric small {
    color: var(--color-ink-muted);
    font-size: 0.59rem;
  }

  .progress-metric > div {
    height: 3px;
    background: var(--color-surface-sunken);
  }

  .progress-metric i {
    display: block;
    height: 100%;
    background: var(--color-lichen);
  }

  .run-metrics > dl {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 1px;
    margin: 0;
    background: var(--color-rule);
  }

  .run-metrics > dl div {
    min-width: 0;
    display: grid;
    gap: 0.2rem;
    padding: 0.52rem 0.6rem;
    background: color-mix(in srgb, var(--color-surface) 68%, transparent);
  }

  .run-actions {
    display: flex;
    justify-content: flex-end;
    gap: 0.4rem;
  }

  .action-feedback {
    position: sticky;
    z-index: 25;
    top: var(--shell-header-height);
    display: flex;
    align-items: center;
    gap: 0.55rem;
    border-bottom: 1px solid var(--color-lichen);
    padding: 0.65rem clamp(1rem, 3vw, 2.4rem);
    background: var(--color-lichen-muted);
    color: var(--color-lichen);
    font-size: 0.68rem;
  }

  .action-feedback--danger {
    border-color: var(--color-danger);
    background: var(--color-danger-muted);
    color: var(--color-danger);
  }

  .studio-body {
    display: grid;
    gap: 0.9rem;
    padding: 0.9rem clamp(0.75rem, 2vw, 1.4rem) 2rem;
  }

  .studio-tabs {
    display: flex;
    overflow-x: auto;
    border: 1px solid var(--color-rule);
    background: var(--color-surface);
  }

  .studio-tabs button {
    min-width: 6.3rem;
    min-height: 2.8rem;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 0.36rem;
    border: 0;
    border-right: 1px solid var(--color-rule);
    padding: 0.5rem 0.72rem;
    background: transparent;
    color: var(--color-ink-muted);
    cursor: pointer;
    font-size: 0.65rem;
    font-weight: 690;
  }

  .studio-tabs button.active {
    box-shadow: inset 0 -2px var(--color-clay);
    background: var(--color-surface-muted);
    color: var(--color-ink);
  }

  .studio-tabs span {
    min-width: 1.1rem;
    padding: 0.09rem 0.22rem;
    background: var(--color-paper);
    color: var(--color-clay);
    font-size: 0.53rem;
  }

  .studio-workspace {
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(0, 1.65fr) minmax(20rem, 0.85fr);
    align-items: start;
    gap: 0.9rem;
  }

  .studio-workspace--wide {
    grid-template-columns: minmax(0, 1fr);
  }

  .studio-main {
    min-width: 0;
  }

  .studio-workspace > :global(.step-inspector) {
    position: sticky;
    top: calc(var(--shell-header-height) + 0.9rem);
    max-height: calc(100vh - var(--shell-header-height) - 1.8rem);
    overflow-y: auto;
  }

  .graph-loading,
  .developer-loading {
    min-height: 32rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-rule);
    background: var(--color-surface-muted);
    color: var(--color-ink-muted);
    font-size: 0.68rem;
  }

  .developer-view {
    border: 1px solid var(--color-rule);
    background: var(--color-surface);
  }

  .developer-view > header {
    display: flex;
    align-items: start;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.9rem;
  }

  .developer-view header > div {
    max-width: 48rem;
    display: grid;
    gap: 0.2rem;
  }

  .developer-view small {
    color: var(--color-clay);
    font-size: 0.58rem;
    font-weight: 780;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .developer-view h2 {
    margin: 0;
    font-family: var(--font-editorial);
    font-size: 1.2rem;
    font-weight: 550;
  }

  .developer-view p {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.67rem;
    line-height: 1.5;
  }

  .developer-view pre {
    max-height: 68vh;
    overflow: auto;
    margin: 0;
    padding: 1rem;
    background: var(--color-surface-muted);
    color: var(--color-ink-soft);
    font: 0.64rem/1.55 ui-monospace, SFMono-Regular, Consolas, monospace;
    white-space: pre-wrap;
  }

  .developer-error {
    padding: 1rem;
    color: var(--color-danger) !important;
  }

  .directory-loading,
  .studio-loading {
    display: grid;
    gap: 1px;
    background: var(--color-rule);
  }

  .directory-loading {
    grid-template-columns: repeat(3, minmax(0, 1fr));
    margin-top: 2rem;
  }

  .directory-loading span {
    min-height: 18rem;
    background: var(--color-surface-muted);
  }

  .studio-loading {
    min-height: calc(100vh - var(--shell-header-height));
    align-content: start;
    padding: 2rem;
    background: transparent;
  }

  .studio-loading__title,
  .studio-loading__summary,
  .studio-loading div span {
    display: block;
    background: var(--color-surface-muted);
  }

  .studio-loading__title {
    width: min(42rem, 85%);
    height: 4.8rem;
  }

  .studio-loading__summary {
    width: min(52rem, 95%);
    height: 1.3rem;
  }

  .studio-loading > div {
    display: grid;
    grid-template-columns: 1.6fr 0.8fr;
    gap: 1px;
    margin-top: 1.5rem;
    background: var(--color-rule);
  }

  .studio-loading div span {
    min-height: 34rem;
  }

  .route-error {
    min-height: 22rem;
    display: grid;
    align-content: center;
    justify-items: start;
    gap: 0.65rem;
    padding: clamp(1.5rem, 6vw, 5rem);
  }

  .route-error h1,
  .route-error h2 {
    margin: 0;
    font-family: var(--font-editorial);
    font-size: clamp(1.5rem, 4vw, 2.8rem);
    font-weight: 520;
  }

  .route-error p {
    max-width: 40rem;
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.72rem;
    line-height: 1.55;
  }

  .route-error > div {
    display: flex;
    align-items: center;
    gap: 0.7rem;
  }

  @media (max-width: 82rem) {
    .run-metrics {
      grid-template-columns: minmax(12rem, 0.8fr) minmax(22rem, 1.4fr);
    }

    .run-actions {
      grid-column: 1 / -1;
      justify-content: flex-start;
    }

    .studio-workspace {
      grid-template-columns: minmax(0, 1.35fr) minmax(18rem, 0.8fr);
    }
  }

  @media (max-width: 64rem) {
    .studio-workspace {
      grid-template-columns: 1fr;
    }

    .studio-workspace > :global(.step-inspector) {
      position: static;
      max-height: none;
    }
  }

  @media (max-width: 52rem) {
    .run-metrics {
      grid-template-columns: 1fr;
    }

    .run-metrics > dl,
    .run-actions {
      grid-column: auto;
    }

    .directory-header,
    .run-heading {
      align-items: flex-start;
      flex-direction: column;
    }

    .directory-actions {
      justify-content: flex-start;
    }
  }

  @media (max-width: 40rem) {
    .run-directory,
    .studio-body {
      padding-right: 0.65rem;
      padding-left: 0.65rem;
    }

    .run-cards {
      grid-template-columns: 1fr;
    }

    .run-metrics > dl {
      grid-template-columns: 1fr 1fr;
    }

    .directory-loading {
      grid-template-columns: 1fr;
    }

    .developer-view > header {
      flex-direction: column;
    }
  }
</style>
