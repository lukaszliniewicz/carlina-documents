<script lang="ts">
  import { onMount } from "svelte";
  import type {
    FrontendApiClient,
    FrontendBootstrap,
    FrontendIntelligenceSettingsDashboard,
    FrontendSettingsMutationResponse,
  } from "@carlina/sdk";
  import { Button, Dialog, EmptyState, Icon, PageHeader } from "@carlina/ui";

  interface Props {
    api: FrontendApiClient;
    bootstrap: FrontendBootstrap;
  }

  let { api, bootstrap }: Props = $props();

  let dashboard = $state<FrontendIntelligenceSettingsDashboard | null>(null);
  let loading = $state(true);
  let saving = $state(false);
  let reviewOpen = $state(false);
  let error = $state("");
  let notice = $state("");
  let fastModel = $state("");
  let mediumModel = $state("");
  let powerfulModel = $state("");

  const isAdmin = $derived(bootstrap.user.is_admin);
  const sharedRoutingModels = $derived(
    (dashboard?.routing.models ?? []).filter(
      (model) => model.scope === "global" && model.active && model.type === "chat",
    ),
  );
  const canManageDefaults = $derived(
    Boolean(dashboard?.routing.can_manage_defaults && isAdmin),
  );
  const routingRoles = $derived([
    {
      key: "fast",
      label: "Fast",
      description: "Routine extraction, classification, and short drafting.",
      value: fastModel,
    },
    {
      key: "medium",
      label: "Balanced",
      description: "Everyday research, analysis, and substantial writing.",
      value: mediumModel,
    },
    {
      key: "powerful",
      label: "Powerful",
      description: "Complex planning, long synthesis, and difficult reasoning.",
      value: powerfulModel,
    },
  ] as const);

  onMount(() => {
    void loadDashboard();
  });

  async function loadDashboard(): Promise<void> {
    loading = true;
    error = "";
    try {
      const payload = await api.request<FrontendIntelligenceSettingsDashboard>(
        "/api/frontend/settings/intelligence",
      );
      dashboard = payload;
      fastModel = payload.routing.defaults.fast;
      mediumModel = payload.routing.defaults.medium;
      powerfulModel = payload.routing.defaults.powerful;
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "Shared routing could not be loaded.";
    } finally {
      loading = false;
    }
  }

  function updateRole(
    role: "fast" | "medium" | "powerful",
    value: string,
  ): void {
    if (role === "fast") fastModel = value;
    if (role === "medium") mediumModel = value;
    if (role === "powerful") powerfulModel = value;
  }

  async function saveRouting(): Promise<void> {
    if (!dashboard) return;
    saving = true;
    error = "";
    notice = "";
    try {
      const response = await api.request<FrontendSettingsMutationResponse>(
        dashboard.links.preferences,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            default_models: {
              fast: fastModel,
              medium: mediumModel,
              powerful: powerfulModel,
            },
          }),
        },
      );
      reviewOpen = false;
      notice = response.message || "Shared routing saved.";
      await loadDashboard();
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "Shared routing could not be saved.";
    } finally {
      saving = false;
    }
  }

  function modelName(modelId: string): string {
    if (!modelId) return "No shared default";
    const model = sharedRoutingModels.find((item) => item.id === modelId);
    return model ? `${model.name} · ${model.provider_name}` : modelId;
  }
</script>

<div class="routing-settings-page">
  <PageHeader
    eyebrow="Settings / Shared intelligence"
    title="Model routing"
    description="Set installation-wide defaults for Fast, Balanced, and Powerful work. They describe workload roles, never a selected provider."
  />

  {#if error}
    <p class="routing-message routing-message--error" role="alert">{error}</p>
  {:else if notice}
    <p class="routing-message" role="status">
      <Icon name="check" size={14} />
      {notice}
    </p>
  {/if}

  {#if loading}
    <section class="routing-loading" aria-label="Loading shared routing" aria-busy="true">
      <span></span><span></span><span></span>
    </section>
  {:else if dashboard}
    <section class="routing-summary" aria-label="Shared routing summary">
      <article>
        <p>Available defaults</p>
        <strong>{sharedRoutingModels.length}</strong>
        <span>active shared chat models</span>
      </article>
      <article>
        <p>Management</p>
        <strong>{canManageDefaults ? "Admin" : "Read-only"}</strong>
        <span>routing is installation-wide</span>
      </article>
      <article>
        <p>Current roles</p>
        <strong>{routingRoles.filter((role) => role.value).length}/3</strong>
        <span>with a selected default</span>
      </article>
    </section>

    {#if canManageDefaults}
      <section class="routing-panel">
        <div class="section-heading">
          <div>
            <p>Shared model routing</p>
            <h2>Fast, balanced, and powerful defaults</h2>
            <span>
              New work resolves these roles to an active shared chat model. Existing
              runs retain the model recorded when they started.
            </span>
          </div>
        </div>
        {#if sharedRoutingModels.length}
          <div class="routing-grid">
            {#each routingRoles as role (role.key)}
              <label>
                <span>{role.label}</span>
                <small>{role.description}</small>
                <select
                  value={role.value}
                  aria-label={`${role.label} shared model`}
                  onchange={(event) => updateRole(role.key, event.currentTarget.value)}
                >
                  <option value="">No shared default</option>
                  {#each sharedRoutingModels as model (model.id)}
                    <option value={model.id}>{model.name} · {model.provider_name}</option>
                  {/each}
                </select>
              </label>
            {/each}
          </div>
          <footer>
            <p>Only active shared chat models can become platform defaults.</p>
            <Button onclick={() => (reviewOpen = true)}>Review routing change</Button>
          </footer>
        {:else}
          <EmptyState
            title="No active shared chat models"
            description="Activate a shared chat model in Models before assigning a routing role."
            icon="brain"
          />
        {/if}
      </section>
    {:else}
      <section class="read-only-panel">
        <Icon name="shield" size={21} />
        <div>
          <p>Shared routing</p>
          <h2>Administrator-managed defaults</h2>
          <span>
            You can use personal models where available, but only administrators
            can change installation-wide Fast, Balanced, and Powerful defaults.
          </span>
          <dl>
            {#each routingRoles as role (role.key)}
              <div>
                <dt>{role.label}</dt>
                <dd>{modelName(role.value)}</dd>
              </div>
            {/each}
          </dl>
        </div>
      </section>
    {/if}
  {/if}
</div>

<Dialog
  open={reviewOpen}
  title="Save shared routing?"
  description="Review the role-to-model mapping before changing defaults for future work."
  onclose={() => {
    if (!saving) reviewOpen = false;
  }}
>
  <dl class="review-list">
    {#each routingRoles as role (role.key)}
      <div>
        <dt>{role.label}</dt>
        <dd>{modelName(role.value)}</dd>
      </div>
    {/each}
  </dl>
  {#snippet footer()}
    <Button variant="quiet" onclick={() => (reviewOpen = false)} disabled={saving}>
      Keep current settings
    </Button>
    <Button onclick={() => void saveRouting()} disabled={saving}>
      {saving ? "Saving…" : "Save shared routing"}
    </Button>
  {/snippet}
</Dialog>

<style>
  .routing-settings-page {
    width: min(calc(100% - 2rem), 92rem);
    margin: 0 auto;
    padding: 2rem 0 4rem;
  }

  .routing-message {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    margin: 1rem 0;
    border-left: 2px solid var(--color-lichen);
    padding: 0.7rem 0.85rem;
    background: var(--color-lichen-muted);
    color: var(--color-lichen);
    font-size: 0.72rem;
  }

  .routing-message--error {
    border-color: var(--color-danger);
    background: var(--color-danger-muted);
    color: var(--color-danger);
  }

  .routing-loading {
    min-height: 20rem;
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 1px;
    background: var(--color-rule);
  }

  .routing-loading span {
    min-height: 16rem;
    background: linear-gradient(100deg, var(--color-surface), var(--color-paper), var(--color-surface));
    background-size: 200% 100%;
    animation: sweep 1.4s infinite linear;
  }

  .routing-summary {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 1px;
    border: 1px solid var(--color-rule-strong);
    margin-top: 1.25rem;
    background: var(--color-rule);
  }

  .routing-summary article {
    min-width: 0;
    padding: 1rem 1.1rem;
    background: var(--color-surface);
  }

  .routing-summary p,
  .section-heading p,
  .read-only-panel > div > p {
    margin: 0;
    color: var(--color-clay);
    font-size: 0.57rem;
    font-weight: 800;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  .routing-summary strong {
    display: block;
    margin-top: 0.35rem;
    font-family: var(--font-editorial);
    font-size: 1.7rem;
    font-weight: 520;
  }

  .routing-summary span,
  .section-heading span,
  .read-only-panel > div > span {
    display: block;
    margin-top: 0.25rem;
    color: var(--color-ink-muted);
    font-size: 0.68rem;
    line-height: 1.55;
  }

  .routing-panel,
  .read-only-panel {
    border: 1px solid var(--color-rule-strong);
    margin-top: 1.25rem;
    background: var(--color-surface);
  }

  .section-heading {
    border-bottom: 1px solid var(--color-rule);
    padding: 1.35rem clamp(1rem, 3vw, 1.8rem);
  }

  .section-heading h2,
  .read-only-panel h2 {
    margin: 0.25rem 0 0;
    font-family: var(--font-editorial);
    font-size: clamp(1.55rem, 3vw, 2.1rem);
    font-weight: 520;
  }

  .routing-grid {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 1px;
    background: var(--color-rule);
  }

  .routing-grid label {
    display: grid;
    gap: 0.45rem;
    padding: 1.15rem;
    background: var(--color-surface);
    color: var(--color-ink-muted);
    font-size: 0.6rem;
    font-weight: 700;
    text-transform: uppercase;
  }

  .routing-grid label > span {
    color: var(--color-clay);
    font-family: var(--font-editorial);
    font-size: 1.35rem;
    font-weight: 540;
    text-transform: none;
  }

  .routing-grid small {
    min-height: 2.7rem;
    color: var(--color-ink-muted);
    font-size: 0.68rem;
    font-weight: 400;
    line-height: 1.5;
    text-transform: none;
  }

  select {
    width: 100%;
    border: 1px solid var(--color-rule);
    padding: 0.65rem 0.7rem;
    background: var(--color-paper);
    color: var(--color-ink);
    font: inherit;
    text-transform: none;
  }

  select:focus {
    border-color: var(--color-focus);
    outline: 2px solid var(--color-focus);
    outline-offset: 2px;
  }

  footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    border-top: 1px solid var(--color-rule);
    padding: 1rem 1.15rem;
    background: var(--color-paper);
  }

  footer p {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.67rem;
  }

  .read-only-panel {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 0.9rem;
    padding: 1.2rem;
  }

  .read-only-panel > :global(svg) {
    color: var(--color-clay);
  }

  .read-only-panel dl,
  .review-list {
    margin: 1rem 0 0;
  }

  .read-only-panel dl > div,
  .review-list > div {
    display: grid;
    grid-template-columns: 6rem minmax(0, 1fr);
    gap: 0.75rem;
    border-top: 1px solid var(--color-rule);
    padding: 0.65rem 0;
  }

  .read-only-panel dt,
  .review-list dt {
    color: var(--color-ink-muted);
    font-size: 0.6rem;
    font-weight: 700;
    text-transform: uppercase;
  }

  .read-only-panel dd,
  .review-list dd {
    margin: 0;
    overflow-wrap: anywhere;
    color: var(--color-ink-soft);
    font-size: 0.72rem;
  }

  .review-list {
    padding: 0;
  }

  @keyframes sweep {
    from { background-position: 200% 0; }
    to { background-position: -200% 0; }
  }

  @media (max-width: 62rem) {
    .routing-grid {
      grid-template-columns: 1fr;
    }
  }

  @media (max-width: 44rem) {
    .routing-settings-page {
      width: min(calc(100% - 1.5rem), 92rem);
      padding-top: 1rem;
    }

    .routing-summary {
      grid-template-columns: 1fr;
    }

    footer {
      align-items: stretch;
      flex-direction: column;
    }

    footer :global(.ui-button) {
      width: 100%;
    }
  }
</style>
