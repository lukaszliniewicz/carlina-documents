<script lang="ts">
  import { onMount } from "svelte";
  import type {
    FrontendApiClient,
    FrontendBootstrap,
    FrontendOperationsActionResponse,
    FrontendOperationsApproval,
    FrontendOperationsContainer,
    FrontendOperationsDashboard,
    FrontendOperationsSchedule,
    FrontendOperationsWorker,
  } from "@carlina/sdk";
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

  import StructuredData from "../components/context/StructuredData.svelte";
  import {
    approvalHasDetail,
    formatOperationsDate,
    matchesOperation,
    operationSearchText,
    operationsRoute,
    operationsViewFromRoute,
    relativeOperationsDate,
    type RuntimeOperationsView,
  } from "../lib/runtime-operations.ts";

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

  interface PendingAction {
    title: string;
    description: string;
    confirmLabel: string;
    endpoint: string;
    body?: Readonly<Record<string, unknown>>;
    successMessage: string;
    danger?: boolean;
    confirmationTarget?: string;
  }

  type ApprovalFilter =
    | "all"
    | "pending"
    | "approved"
    | "rejected"
    | "closed";
  type JobFilter =
    | "all"
    | "active"
    | "attention"
    | "completed";

  let { api, bootstrap, route, navigate }: Props = $props();

  let dashboard = $state<FrontendOperationsDashboard | null>(null);
  let loading = $state(true);
  let refreshing = $state(false);
  let error = $state("");
  let notice = $state("");
  let query = $state("");
  let approvalFilter = $state<ApprovalFilter>("pending");
  let jobFilter = $state<JobFilter>("active");
  let selectedApprovalId = $state("");
  let pendingAction = $state<PendingAction | null>(null);
  let confirmationText = $state("");
  let actionPending = $state(false);
  let controller: AbortController | null = null;
  let refreshTimer: number | null = null;

  const isAdmin = $derived(bootstrap.user.is_admin);
  const view = $derived(operationsViewFromRoute(route, isAdmin));
  const tabs = $derived(
    [
      { id: "overview", label: "Overview", count: dashboard?.summary.attention ?? 0 },
      {
        id: "approvals",
        label: "Approvals",
        count: dashboard?.summary.pending_approvals ?? 0,
      },
      { id: "jobs", label: "Jobs", count: dashboard?.summary.active_jobs ?? 0 },
      {
        id: "workers",
        label: "Workers",
        count: dashboard?.summary.worker_processes ?? 0,
        admin: true,
      },
      {
        id: "schedules",
        label: "Schedules",
        count: dashboard?.summary.schedules ?? 0,
        admin: true,
      },
      {
        id: "containers",
        label: "Containers",
        count: dashboard?.summary.containers ?? 0,
        admin: true,
      },
    ].filter((item) => !item.admin || isAdmin) as Array<{
      id: RuntimeOperationsView;
      label: string;
      count: number;
      admin?: boolean;
    }>,
  );
  const filteredApprovals = $derived.by(() => {
    const approvals = dashboard?.approvals ?? [];
    return approvals.filter((approval) => {
      const statusMatches =
        approvalFilter === "all" ||
        approval.status === approvalFilter ||
        (approvalFilter === "closed" &&
          ["cancelled", "expired"].includes(approval.status));
      return (
        statusMatches &&
        matchesOperation(operationSearchText(approval), query)
      );
    });
  });
  const selectedApproval = $derived(
    filteredApprovals.find((item) => item.id === selectedApprovalId) ??
      filteredApprovals[0] ??
      null,
  );
  const filteredJobs = $derived.by(() => {
    const jobs = dashboard?.jobs ?? [];
    return jobs.filter((job) => {
      const statusMatches =
        jobFilter === "all" ||
        (jobFilter === "active" && job.active) ||
        (jobFilter === "attention" && job.needs_attention) ||
        (jobFilter === "completed" && job.status === "completed");
      return (
        statusMatches &&
        matchesOperation(operationSearchText(job), query)
      );
    });
  });
  const filteredWorkers = $derived(
    (dashboard?.workers ?? []).filter((worker) =>
      matchesOperation(operationSearchText(worker), query),
    ),
  );
  const filteredSchedules = $derived(
    (dashboard?.schedules ?? []).filter((schedule) =>
      matchesOperation(operationSearchText(schedule), query),
    ),
  );
  const filteredContainers = $derived(
    (dashboard?.docker?.containers ?? []).filter((container) =>
      matchesOperation(operationSearchText(container), query),
    ),
  );

  onMount(() => {
    void loadOperations(false);
    refreshTimer = window.setInterval(() => {
      if (!document.hidden && !pendingAction && !actionPending) {
        void loadOperations(true);
      }
    }, 15_000);
    return () => {
      if (refreshTimer !== null) window.clearInterval(refreshTimer);
      controller?.abort();
    };
  });

  async function loadOperations(
    silent: boolean,
    refreshDocker = false,
  ): Promise<void> {
    controller?.abort();
    const nextController = new AbortController();
    controller = nextController;
    if (silent) refreshing = true;
    else loading = true;
    if (!silent) error = "";
    try {
      dashboard = await api.request<FrontendOperationsDashboard>(
        `/api/frontend/runtime/operations${refreshDocker ? "?refresh_docker=1" : ""}`,
        { signal: nextController.signal },
      );
      if (
        dashboard.approvals.length &&
        !dashboard.approvals.some((item) => item.id === selectedApprovalId)
      ) {
        selectedApprovalId = dashboard.approvals[0]?.id ?? "";
      }
    } catch (reason) {
      if (nextController.signal.aborted) return;
      if (!silent) {
        error =
          reason instanceof Error
            ? reason.message
            : "Runtime operations could not be loaded.";
      }
    } finally {
      if (controller === nextController) {
        controller = null;
        loading = false;
        refreshing = false;
      }
    }
  }

  function selectView(nextView: RuntimeOperationsView): void {
    query = "";
    navigate(operationsRoute(nextView, isAdmin), true);
  }

  function openHref(href: string): void {
    navigate(href, true);
  }

  function requestApprovalAction(
    approval: FrontendOperationsApproval,
    status: "approved" | "rejected" | "cancelled",
  ): void {
    const definitions = {
      approved: {
        title: `Approve “${approval.title}”?`,
        description:
          "The suspended activity may continue immediately after this decision. Review the effects and preview before approving.",
        confirmLabel: "Approve and continue",
        successMessage: "The approval was accepted.",
      },
      rejected: {
        title: `Reject “${approval.title}”?`,
        description:
          "The requested activity will not proceed. The workflow may stop or choose another path.",
        confirmLabel: "Reject request",
        successMessage: "The approval was rejected.",
      },
      cancelled: {
        title: `Cancel “${approval.title}”?`,
        description:
          "This withdraws the pending request. Any workflow waiting on it may remain paused until it is updated.",
        confirmLabel: "Cancel request",
        successMessage: "The approval request was cancelled.",
      },
    } as const;
    pendingAction = {
      ...definitions[status],
      endpoint: approval.resolve_url,
      body: { status },
      danger: status !== "approved",
    };
    confirmationText = "";
  }

  function requestWorkerAction(
    worker: FrontendOperationsWorker,
    action: "stop" | "restart",
  ): void {
    const stopping = action === "stop";
    pendingAction = {
      title: `${stopping ? "Stop" : "Restart"} ${worker.name} workers?`,
      description: stopping
        ? "All tracked processes for this addon will stop. In-flight tasks may need to be retried."
        : "Tracked processes will restart with the registered entrypoint and desired process count.",
      confirmLabel: stopping ? "Stop workers" : "Restart workers",
      endpoint: stopping ? worker.stop_url : worker.restart_url,
      successMessage: `${worker.name} workers were ${stopping ? "stopped" : "restarted"}.`,
      danger: stopping,
    };
    confirmationText = "";
  }

  function requestPidTermination(
    worker: FrontendOperationsWorker,
    pid: number,
    endpoint: string,
  ): void {
    pendingAction = {
      title: `Terminate worker process ${pid}?`,
      description: `Only PID ${pid} from ${worker.name} will be terminated. Its current task may be interrupted.`,
      confirmLabel: "Terminate process",
      endpoint,
      successMessage: `Worker process ${pid} was terminated.`,
      danger: true,
      confirmationTarget: String(pid),
    };
    confirmationText = "";
  }

  function requestScheduleAction(
    schedule: FrontendOperationsSchedule,
    action: "pause" | "resume" | "trigger",
  ): void {
    const definitions = {
      pause: {
        title: `Pause “${schedule.title}”?`,
        description:
          "Future executions will remain paused until an administrator resumes this schedule.",
        confirmLabel: "Pause schedule",
        endpoint: schedule.pause_url,
        message: `${schedule.title} was paused.`,
        danger: true,
      },
      resume: {
        title: `Resume “${schedule.title}”?`,
        description:
          "The scheduler will restore its declared cadence and next execution time.",
        confirmLabel: "Resume schedule",
        endpoint: schedule.resume_url,
        message: `${schedule.title} was resumed.`,
        danger: false,
      },
      trigger: {
        title: `Run “${schedule.title}” now?`,
        description:
          "The scheduler will queue an immediate execution in addition to its normal cadence.",
        confirmLabel: "Run now",
        endpoint: schedule.trigger_url,
        message: `${schedule.title} was queued to run.`,
        danger: false,
      },
    } as const;
    const definition = definitions[action];
    pendingAction = {
      title: definition.title,
      description: definition.description,
      confirmLabel: definition.confirmLabel,
      endpoint: definition.endpoint,
      successMessage: definition.message,
      danger: definition.danger,
    };
    confirmationText = "";
  }

  function requestContainerAction(
    container: FrontendOperationsContainer,
    action: "start" | "stop" | "restart" | "rebuild" | "remove",
  ): void {
    const definitions = {
      start: {
        title: `Start ${container.name}?`,
        description: "The addon-managed service will become available.",
        confirmLabel: "Start container",
        message: `${container.name} was started.`,
        danger: false,
      },
      stop: {
        title: `Stop ${container.name}?`,
        description:
          "The service will become unavailable and active work may be interrupted.",
        confirmLabel: "Stop container",
        message: `${container.name} was stopped.`,
        danger: true,
      },
      restart: {
        title: `Restart ${container.name}?`,
        description:
          "The service will disconnect briefly while the existing container restarts.",
        confirmLabel: "Restart container",
        message: `${container.name} was restarted.`,
        danger: false,
      },
      rebuild: {
        title: `Rebuild ${container.name}?`,
        description:
          "The addon image will be replaced and the service stack may restart. This can take several minutes.",
        confirmLabel: "Rebuild image",
        message: `${container.name} rebuild was requested.`,
        danger: true,
      },
      remove: {
        title: `Remove ${container.name}?`,
        description:
          "The container will be deleted. The addon may recreate it when the service is next requested.",
        confirmLabel: "Remove container",
        message: `${container.name} was removed.`,
        danger: true,
      },
    } as const;
    const definition = definitions[action];
    pendingAction = {
      title: definition.title,
      description: definition.description,
      confirmLabel: definition.confirmLabel,
      endpoint: `${container.action_url}/${action}`,
      successMessage: definition.message,
      danger: definition.danger,
      confirmationTarget:
        action === "remove" || action === "rebuild"
          ? container.name
          : undefined,
    };
    confirmationText = "";
  }

  async function performPendingAction(): Promise<void> {
    if (!pendingAction || actionPending) return;
    const action = pendingAction;
    actionPending = true;
    error = "";
    notice = "";
    try {
      const response = await api.request<FrontendOperationsActionResponse>(
        action.endpoint,
        {
          method: "POST",
          ...(action.body
            ? {
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(action.body),
              }
            : {}),
        },
      );
      notice = response.message || action.successMessage;
      pendingAction = null;
      confirmationText = "";
      await loadOperations(true);
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "The operation could not be completed.";
    } finally {
      actionPending = false;
    }
  }

  function approvalCount(status: string): number {
    return dashboard?.counts.approvals[status] ?? 0;
  }

  function jobCount(status: string): number {
    return dashboard?.counts.jobs[status] ?? 0;
  }
</script>

<svelte:head>
  <meta
    name="description"
    content="Review runtime activity, decisions, workers, schedules, and managed services."
  />
</svelte:head>

<div class="operations-page">
  <PageHeader
    eyebrow="Runtime operations"
    title="Operations desk"
    description="See what is moving, what needs a decision, and which services are carrying the work—without reading the machinery."
  >
    {#snippet actions()}
      <Button
        variant="outline"
        disabled={refreshing || loading}
        onclick={() => void loadOperations(true, isAdmin)}
      >
        <Icon name="refresh" size={15} />
        {refreshing ? "Refreshing…" : "Refresh"}
      </Button>
      <Button variant="quiet" onclick={() => openHref("/runs")}>
        Run Studio <Icon name="arrow-right" size={15} />
      </Button>
    {/snippet}
  </PageHeader>

  {#if notice}
    <div class="page-notice" role="status">
      <Icon name="check" size={16} />
      <span>{notice}</span>
      <button type="button" aria-label="Dismiss notice" onclick={() => (notice = "")}>
        <Icon name="close" size={14} />
      </button>
    </div>
  {/if}

  {#if error && dashboard}
    <div class="page-notice page-notice--danger" role="alert">
      <Icon name="activity" size={16} />
      <span>{error}</span>
      <button type="button" aria-label="Dismiss error" onclick={() => (error = "")}>
        <Icon name="close" size={14} />
      </button>
    </div>
  {/if}

  <nav class="operations-tabs" aria-label="Runtime operations views">
    {#each tabs as tab (tab.id)}
      <button
        type="button"
        class:active={view === tab.id}
        aria-current={view === tab.id ? "page" : undefined}
        onclick={() => selectView(tab.id)}
      >
        {tab.label}
        {#if tab.count > 0}<span>{tab.count}</span>{/if}
      </button>
    {/each}
  </nav>

  {#if loading && !dashboard}
    <section class="operations-loading" aria-busy="true" aria-label="Loading operations">
      <div class="loading-summary">
        <span></span><span></span><span></span><span></span>
      </div>
      <div class="loading-workspace">
        <span></span><span></span>
      </div>
    </section>
  {:else if error && !dashboard}
    <EmptyState
      icon="activity"
      title="Operations could not be opened"
      description={error}
    >
      {#snippet actions()}
        <Button variant="outline" onclick={() => void loadOperations(false)}>
          Try again
        </Button>
      {/snippet}
    </EmptyState>
  {:else if dashboard}
    {#if view === "overview"}
      <section class="overview" aria-label="Operations overview">
        <div class="summary-grid">
          <button type="button" onclick={() => selectView("approvals")}>
            <small>Decisions waiting</small>
            <strong>{dashboard.summary.pending_approvals}</strong>
            <span>
              {dashboard.summary.pending_approvals === 1
                ? "approval needs review"
                : "approvals need review"}
            </span>
          </button>
          <button type="button" onclick={() => selectView("jobs")}>
            <small>Work in motion</small>
            <strong>{dashboard.summary.active_jobs}</strong>
            <span>{dashboard.summary.jobs_total} jobs recorded</span>
          </button>
          {#if isAdmin}
            <button type="button" onclick={() => selectView("workers")}>
              <small>Worker processes</small>
              <strong>{dashboard.summary.worker_processes}</strong>
              <span>
                {dashboard.summary.worker_addons} configured addons
              </span>
            </button>
            <button type="button" onclick={() => selectView("schedules")}>
              <small>Scheduled work</small>
              <strong>{dashboard.summary.schedules}</strong>
              <span>
                {dashboard.summary.unhealthy_workers
                  ? `${dashboard.summary.unhealthy_workers} worker groups need attention`
                  : "runtime services look settled"}
              </span>
            </button>
          {:else}
            <div class="summary-reading">
              <small>Completed work</small>
              <strong>{jobCount("completed")}</strong>
              <span>recent results available in Run Studio</span>
            </div>
            <div class="summary-reading">
              <small>Needs attention</small>
              <strong>{dashboard.summary.attention}</strong>
              <span>decisions and failed jobs combined</span>
            </div>
          {/if}
        </div>

        <div class="overview-grid">
          <section class="editorial-panel attention-panel">
            <header>
              <div>
                <p>Editorial queue</p>
                <h2>What needs a human</h2>
              </div>
              <button type="button" onclick={() => selectView("approvals")}>
                All approvals <Icon name="arrow-right" size={14} />
              </button>
            </header>
            {#if dashboard.approvals.filter((item) => item.status === "pending").length}
              <div class="attention-list">
                {#each dashboard.approvals.filter((item) => item.status === "pending").slice(0, 4) as approval (approval.id)}
                  <button
                    type="button"
                    onclick={() => {
                      selectedApprovalId = approval.id;
                      approvalFilter = "pending";
                      selectView("approvals");
                    }}
                  >
                    <span>
                      <small>{approval.subject_label}</small>
                      <strong>{approval.title}</strong>
                      <em>{approval.summary}</em>
                    </span>
                    <Icon name="chevron-right" size={16} />
                  </button>
                {/each}
              </div>
            {:else}
              <div class="quiet-reading">
                <Icon name="check" size={20} />
                <div>
                  <strong>No decisions are waiting.</strong>
                  <p>New approval requests will appear here automatically.</p>
                </div>
              </div>
            {/if}
          </section>

          <section class="editorial-panel">
            <header>
              <div>
                <p>Live ledger</p>
                <h2>Recent runtime work</h2>
              </div>
              <button type="button" onclick={() => selectView("jobs")}>
                All jobs <Icon name="arrow-right" size={14} />
              </button>
            </header>
            {#if dashboard.jobs.length}
              <div class="job-ledger">
                {#each dashboard.jobs.slice(0, 6) as job (job.id)}
                  <article>
                    <StatusMark status={job.status} label={job.status_label} compact />
                    <div>
                      <strong>{job.title}</strong>
                      <span>{job.source_label} · {relativeOperationsDate(job.updated_at)}</span>
                    </div>
                    {#if job.progress !== null}
                      <small>{job.progress}%</small>
                    {/if}
                  </article>
                {/each}
              </div>
            {:else}
              <div class="quiet-reading">
                <Icon name="activity" size={20} />
                <div>
                  <strong>No runtime jobs yet.</strong>
                  <p>Background and workflow work will be recorded here.</p>
                </div>
              </div>
            {/if}
          </section>

          {#if isAdmin}
            <section class="editorial-panel service-panel">
              <header>
                <div>
                  <p>Service room</p>
                  <h2>Worker health</h2>
                </div>
                <button type="button" onclick={() => selectView("workers")}>
                  Inspect <Icon name="arrow-right" size={14} />
                </button>
              </header>
              {#if dashboard.workers.length}
                <div class="service-list">
                  {#each dashboard.workers.slice(0, 6) as worker (worker.id)}
                    <article>
                      <i class:healthy={worker.healthy}></i>
                      <div>
                        <strong>{worker.name}</strong>
                        <span>
                          {worker.running} of {worker.desired || worker.configured}
                          processes
                        </span>
                      </div>
                      <Badge tone={worker.healthy ? "success" : "warning"}>
                        {worker.status_label}
                      </Badge>
                    </article>
                  {/each}
                </div>
              {:else}
                <div class="quiet-reading">
                  <Icon name="activity" size={20} />
                  <div>
                    <strong>No dedicated workers are configured.</strong>
                    <p>In-process features remain available.</p>
                  </div>
                </div>
              {/if}
            </section>

            <section class="editorial-panel docker-reading">
              <header>
                <div>
                  <p>Managed services</p>
                  <h2>Docker availability</h2>
                </div>
                <button type="button" onclick={() => selectView("containers")}>
                  Details <Icon name="arrow-right" size={14} />
                </button>
              </header>
              <div class="docker-state">
                <span class:available={dashboard.docker?.available}>
                  <Icon
                    name={dashboard.docker?.available ? "check" : "stop"}
                    size={20}
                  />
                </span>
                <div>
                  <strong>
                    {dashboard.docker?.available
                      ? "Docker Engine available"
                      : "Container services are offline"}
                  </strong>
                  <p>{dashboard.docker?.message}</p>
                </div>
              </div>
            </section>
          {/if}
        </div>
      </section>
    {:else if view === "approvals"}
      <section class="directory-view">
        <div class="directory-toolbar">
          <SearchField
            id="approval-search"
            value={query}
            placeholder="Search approvals, effects, or runs"
            oninput={(value) => (query = value)}
          />
          <div class="filter-row" aria-label="Approval status">
            {#each [
              { id: "pending", label: "Pending", count: approvalCount("pending") },
              { id: "approved", label: "Approved", count: approvalCount("approved") },
              { id: "rejected", label: "Rejected", count: approvalCount("rejected") },
              {
                id: "closed",
                label: "Other closed",
                count: approvalCount("cancelled") + approvalCount("expired"),
              },
              {
                id: "all",
                label: "All",
                count: Object.values(dashboard.counts.approvals).reduce(
                  (total, count) => total + count,
                  0,
                ),
              },
            ] as Array<{ id: ApprovalFilter; label: string; count: number }> as filter (filter.id)}
              <button
                type="button"
                class:active={approvalFilter === filter.id}
                onclick={() => (approvalFilter = filter.id)}
              >
                {filter.label} <span>{filter.count}</span>
              </button>
            {/each}
          </div>
        </div>

        {#if filteredApprovals.length}
          <div class="approval-workspace">
            <section class="approval-list" aria-label="Approval requests">
              <header>
                <p>{filteredApprovals.length} visible requests</p>
                <span>Newest first</span>
              </header>
              {#each filteredApprovals as approval (approval.id)}
                <button
                  type="button"
                  class:active={selectedApproval?.id === approval.id}
                  aria-pressed={selectedApproval?.id === approval.id}
                  onclick={() => (selectedApprovalId = approval.id)}
                >
                  <span class="approval-list__status">
                    <StatusMark
                      status={approval.status}
                      label={approval.status_label}
                      compact
                    />
                    <small>{relativeOperationsDate(approval.created_at)}</small>
                  </span>
                  <strong>{approval.title}</strong>
                  <p>{approval.summary}</p>
                  <span class="approval-list__meta">
                    {approval.subject_label}
                    {#if approval.effects.length}
                      · {approval.effects.slice(0, 2).join(", ")}
                    {/if}
                  </span>
                </button>
              {/each}
            </section>

            {#if selectedApproval}
              <article class="approval-detail">
                <header>
                  <div>
                    <p>{selectedApproval.subject_label}</p>
                    <h2>{selectedApproval.title}</h2>
                    <span>{selectedApproval.summary}</span>
                  </div>
                  <StatusMark
                    status={selectedApproval.status}
                    label={selectedApproval.status_label}
                  />
                </header>

                <dl class="approval-facts">
                  <div>
                    <dt>Requested</dt>
                    <dd>{formatOperationsDate(selectedApproval.created_at)}</dd>
                  </div>
                  <div>
                    <dt>Expires</dt>
                    <dd>{formatOperationsDate(selectedApproval.expires_at)}</dd>
                  </div>
                  {#if selectedApproval.resolved_at}
                    <div>
                      <dt>Resolved</dt>
                      <dd>{formatOperationsDate(selectedApproval.resolved_at)}</dd>
                    </div>
                  {/if}
                  {#if selectedApproval.tool_label}
                    <div>
                      <dt>Requested by</dt>
                      <dd>{selectedApproval.tool_label}</dd>
                    </div>
                  {/if}
                </dl>

                {#if selectedApproval.effects.length}
                  <section class="approval-section">
                    <p>Expected effects</p>
                    <div class="effect-list">
                      {#each selectedApproval.effects as effect}
                        <Badge tone="warning">{effect}</Badge>
                      {/each}
                    </div>
                  </section>
                {/if}

                {#if approvalHasDetail(selectedApproval)}
                  <section class="approval-section">
                    <p>Review preview</p>
                    <StructuredData
                      value={selectedApproval.preview}
                      emptyText="No preview was supplied."
                      maxRows={40}
                    />
                  </section>
                {/if}

                {#if selectedApproval.resolved_at}
                  <section class="approval-section">
                    <p>Resolution note</p>
                    <StructuredData
                      value={selectedApproval.resolution_note}
                      emptyText="No resolution note was recorded."
                      compact
                    />
                  </section>
                {/if}

                <footer>
                  <div class="detail-links">
                    {#if selectedApproval.run}
                      <button
                        type="button"
                        onclick={() => openHref(selectedApproval.run!.url)}
                      >
                        Open in Run Studio <Icon name="arrow-up-right" size={14} />
                      </button>
                    {/if}
                    {#each selectedApproval.log_links as link (link.id)}
                      <button type="button" onclick={() => openHref(link.url)}>
                        {link.name} <Icon name="logs" size={14} />
                      </button>
                    {/each}
                  </div>
                  {#if
                    selectedApproval.can_approve ||
                    selectedApproval.can_reject ||
                    selectedApproval.can_cancel
                  }
                    <div class="approval-actions">
                      {#if selectedApproval.can_cancel}
                        <Button
                          variant="quiet"
                          size="sm"
                          onclick={() =>
                            requestApprovalAction(selectedApproval, "cancelled")}
                        >
                          Cancel request
                        </Button>
                      {/if}
                      {#if selectedApproval.can_reject}
                        <Button
                          variant="outline"
                          size="sm"
                          onclick={() =>
                            requestApprovalAction(selectedApproval, "rejected")}
                        >
                          Reject
                        </Button>
                      {/if}
                      {#if selectedApproval.can_approve}
                        <Button
                          size="sm"
                          onclick={() =>
                            requestApprovalAction(selectedApproval, "approved")}
                        >
                          Approve
                        </Button>
                      {/if}
                    </div>
                  {/if}
                </footer>
              </article>
            {/if}
          </div>
        {:else}
          <EmptyState
            icon="shield"
            title="No approvals match"
            description="Adjust the status or search. Pending decisions will appear here as workflows request them."
          />
        {/if}
      </section>
    {:else if view === "jobs"}
      <section class="directory-view">
        <div class="directory-toolbar">
          <SearchField
            id="job-search"
            value={query}
            placeholder="Search jobs, addons, or runs"
            oninput={(value) => (query = value)}
          />
          <div class="filter-row" aria-label="Job status">
            {#each [
              {
                id: "active",
                label: "In motion",
                count: dashboard.summary.active_jobs,
              },
              {
                id: "attention",
                label: "Attention",
                count: dashboard.jobs.filter((item) => item.needs_attention).length,
              },
              {
                id: "completed",
                label: "Completed",
                count: jobCount("completed"),
              },
              {
                id: "all",
                label: "All",
                count: dashboard.summary.jobs_total,
              },
            ] as Array<{ id: JobFilter; label: string; count: number }> as filter (filter.id)}
              <button
                type="button"
                class:active={jobFilter === filter.id}
                onclick={() => (jobFilter = filter.id)}
              >
                {filter.label} <span>{filter.count}</span>
              </button>
            {/each}
          </div>
        </div>

        {#if filteredJobs.length}
          <div class="jobs-directory">
            <header>
              <span>{filteredJobs.length} recent jobs shown</span>
              <small>
                The server keeps aggregate counts beyond this
                {dashboard.limits.jobs}-item working set.
              </small>
            </header>
            {#each filteredJobs as job (job.id)}
              <article class:needs-attention={job.needs_attention}>
                <div class="job-state">
                  <StatusMark status={job.status} label={job.status_label} />
                  {#if job.progress !== null}
                    <div
                      class="job-progress"
                      role="progressbar"
                      aria-label={`${job.title} progress`}
                      aria-valuenow={job.progress}
                      aria-valuemin="0"
                      aria-valuemax="100"
                    >
                      <i style={`width: ${job.progress}%`}></i>
                    </div>
                    <small>{job.progress}%</small>
                  {/if}
                </div>
                <div class="job-copy">
                  <p>{job.source_label}</p>
                  <h2>{job.title}</h2>
                  <span>{job.summary}</span>
                </div>
                <dl>
                  <div>
                    <dt>Updated</dt>
                    <dd>{formatOperationsDate(job.updated_at)}</dd>
                  </div>
                  <div>
                    <dt>Started</dt>
                    <dd>{formatOperationsDate(job.started_at)}</dd>
                  </div>
                </dl>
                <div class="job-links">
                  {#if job.run}
                    <button type="button" onclick={() => openHref(job.run!.url)}>
                      Open run <Icon name="arrow-up-right" size={14} />
                    </button>
                  {/if}
                  {#each job.log_links as link (link.id)}
                    <button type="button" onclick={() => openHref(link.url)}>
                      {link.name} <Icon name="logs" size={14} />
                    </button>
                  {/each}
                </div>
              </article>
            {/each}
          </div>
        {:else}
          <EmptyState
            icon="activity"
            title="No jobs match"
            description="Change the filter or search. New runtime work appears here automatically."
          />
        {/if}
      </section>
    {:else if view === "workers" && isAdmin}
      <section class="directory-view">
        <div class="directory-toolbar directory-toolbar--simple">
          <SearchField
            id="worker-search"
            value={query}
            placeholder="Search worker addons or environments"
            oninput={(value) => (query = value)}
          />
          <p>
            {dashboard.summary.worker_processes} live processes across
            {dashboard.summary.worker_addons} addons
          </p>
        </div>

        {#if filteredWorkers.length}
          <div class="worker-grid">
            {#each filteredWorkers as worker (worker.id)}
              <article class:needs-attention={!worker.healthy}>
                <header>
                  <span class:healthy={worker.healthy}>
                    <Icon
                      name={worker.healthy ? "check" : "activity"}
                      size={18}
                    />
                  </span>
                  <div>
                    <p>{worker.environment}</p>
                    <h2>{worker.name}</h2>
                  </div>
                  <Badge tone={worker.healthy ? "success" : "warning"}>
                    {worker.status_label}
                  </Badge>
                </header>
                <div class="worker-count">
                  <strong>{worker.running}</strong>
                  <span>
                    running of {worker.desired || worker.configured} desired
                  </span>
                </div>
                <dl>
                  <div>
                    <dt>Entrypoint</dt>
                    <dd>{worker.entrypoint ?? "Registered worker"}</dd>
                  </div>
                  <div>
                    <dt>Processes</dt>
                    <dd>
                      {worker.pids.length
                        ? worker.pids.map((item) => item.value).join(", ")
                        : "None running"}
                    </dd>
                  </div>
                </dl>
                <footer>
                  <div>
                    {#if worker.log_link}
                      <button
                        type="button"
                        onclick={() => openHref(worker.log_link!.url)}
                      >
                        Logs <Icon name="logs" size={14} />
                      </button>
                    {/if}
                    {#each worker.pids as pid (pid.value)}
                      <button
                        type="button"
                        class="danger-link"
                        onclick={() =>
                          requestPidTermination(
                            worker,
                            pid.value,
                            pid.terminate_url,
                          )}
                      >
                        End PID {pid.value}
                      </button>
                    {/each}
                  </div>
                  <div>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={!worker.running}
                      onclick={() => requestWorkerAction(worker, "stop")}
                    >
                      Stop
                    </Button>
                    <Button
                      size="sm"
                      onclick={() => requestWorkerAction(worker, "restart")}
                    >
                      Restart
                    </Button>
                  </div>
                </footer>
              </article>
            {/each}
          </div>
        {:else}
          <EmptyState
            icon="activity"
            title="No worker groups match"
            description="Dedicated addon workers will appear here when they are configured or running."
          />
        {/if}
      </section>
    {:else if view === "schedules" && isAdmin}
      <section class="directory-view">
        <div class="directory-toolbar directory-toolbar--simple">
          <SearchField
            id="schedule-search"
            value={query}
            placeholder="Search scheduled work or addons"
            oninput={(value) => (query = value)}
          />
          <p>{dashboard.summary.schedules} registered schedules</p>
        </div>

        {#if filteredSchedules.length}
          <div class="schedule-list">
            {#each filteredSchedules as schedule (schedule.id)}
              <article>
                <div class="schedule-date">
                  <small>Next execution</small>
                  <strong>
                    {schedule.paused
                      ? "Paused"
                      : relativeOperationsDate(schedule.next_run_at)}
                  </strong>
                  <span>{formatOperationsDate(schedule.next_run_at)}</span>
                </div>
                <div class="schedule-copy">
                  <p>{schedule.addon_name}</p>
                  <h2>{schedule.title}</h2>
                  <span>{schedule.trigger || "Registered scheduler cadence"}</span>
                </div>
                <StatusMark
                  status={schedule.status}
                  label={schedule.status_label}
                />
                <div class="schedule-actions">
                  {#if schedule.log_link}
                    <button
                      type="button"
                      onclick={() => openHref(schedule.log_link!.url)}
                    >
                      Logs
                    </button>
                  {/if}
                  {#if schedule.paused}
                    <Button
                      variant="outline"
                      size="sm"
                      onclick={() => requestScheduleAction(schedule, "resume")}
                    >
                      Resume
                    </Button>
                  {:else}
                    <Button
                      variant="quiet"
                      size="sm"
                      onclick={() => requestScheduleAction(schedule, "pause")}
                    >
                      Pause
                    </Button>
                  {/if}
                  <Button
                    size="sm"
                    onclick={() => requestScheduleAction(schedule, "trigger")}
                  >
                    Run now
                  </Button>
                </div>
              </article>
            {/each}
          </div>
        {:else}
          <EmptyState
            icon="activity"
            title="No schedules match"
            description="Registered addon schedules will appear here with their next execution and controls."
          />
        {/if}
      </section>
    {:else if view === "containers" && isAdmin}
      <section class="directory-view">
        <div class="directory-toolbar directory-toolbar--simple">
          <SearchField
            id="container-search"
            value={query}
            placeholder="Search managed containers or images"
            oninput={(value) => (query = value)}
          />
          <p>{dashboard.docker?.message}</p>
        </div>

        {#if !dashboard.docker?.available}
          <section class="docker-unavailable">
            <span><Icon name="stop" size={24} /></span>
            <div>
              <p>Optional service tier</p>
              <h2>Docker Engine is not available</h2>
              <p>
                Container-backed addons are offline, while application workers,
                schedules, approvals, and ordinary runtime jobs continue
                independently.
              </p>
              {#if dashboard.docker?.log_links.length}
                <div>
                  {#each dashboard.docker.log_links as link (link.id)}
                    <button type="button" onclick={() => openHref(link.url)}>
                      {link.name} <Icon name="logs" size={14} />
                    </button>
                  {/each}
                </div>
              {/if}
            </div>
          </section>
        {:else if filteredContainers.length}
          <div class="container-grid">
            {#each filteredContainers as container (container.id)}
              <article>
                <header>
                  <div>
                    <p>
                      {container.legacy ? "Legacy service" : "Addon-managed service"}
                      · {container.addon_id} / {container.service_name}
                    </p>
                    <h2>{container.name}</h2>
                  </div>
                  <StatusMark
                    status={container.status}
                    label={container.status_label}
                  />
                </header>
                <dl>
                  <div>
                    <dt>Image</dt>
                    <dd>{container.image}</dd>
                  </div>
                  <div>
                    <dt>Created</dt>
                    <dd>{formatOperationsDate(container.created_at)}</dd>
                  </div>
                </dl>
                <footer>
                  <div>
                    {#each container.log_links as link (link.id)}
                      <button type="button" onclick={() => openHref(link.url)}>
                        {link.name}
                      </button>
                    {/each}
                  </div>
                  <div>
                    {#if container.status === "running"}
                      <Button
                        variant="quiet"
                        size="sm"
                        onclick={() => requestContainerAction(container, "stop")}
                      >
                        Stop
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        onclick={() =>
                          requestContainerAction(container, "restart")}
                      >
                        Restart
                      </Button>
                    {:else}
                      <Button
                        variant="outline"
                        size="sm"
                        onclick={() => requestContainerAction(container, "start")}
                      >
                        Start
                      </Button>
                    {/if}
                    <Button
                      variant="quiet"
                      size="sm"
                      onclick={() => requestContainerAction(container, "rebuild")}
                    >
                      Rebuild
                    </Button>
                    <Button
                      variant="danger"
                      size="sm"
                      onclick={() => requestContainerAction(container, "remove")}
                    >
                      Remove
                    </Button>
                  </div>
                </footer>
              </article>
            {/each}
          </div>
        {:else}
          <EmptyState
            icon="workspaces"
            title="No managed containers match"
            description="Addon-managed containers will appear here when their service stack has been created."
          />
        {/if}
      </section>
    {/if}

    <p class="updated-reading">
      Snapshot updated {relativeOperationsDate(dashboard.generated_at)}.
      Automatic refresh pauses while this tab is hidden.
    </p>
  {/if}
</div>

<Dialog
  open={Boolean(pendingAction)}
  title={pendingAction?.title ?? "Confirm runtime operation"}
  description={pendingAction?.description}
  onclose={() => {
    if (!actionPending) {
      pendingAction = null;
      confirmationText = "";
    }
  }}
  size="sm"
>
  {#if pendingAction?.confirmationTarget}
    <label class="typed-confirmation">
      <span>
        Type <code>{pendingAction.confirmationTarget}</code> to confirm
      </span>
      <input
        type="text"
        autocomplete="off"
        spellcheck="false"
        bind:value={confirmationText}
      />
    </label>
  {:else}
    <p class="dialog-note">
      This change takes effect immediately and will be reflected in the next
      operations snapshot.
    </p>
  {/if}
  {#snippet footer()}
    <Button
      variant="quiet"
      disabled={actionPending}
      onclick={() => {
        pendingAction = null;
        confirmationText = "";
      }}
    >
      Keep current state
    </Button>
    <Button
      variant={pendingAction?.danger ? "danger" : "primary"}
      disabled={actionPending ||
        Boolean(
          pendingAction?.confirmationTarget &&
            confirmationText.trim() !== pendingAction.confirmationTarget,
        )}
      onclick={() => void performPendingAction()}
    >
      {actionPending ? "Working…" : pendingAction?.confirmLabel ?? "Continue"}
    </Button>
  {/snippet}
</Dialog>

<style>
  .operations-page {
    width: min(100%, 102rem);
    margin: 0 auto;
    padding: clamp(1.4rem, 4vw, 3.5rem);
  }

  .page-notice {
    min-height: 2.8rem;
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: center;
    gap: 0.7rem;
    border: 1px solid
      color-mix(in srgb, var(--color-lichen) 42%, var(--color-rule));
    border-left: 3px solid var(--color-lichen);
    margin-top: 1rem;
    padding: 0.55rem 0.7rem 0.55rem 0.9rem;
    background: var(--color-lichen-muted);
    color: var(--color-ink-soft);
    font-size: 0.75rem;
  }

  .page-notice--danger {
    border-color: color-mix(
      in srgb,
      var(--color-danger) 42%,
      var(--color-rule)
    );
    border-left-color: var(--color-danger);
    background: var(--color-danger-muted);
  }

  .page-notice button {
    display: grid;
    border: 0;
    padding: 0.25rem;
    background: transparent;
    color: inherit;
    cursor: pointer;
  }

  .operations-tabs {
    display: flex;
    overflow-x: auto;
    border-bottom: 1px solid var(--color-rule-strong);
    margin: 1.1rem 0 0;
  }

  .operations-tabs button {
    min-height: 3rem;
    display: inline-flex;
    flex: 0 0 auto;
    align-items: center;
    gap: 0.45rem;
    border: 0;
    border-bottom: 2px solid transparent;
    padding: 0.45rem 0.9rem;
    background: transparent;
    color: var(--color-ink-muted);
    cursor: pointer;
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.035em;
  }

  .operations-tabs button:hover,
  .operations-tabs button.active {
    border-bottom-color: var(--color-clay);
    color: var(--color-ink);
  }

  .operations-tabs span,
  .filter-row span {
    min-width: 1.35rem;
    border: 1px solid var(--color-rule);
    padding: 0.16rem 0.3rem;
    background: var(--color-surface-muted);
    color: var(--color-ink-soft);
    font-size: 0.58rem;
    text-align: center;
  }

  .operations-loading {
    display: grid;
    gap: 1px;
    margin-top: 1.5rem;
    background: var(--color-rule);
  }

  .loading-summary,
  .loading-workspace {
    display: grid;
    gap: 1px;
    background: var(--color-rule);
  }

  .loading-summary {
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }

  .loading-workspace {
    grid-template-columns: 1.2fr 1fr;
  }

  .operations-loading span {
    min-height: 8rem;
    background: linear-gradient(
      90deg,
      var(--color-surface-muted),
      var(--color-surface),
      var(--color-surface-muted)
    );
    background-size: 200% 100%;
    animation: loading 1.6s ease-in-out infinite;
  }

  .loading-workspace span {
    min-height: 24rem;
  }

  .overview {
    margin-top: 1.5rem;
  }

  .summary-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    border: 1px solid var(--color-rule-strong);
    background: var(--color-rule);
    gap: 1px;
  }

  .summary-grid button,
  .summary-reading {
    min-width: 0;
    display: grid;
    justify-items: start;
    gap: 0.25rem;
    border: 0;
    padding: 1.1rem 1.15rem;
    background: var(--color-surface);
    color: var(--color-ink);
    text-align: left;
  }

  .summary-grid button {
    cursor: pointer;
  }

  .summary-grid button:hover {
    background: var(--color-surface-muted);
  }

  .summary-grid small,
  .summary-reading small {
    color: var(--color-clay);
    font-size: 0.58rem;
    font-weight: 800;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  .summary-grid strong,
  .summary-reading strong {
    font-family: var(--font-editorial);
    font-size: 2.35rem;
    font-weight: 450;
    line-height: 1;
  }

  .summary-grid button > span,
  .summary-reading > span {
    color: var(--color-ink-muted);
    font-size: 0.67rem;
    line-height: 1.45;
  }

  .overview-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 1rem;
    margin-top: 1rem;
  }

  .editorial-panel {
    min-width: 0;
    border: 1px solid var(--color-rule-strong);
    background: var(--color-surface);
  }

  .editorial-panel > header {
    min-height: 4.8rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.9rem 1rem;
  }

  .editorial-panel header p,
  .approval-detail header p,
  .worker-grid header p,
  .schedule-copy p,
  .container-grid header p,
  .job-copy p,
  .docker-unavailable > div > p:first-child {
    margin: 0 0 0.22rem;
    color: var(--color-clay);
    font-size: 0.56rem;
    font-weight: 800;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  .editorial-panel h2,
  .approval-detail h2,
  .worker-grid h2,
  .schedule-list h2,
  .container-grid h2,
  .jobs-directory h2,
  .docker-unavailable h2 {
    margin: 0;
    font-family: var(--font-editorial);
    font-size: 1.35rem;
    font-weight: 500;
    line-height: 1.1;
  }

  .editorial-panel header > button,
  .detail-links button,
  .job-links button,
  .worker-grid footer button:not(.ui-button),
  .schedule-actions > button,
  .container-grid footer button:not(.ui-button),
  .docker-unavailable button {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    border: 0;
    padding: 0.25rem 0;
    background: transparent;
    color: var(--color-clay);
    cursor: pointer;
    font-size: 0.64rem;
    font-weight: 750;
  }

  .attention-list,
  .job-ledger,
  .service-list {
    display: grid;
  }

  .attention-list > button {
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: 0.75rem;
    border: 0;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.85rem 1rem;
    background: transparent;
    color: var(--color-ink);
    cursor: pointer;
    text-align: left;
  }

  .attention-list > button:last-child,
  .job-ledger article:last-child,
  .service-list article:last-child {
    border-bottom: 0;
  }

  .attention-list > button:hover {
    background: var(--color-surface-muted);
  }

  .attention-list span {
    min-width: 0;
    display: grid;
    gap: 0.2rem;
  }

  .attention-list small,
  .approval-list__meta,
  .job-ledger span,
  .service-list span {
    color: var(--color-ink-muted);
    font-size: 0.6rem;
  }

  .attention-list strong {
    overflow: hidden;
    font-size: 0.76rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .attention-list em {
    overflow: hidden;
    color: var(--color-ink-soft);
    font-size: 0.66rem;
    font-style: normal;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .quiet-reading,
  .docker-state {
    min-height: 12rem;
    display: flex;
    align-items: center;
    gap: 1rem;
    padding: 1.4rem;
  }

  .quiet-reading strong,
  .docker-state strong {
    font-size: 0.78rem;
  }

  .quiet-reading p,
  .docker-state p {
    margin: 0.3rem 0 0;
    color: var(--color-ink-muted);
    font-size: 0.68rem;
    line-height: 1.5;
  }

  .job-ledger article,
  .service-list article {
    min-width: 0;
    display: grid;
    align-items: center;
    gap: 0.7rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.75rem 1rem;
  }

  .job-ledger article {
    grid-template-columns: auto minmax(0, 1fr) auto;
  }

  .job-ledger article > div,
  .service-list article > div {
    min-width: 0;
    display: grid;
    gap: 0.16rem;
  }

  .job-ledger strong,
  .service-list strong {
    overflow: hidden;
    font-size: 0.72rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .job-ledger article > small {
    color: var(--color-ink-muted);
    font-size: 0.62rem;
  }

  .service-list article {
    grid-template-columns: auto minmax(0, 1fr) auto;
  }

  .service-list i {
    width: 0.56rem;
    height: 0.56rem;
    border: 1px solid var(--color-tobacco);
    background: var(--color-tobacco-muted);
  }

  .service-list i.healthy {
    border-color: var(--color-lichen);
    background: var(--color-lichen);
  }

  .docker-state {
    min-height: 11rem;
  }

  .docker-state > span {
    width: 2.8rem;
    height: 2.8rem;
    display: grid;
    flex: 0 0 auto;
    place-items: center;
    border: 1px solid var(--color-rule-strong);
    color: var(--color-ink-muted);
  }

  .docker-state > span.available {
    border-color: var(--color-lichen);
    color: var(--color-lichen);
  }

  .directory-view {
    margin-top: 1.25rem;
  }

  .directory-toolbar {
    display: grid;
    grid-template-columns: minmax(16rem, 0.75fr) minmax(0, 1.25fr);
    align-items: center;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding-bottom: 1rem;
  }

  .directory-toolbar--simple {
    grid-template-columns: minmax(16rem, 0.65fr) minmax(0, 1fr);
  }

  .directory-toolbar--simple > p {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.68rem;
    line-height: 1.5;
    text-align: right;
  }

  .filter-row {
    display: flex;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 0.35rem;
  }

  .filter-row button {
    min-height: 2.35rem;
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    border: 1px solid var(--color-rule);
    padding: 0.45rem 0.65rem;
    background: var(--color-surface);
    color: var(--color-ink-muted);
    cursor: pointer;
    font-size: 0.64rem;
    font-weight: 700;
  }

  .filter-row button.active {
    border-color: var(--color-umber);
    background: var(--color-umber);
    color: var(--color-surface);
  }

  .filter-row button.active span {
    border-color: color-mix(in srgb, var(--color-surface) 30%, transparent);
    background: color-mix(in srgb, var(--color-surface) 12%, transparent);
    color: inherit;
  }

  .approval-workspace {
    display: grid;
    grid-template-columns: minmax(18rem, 0.72fr) minmax(24rem, 1.28fr);
    border: 1px solid var(--color-rule-strong);
    margin-top: 1rem;
    background: var(--color-rule);
    gap: 1px;
  }

  .approval-list {
    max-height: 56rem;
    overflow: auto;
    background: var(--color-surface);
  }

  .approval-list > header {
    position: sticky;
    z-index: 1;
    top: 0;
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule-strong);
    padding: 0.7rem 0.8rem;
    background: var(--color-paper);
    color: var(--color-ink-muted);
    font-size: 0.58rem;
  }

  .approval-list > header p {
    margin: 0;
    font-weight: 700;
  }

  .approval-list > button {
    width: 100%;
    display: grid;
    justify-items: start;
    gap: 0.35rem;
    border: 0;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.9rem;
    background: var(--color-surface);
    color: var(--color-ink);
    cursor: pointer;
    text-align: left;
  }

  .approval-list > button:hover,
  .approval-list > button.active {
    background: var(--color-surface-muted);
  }

  .approval-list > button.active {
    box-shadow: inset 3px 0 0 var(--color-clay);
  }

  .approval-list__status {
    width: 100%;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
  }

  .approval-list__status small {
    color: var(--color-ink-muted);
    font-size: 0.58rem;
  }

  .approval-list > button > strong {
    font-family: var(--font-editorial);
    font-size: 1.04rem;
    font-weight: 500;
  }

  .approval-list > button > p {
    display: -webkit-box;
    overflow: hidden;
    margin: 0;
    color: var(--color-ink-soft);
    font-size: 0.66rem;
    line-height: 1.5;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    line-clamp: 2;
  }

  .approval-detail {
    min-width: 0;
    background: var(--color-surface);
  }

  .approval-detail > header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 1.35rem;
  }

  .approval-detail header > div {
    min-width: 0;
  }

  .approval-detail header > div > span {
    display: block;
    margin-top: 0.55rem;
    color: var(--color-ink-muted);
    font-size: 0.73rem;
    line-height: 1.55;
  }

  .approval-facts {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 1px;
    margin: 0;
    background: var(--color-rule);
  }

  .approval-facts div {
    padding: 0.75rem 1rem;
    background: var(--color-paper);
  }

  .approval-facts dt,
  .jobs-directory dt,
  .worker-grid dt,
  .container-grid dt {
    color: var(--color-ink-muted);
    font-size: 0.55rem;
    font-weight: 750;
    letter-spacing: 0.09em;
    text-transform: uppercase;
  }

  .approval-facts dd,
  .jobs-directory dd,
  .worker-grid dd,
  .container-grid dd {
    margin: 0.25rem 0 0;
    color: var(--color-ink-soft);
    font-size: 0.66rem;
    overflow-wrap: anywhere;
  }

  .approval-section {
    border-bottom: 1px solid var(--color-rule);
    padding: 1rem 1.35rem 1.25rem;
  }

  .approval-section > p {
    margin: 0 0 0.65rem;
    color: var(--color-clay);
    font-size: 0.56rem;
    font-weight: 800;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .effect-list {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
  }

  .approval-detail > footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 1rem 1.35rem;
  }

  .detail-links,
  .approval-actions,
  .job-links,
  .worker-grid footer > div,
  .schedule-actions,
  .container-grid footer > div {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.45rem;
  }

  .jobs-directory {
    display: grid;
    gap: 1px;
    border: 1px solid var(--color-rule-strong);
    margin-top: 1rem;
    background: var(--color-rule);
  }

  .jobs-directory > header {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.7rem 0.85rem;
    background: var(--color-paper);
    color: var(--color-ink-muted);
    font-size: 0.62rem;
  }

  .jobs-directory article {
    min-width: 0;
    display: grid;
    grid-template-columns: 9rem minmax(15rem, 1fr) minmax(12rem, 0.6fr) auto;
    align-items: center;
    gap: 1rem;
    padding: 0.9rem;
    background: var(--color-surface);
  }

  .jobs-directory article.needs-attention {
    box-shadow: inset 3px 0 0 var(--color-tobacco);
  }

  .job-state {
    display: grid;
    justify-items: start;
    gap: 0.45rem;
  }

  .job-progress {
    width: 100%;
    height: 0.22rem;
    background: var(--color-rule);
  }

  .job-progress i {
    display: block;
    height: 100%;
    background: var(--color-lichen);
  }

  .job-state > small {
    color: var(--color-ink-muted);
    font-size: 0.58rem;
  }

  .job-copy {
    min-width: 0;
  }

  .job-copy span {
    display: -webkit-box;
    overflow: hidden;
    margin-top: 0.3rem;
    color: var(--color-ink-muted);
    font-size: 0.66rem;
    line-height: 1.45;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    line-clamp: 2;
  }

  .jobs-directory dl,
  .worker-grid dl,
  .container-grid dl {
    display: grid;
    gap: 0.55rem;
    margin: 0;
  }

  .job-links {
    justify-content: flex-end;
  }

  .worker-grid,
  .container-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 1rem;
    margin-top: 1rem;
  }

  .worker-grid article,
  .container-grid article {
    min-width: 0;
    border: 1px solid var(--color-rule-strong);
    background: var(--color-surface);
  }

  .worker-grid article.needs-attention {
    border-top: 3px solid var(--color-tobacco);
  }

  .worker-grid header,
  .container-grid header {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: start;
    gap: 0.75rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 1rem;
  }

  .container-grid header {
    grid-template-columns: minmax(0, 1fr) auto;
  }

  .worker-grid header > span {
    width: 2.35rem;
    height: 2.35rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-tobacco);
    color: var(--color-tobacco);
  }

  .worker-grid header > span.healthy {
    border-color: var(--color-lichen);
    color: var(--color-lichen);
  }

  .worker-count {
    display: flex;
    align-items: baseline;
    gap: 0.55rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.9rem 1rem;
  }

  .worker-count strong {
    font-family: var(--font-editorial);
    font-size: 2rem;
    font-weight: 450;
  }

  .worker-count span {
    color: var(--color-ink-muted);
    font-size: 0.68rem;
  }

  .worker-grid dl,
  .container-grid dl {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    padding: 1rem;
  }

  .worker-grid footer,
  .container-grid footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    border-top: 1px solid var(--color-rule);
    padding: 0.8rem 1rem;
    background: var(--color-paper);
  }

  .worker-grid footer .danger-link {
    color: var(--color-danger);
  }

  .schedule-list {
    display: grid;
    gap: 1px;
    border: 1px solid var(--color-rule-strong);
    margin-top: 1rem;
    background: var(--color-rule);
  }

  .schedule-list article {
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(9rem, 0.45fr) minmax(16rem, 1fr) auto minmax(16rem, auto);
    align-items: center;
    gap: 1rem;
    padding: 1rem;
    background: var(--color-surface);
  }

  .schedule-date {
    display: grid;
    gap: 0.16rem;
  }

  .schedule-date small {
    color: var(--color-clay);
    font-size: 0.55rem;
    font-weight: 800;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .schedule-date strong {
    font-family: var(--font-editorial);
    font-size: 1.2rem;
    font-weight: 500;
  }

  .schedule-date span,
  .schedule-copy span {
    color: var(--color-ink-muted);
    font-size: 0.62rem;
    line-height: 1.45;
  }

  .schedule-copy {
    min-width: 0;
  }

  .schedule-copy span {
    display: block;
    overflow: hidden;
    margin-top: 0.28rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .schedule-actions {
    justify-content: flex-end;
  }

  .docker-unavailable {
    min-height: 25rem;
    display: grid;
    grid-template-columns: auto minmax(0, 34rem);
    align-content: center;
    justify-content: center;
    gap: 1.25rem;
    border-block: 1px solid var(--color-rule);
    margin-top: 1rem;
    padding: 3rem 1rem;
    background: color-mix(in srgb, var(--color-surface) 78%, transparent);
  }

  .docker-unavailable > span {
    width: 3.4rem;
    height: 3.4rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-rule-strong);
    color: var(--color-ink-muted);
  }

  .docker-unavailable > div > p:not(:first-child) {
    margin: 0.7rem 0 0;
    color: var(--color-ink-muted);
    font-size: 0.76rem;
    line-height: 1.6;
  }

  .docker-unavailable > div > div {
    display: flex;
    flex-wrap: wrap;
    gap: 0.7rem;
    margin-top: 1rem;
  }

  .container-grid header h2 {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .updated-reading {
    margin: 1rem 0 0;
    color: var(--color-ink-muted);
    font-size: 0.58rem;
    text-align: right;
  }

  .typed-confirmation {
    display: grid;
    gap: 0.55rem;
  }

  .typed-confirmation span {
    color: var(--color-ink-soft);
    font-size: 0.75rem;
  }

  .typed-confirmation code {
    color: var(--color-danger);
    font-family: var(--font-mono);
    font-size: 0.72rem;
  }

  .typed-confirmation input {
    min-height: 2.55rem;
    border: 1px solid var(--color-rule-strong);
    border-radius: 0;
    padding: 0.55rem 0.65rem;
    background: var(--color-surface);
    color: var(--color-ink);
    font-family: var(--font-mono);
    outline: none;
  }

  .typed-confirmation input:focus {
    border-color: var(--color-focus);
    box-shadow: inset 3px 0 0 var(--color-focus);
  }

  .dialog-note {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.76rem;
    line-height: 1.6;
  }

  @keyframes loading {
    to {
      background-position: -200% 0;
    }
  }

  @media (max-width: 78rem) {
    .jobs-directory article {
      grid-template-columns: 8rem minmax(0, 1fr) auto;
    }

    .jobs-directory article dl {
      display: none;
    }

    .schedule-list article {
      grid-template-columns: minmax(8rem, 0.45fr) minmax(0, 1fr) auto;
    }

    .schedule-actions {
      grid-column: 1 / -1;
      justify-content: flex-end;
      border-top: 1px solid var(--color-rule);
      padding-top: 0.7rem;
    }
  }

  @media (max-width: 58rem) {
    .summary-grid {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .overview-grid,
    .worker-grid,
    .container-grid {
      grid-template-columns: 1fr;
    }

    .approval-workspace {
      grid-template-columns: minmax(15rem, 0.62fr) minmax(20rem, 1fr);
      overflow-x: auto;
    }

    .directory-toolbar,
    .directory-toolbar--simple {
      grid-template-columns: 1fr;
    }

    .filter-row {
      justify-content: flex-start;
    }

    .directory-toolbar--simple > p {
      text-align: left;
    }
  }

  @media (max-width: 42rem) {
    .operations-page {
      padding-inline: 1rem;
    }

    .summary-grid {
      grid-template-columns: 1fr 1fr;
    }

    .summary-grid button,
    .summary-reading {
      padding: 0.9rem;
    }

    .summary-grid strong,
    .summary-reading strong {
      font-size: 1.8rem;
    }

    .approval-workspace {
      display: block;
      overflow: visible;
    }

    .approval-list {
      max-height: 22rem;
      border-bottom: 1px solid var(--color-rule-strong);
    }

    .approval-detail > header,
    .approval-detail > footer,
    .worker-grid footer,
    .container-grid footer {
      align-items: stretch;
      flex-direction: column;
    }

    .approval-facts,
    .worker-grid dl,
    .container-grid dl {
      grid-template-columns: 1fr;
    }

    .jobs-directory article {
      grid-template-columns: 1fr;
    }

    .job-links {
      justify-content: flex-start;
    }

    .schedule-list article {
      grid-template-columns: 1fr;
    }

    .schedule-actions {
      grid-column: auto;
      justify-content: flex-start;
    }

    .docker-unavailable {
      grid-template-columns: 1fr;
      justify-items: start;
    }
  }
</style>
