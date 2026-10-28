<script lang="ts">
  import { onMount } from "svelte";
  import type {
    FrontendApiClient,
    FrontendUsageDashboard,
  } from "@carlina/sdk";
  import { Button, Icon } from "@carlina/ui";

  import { usageDailyMaximum } from "../../../lib/administration.ts";

  interface Props {
    api: FrontendApiClient;
    usageUrl: string;
  }

  let { api, usageUrl }: Props = $props();

  const now = new Date();
  const initialEnd = now.toISOString().slice(0, 10);
  const start = new Date(now);
  start.setUTCDate(start.getUTCDate() - 29);

  let startDate = $state(start.toISOString().slice(0, 10));
  let endDate = $state(initialEnd);
  let usage = $state<FrontendUsageDashboard | null>(null);
  let metric = $state<"cost" | "tokens" | "requests">("cost");
  let loading = $state(true);
  let error = $state("");

  const dailyMaximum = $derived(
    usage ? usageDailyMaximum(usage.daily, metric) : 0,
  );
  const sourceTotals = $derived.by(() => {
    const totals = new Map<
      string,
      {
        label: string;
        cost: number;
        tokens: number;
        requests: number;
      }
    >();
    for (const record of usage?.daily ?? []) {
      const existing = totals.get(record.source) ?? {
        label: record.source_label,
        cost: 0,
        tokens: 0,
        requests: 0,
      };
      existing.cost += record.cost;
      existing.tokens += record.tokens;
      existing.requests += record.requests;
      totals.set(record.source, existing);
    }
    return [...totals.values()].sort((left, right) => {
      if (metric === "cost") return right.cost - left.cost;
      if (metric === "tokens") return right.tokens - left.tokens;
      return right.requests - left.requests;
    });
  });

  onMount(() => {
    void loadUsage();
  });

  async function loadUsage(): Promise<void> {
    loading = true;
    error = "";
    try {
      const url = new URL(usageUrl, "https://iarf.local");
      url.searchParams.set("start_date", startDate);
      url.searchParams.set("end_date", endDate);
      usage = await api.request<FrontendUsageDashboard>(
        `${url.pathname}${url.search}`,
      );
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "The usage ledger could not be loaded.";
    } finally {
      loading = false;
    }
  }

  function formatCost(value: number): string {
    return new Intl.NumberFormat(undefined, {
      style: "currency",
      currency: "USD",
      minimumFractionDigits: value < 1 ? 3 : 2,
      maximumFractionDigits: value < 1 ? 4 : 2,
    }).format(value);
  }

  function metricLabel(
    value: { cost: number; tokens: number; requests: number },
  ): string {
    if (metric === "cost") return formatCost(value.cost);
    if (metric === "tokens") return value.tokens.toLocaleString();
    return value.requests.toLocaleString();
  }

  function dailyMetric(
    value: { cost: number; tokens: number; requests: number },
  ): number {
    return value[metric];
  }

  function formatDate(value: string): string {
    const date = new Date(`${value}T00:00:00Z`);
    if (Number.isNaN(date.valueOf())) return value;
    return new Intl.DateTimeFormat(undefined, {
      month: "short",
      day: "numeric",
      timeZone: "UTC",
    }).format(date);
  }
</script>

<section class="usage-panel" aria-labelledby="usage-heading">
  <header class="section-intro">
    <div>
      <p>Model ledger</p>
      <h2 id="usage-heading">Usage and cost</h2>
    </div>
    <span>
      Read bounded daily source totals and member-level usage without loading
      individual prompts, responses, or raw event records.
    </span>
  </header>

  <form
    class="range-toolbar"
    onsubmit={(event) => {
      event.preventDefault();
      void loadUsage();
    }}
  >
    <label for="usage-start-date">
      From
      <input
        id="usage-start-date"
        type="date"
        bind:value={startDate}
        max={endDate}
      />
    </label>
    <label for="usage-end-date">
      Through
      <input
        id="usage-end-date"
        type="date"
        bind:value={endDate}
        min={startDate}
        max={initialEnd}
      />
    </label>
    <Button type="submit" disabled={loading}>
      <Icon name="refresh" size={14} />
      {loading ? "Loading…" : "Apply range"}
    </Button>
    <span>Maximum range: {usage?.limits.maximum_days ?? 366} days</span>
  </form>

  {#if error}
    <p class="panel-message panel-message--error" role="alert">{error}</p>
  {:else if loading}
    <section
      class="usage-loading"
      aria-label="Loading model usage"
      aria-busy="true"
    >
      <span></span><span></span>
    </section>
  {:else if usage}
    <section class="usage-reading" aria-label="Usage summary">
      <article>
        <p>Estimated cost</p>
        <strong>{formatCost(usage.summary.cost)}</strong>
        <span>{usage.range.days} calendar days</span>
      </article>
      <article>
        <p>Tokens</p>
        <strong>{usage.summary.tokens.toLocaleString()}</strong>
        <span>prompt and completion combined</span>
      </article>
      <article>
        <p>Model requests</p>
        <strong>{usage.summary.requests.toLocaleString()}</strong>
        <span>across {usage.summary.active_members} active members</span>
      </article>
    </section>

    <div class="metric-toolbar" aria-label="Usage chart metric">
      <span>Read the ledger by</span>
      {#each [
        ["cost", "Cost"],
        ["tokens", "Tokens"],
        ["requests", "Requests"],
      ] as option}
        <button
          type="button"
          class:active={metric === option[0]}
          aria-pressed={metric === option[0]}
          onclick={() =>
            (metric = option[0] as "cost" | "tokens" | "requests")}
        >
          {option[1]}
        </button>
      {/each}
    </div>

    <div class="usage-grid">
      <section class="daily-ledger">
        <header>
          <div>
            <p>Daily source ledger</p>
            <h3>Where model work occurred</h3>
          </div>
          <span>{usage.daily.length} grouped readings</span>
        </header>
        {#if usage.daily.length}
          <div class="daily-list">
            {#each usage.daily as record, index (
              `${record.date}:${record.source}:${index}`
            )}
              <article>
                <time datetime={record.date}>{formatDate(record.date)}</time>
                <div>
                  <strong>{record.source_label}</strong>
                  <span>
                    {record.requests.toLocaleString()} requests ·
                    {record.tokens.toLocaleString()} tokens
                  </span>
                </div>
                <div class="bar-reading">
                  <em>{metricLabel(record)}</em>
                  <span>
                    <i
                      style={`width: ${
                        dailyMaximum
                          ? Math.max(
                              2,
                              (dailyMetric(record) / dailyMaximum) * 100,
                            )
                          : 0
                      }%`}
                    ></i>
                  </span>
                </div>
              </article>
            {/each}
          </div>
        {:else}
          <div class="empty-ledger">
            <Icon name="activity" size={23} />
            <h3>No model use in this range</h3>
            <p>Choose a broader date range to inspect earlier activity.</p>
          </div>
        {/if}
      </section>

      <aside class="source-summary">
        <p>Source index</p>
        <h3>Activity by origin</h3>
        {#if sourceTotals.length}
          <ol>
            {#each sourceTotals as source, index (source.label)}
              <li>
                <span>{String(index + 1).padStart(2, "0")}</span>
                <strong>{source.label}</strong>
                <em>{metricLabel(source)}</em>
              </li>
            {/each}
          </ol>
        {:else}
          <p class="quiet-copy">No sources recorded in this range.</p>
        {/if}
      </aside>
    </div>

    <section class="member-ledger">
      <header>
        <div>
          <p>Member ledger</p>
          <h3>Who used model capacity</h3>
        </div>
        <span>Highest estimated cost first</span>
      </header>
      {#if usage.by_user.length}
        <div class="member-table" role="table" aria-label="Usage by member">
          <div class="table-head" role="row">
            <span role="columnheader">Member</span>
            <span role="columnheader">Requests</span>
            <span role="columnheader">Tokens</span>
            <span role="columnheader">Cost</span>
          </div>
          {#each usage.by_user as member (member.id)}
            <div role="row">
              <div role="cell"><strong>{member.email}</strong></div>
              <span role="cell">{member.requests.toLocaleString()}</span>
              <span role="cell">{member.tokens.toLocaleString()}</span>
              <span role="cell">{formatCost(member.cost)}</span>
            </div>
          {/each}
        </div>
      {:else}
        <p class="quiet-copy">No member-attributed usage in this range.</p>
      {/if}
    </section>
  {/if}
</section>

<style>
  .usage-panel {
    border-inline: 1px solid var(--color-rule-strong);
    border-bottom: 1px solid var(--color-rule-strong);
    background: var(--color-paper);
  }

  .section-intro {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(16rem, 0.6fr);
    gap: 2rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 1.4rem;
  }

  .section-intro p,
  .section-intro h2,
  .section-intro > span {
    margin: 0;
  }

  .section-intro p,
  .daily-ledger header p,
  .source-summary > p:first-child,
  .member-ledger header p {
    color: var(--color-clay);
    font-size: 0.58rem;
    font-weight: 800;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  .section-intro h2 {
    margin-top: 0.25rem;
    font-family: var(--font-editorial);
    font-size: clamp(1.4rem, 2.7vw, 2rem);
    font-weight: 520;
  }

  .section-intro > span {
    align-self: end;
    color: var(--color-ink-muted);
    font-size: 0.73rem;
    line-height: 1.6;
  }

  .range-toolbar {
    display: grid;
    grid-template-columns: repeat(2, minmax(9rem, 0.22fr)) auto 1fr;
    align-items: end;
    gap: 0.7rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.8rem 1rem;
    background: var(--color-surface);
  }

  .range-toolbar label {
    display: grid;
    gap: 0.3rem;
    color: var(--color-ink-muted);
    font-size: 0.61rem;
    font-weight: 700;
  }

  .range-toolbar input {
    min-height: 2.5rem;
    border: 1px solid var(--color-rule);
    padding: 0.55rem 0.65rem;
    background: var(--color-paper);
    color: var(--color-ink);
    font: inherit;
  }

  .range-toolbar > span {
    align-self: center;
    justify-self: end;
    color: var(--color-ink-muted);
    font-size: 0.61rem;
  }

  .panel-message {
    margin: 0;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.8rem 1.4rem;
    font-size: 0.7rem;
  }

  .panel-message--error {
    color: var(--color-danger);
  }

  .usage-loading {
    display: grid;
    gap: 1px;
    background: var(--color-rule);
  }

  .usage-loading span {
    height: 10rem;
    background: linear-gradient(
      90deg,
      var(--color-paper),
      var(--color-surface),
      var(--color-paper)
    );
    background-size: 200% 100%;
    animation: loading 1.5s ease-in-out infinite;
  }

  @keyframes loading {
    to {
      background-position: -200% 0;
    }
  }

  .usage-reading {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    border-bottom: 1px solid var(--color-rule-strong);
    background: var(--color-surface);
  }

  .usage-reading article {
    display: grid;
    gap: 0.2rem;
    border-right: 1px solid var(--color-rule);
    padding: 0.9rem 1rem;
  }

  .usage-reading article:last-child {
    border-right: 0;
  }

  .usage-reading p,
  .usage-reading span {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.63rem;
  }

  .usage-reading strong {
    font-family: var(--font-editorial);
    font-size: 1.35rem;
    font-weight: 540;
  }

  .metric-toolbar {
    display: flex;
    align-items: center;
    gap: 0;
    border-bottom: 1px solid var(--color-rule);
    padding-left: 1rem;
  }

  .metric-toolbar > span {
    margin-right: 0.8rem;
    color: var(--color-ink-muted);
    font-size: 0.61rem;
  }

  .metric-toolbar button {
    min-height: 2.7rem;
    border: 0;
    border-inline-start: 1px solid var(--color-rule);
    padding: 0.6rem 0.9rem;
    cursor: pointer;
    background: transparent;
    color: var(--color-ink-muted);
    font-size: 0.66rem;
  }

  .metric-toolbar button:last-child {
    border-inline-end: 1px solid var(--color-rule);
  }

  .metric-toolbar button.active {
    background: var(--color-clay-muted);
    box-shadow: inset 0 -3px 0 var(--color-clay);
    color: var(--color-ink);
  }

  .usage-grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(15rem, 0.28fr);
    border-bottom: 1px solid var(--color-rule-strong);
  }

  .daily-ledger {
    min-width: 0;
    border-right: 1px solid var(--color-rule-strong);
  }

  .daily-ledger header,
  .member-ledger header {
    display: flex;
    align-items: end;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.9rem 1rem;
  }

  .daily-ledger header p,
  .daily-ledger header h3,
  .member-ledger header p,
  .member-ledger header h3 {
    margin: 0;
  }

  .daily-ledger header h3,
  .member-ledger header h3,
  .source-summary h3 {
    margin-top: 0.2rem;
    font-family: var(--font-editorial);
    font-size: 1.2rem;
    font-weight: 520;
  }

  .daily-ledger header > span,
  .member-ledger header > span {
    color: var(--color-ink-muted);
    font-size: 0.6rem;
  }

  .daily-list article {
    display: grid;
    grid-template-columns: 4rem minmax(9rem, 0.4fr) minmax(8rem, 0.6fr);
    align-items: center;
    gap: 0.8rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.72rem 1rem;
  }

  .daily-list article:last-child {
    border-bottom: 0;
  }

  .daily-list time {
    color: var(--color-ink-muted);
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.59rem;
  }

  .daily-list article > div:nth-of-type(1) {
    min-width: 0;
    display: grid;
    gap: 0.2rem;
  }

  .daily-list strong {
    overflow-wrap: anywhere;
    font-size: 0.69rem;
  }

  .daily-list article > div:nth-of-type(1) span {
    color: var(--color-ink-muted);
    font-size: 0.59rem;
  }

  .bar-reading {
    min-width: 0;
    display: grid;
    gap: 0.28rem;
  }

  .bar-reading em {
    justify-self: end;
    font-size: 0.64rem;
    font-style: normal;
    font-weight: 700;
  }

  .bar-reading > span {
    height: 4px;
    background: var(--color-rule);
  }

  .bar-reading i {
    display: block;
    height: 100%;
    background: var(--color-clay);
  }

  .source-summary {
    min-width: 0;
    padding: 1.1rem;
    background:
      linear-gradient(145deg, var(--color-clay-muted), transparent 62%),
      var(--color-surface);
  }

  .source-summary > p:first-child,
  .source-summary h3 {
    margin: 0;
  }

  .source-summary ol {
    margin: 1rem 0 0;
    padding: 0;
    list-style: none;
    border-block: 1px solid var(--color-rule);
  }

  .source-summary li {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    gap: 0.55rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.65rem 0;
    font-size: 0.63rem;
  }

  .source-summary li:last-child {
    border-bottom: 0;
  }

  .source-summary li span {
    color: var(--color-clay);
    font-family: ui-monospace, "Cascadia Code", monospace;
  }

  .source-summary li strong {
    overflow-wrap: anywhere;
  }

  .source-summary li em {
    font-style: normal;
    text-align: right;
  }

  .member-ledger {
    min-width: 0;
  }

  .member-table > div {
    display: grid;
    grid-template-columns: minmax(12rem, 1fr) repeat(3, minmax(6rem, 0.25fr));
    gap: 0.7rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.7rem 1rem;
  }

  .member-table > div:last-child {
    border-bottom: 0;
  }

  .member-table span,
  .member-table strong {
    overflow-wrap: anywhere;
    font-size: 0.65rem;
  }

  .member-table span:not(:first-child) {
    text-align: right;
  }

  .table-head {
    background: var(--color-surface);
    color: var(--color-ink-muted);
    font-size: 0.58rem;
    font-weight: 800;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .empty-ledger {
    display: grid;
    justify-items: start;
    gap: 0.4rem;
    min-height: 14rem;
    align-content: center;
    padding: 2rem;
  }

  .empty-ledger h3,
  .empty-ledger p,
  .quiet-copy {
    margin: 0;
  }

  .empty-ledger h3 {
    font-family: var(--font-editorial);
    font-size: 1.2rem;
    font-weight: 520;
  }

  .empty-ledger p,
  .quiet-copy {
    color: var(--color-ink-muted);
    font-size: 0.68rem;
  }

  .quiet-copy {
    padding: 1rem;
  }

  @media (max-width: 68rem) {
    .usage-grid {
      grid-template-columns: 1fr;
    }

    .daily-ledger {
      border-right: 0;
      border-bottom: 1px solid var(--color-rule-strong);
    }
  }

  @media (max-width: 50rem) {
    .section-intro,
    .usage-reading {
      grid-template-columns: 1fr;
    }

    .section-intro {
      gap: 0.75rem;
    }

    .range-toolbar {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .range-toolbar > span {
      justify-self: start;
    }

    .usage-reading article,
    .usage-reading article:last-child {
      border-right: 0;
      border-bottom: 1px solid var(--color-rule);
    }

    .daily-list article {
      grid-template-columns: 3.5rem minmax(0, 1fr);
    }

    .bar-reading {
      grid-column: 1 / -1;
    }

    .member-table {
      overflow-x: auto;
    }

    .member-table > div {
      min-width: 38rem;
    }
  }
</style>
