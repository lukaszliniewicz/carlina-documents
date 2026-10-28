<script lang="ts">
  import type { FrontendAddonManagerSummary } from "@carlina/sdk";
  import { Badge, Icon } from "@carlina/ui";

  import {
    addonNeedsAttention,
    addonStatusLabel,
    addonStatusTone,
    readableAddonLabel,
  } from "../../../lib/addon-manager.ts";

  interface Props {
    addons: readonly FrontendAddonManagerSummary[];
    totalCount: number;
    visibleAddons: readonly FrontendAddonManagerSummary[];
    selectedId: string;
    hasFilters: boolean;
    batchSize: number;
    onselect: (addonId: string) => void;
    onclear: () => void;
    onshowmore: () => void;
  }

  let {
    addons,
    totalCount,
    visibleAddons,
    selectedId,
    hasFilters,
    batchSize,
    onselect,
    onclear,
    onshowmore,
  }: Props = $props();
</script>

<section class="addon-directory" aria-label="Installed addons">
  <header>
    <div>
      <p>Directory</p>
      <h2>{addons.length} of {totalCount}</h2>
    </div>
    {#if hasFilters}
      <button class="clear-filters" onclick={onclear}>Clear filters</button>
    {/if}
  </header>

  <div class="addon-list">
    {#each visibleAddons as addon, index (addon.id)}
      <button
        class="addon-row"
        class:selected={selectedId === addon.id}
        aria-pressed={selectedId === addon.id}
        onclick={() => onselect(addon.id)}
      >
        <span class="addon-index">{String(index + 1).padStart(2, "0")}</span>
        <span
          class="status-rule"
          class:status-rule--active={addon.is_active && !addonNeedsAttention(addon)}
          class:status-rule--attention={addonNeedsAttention(addon)}
        ></span>
        <span class="addon-row__copy">
          <strong>{addon.name}</strong>
          <small>{addon.id} · v{addon.version}</small>
          <span>
            {#if addon.kind}
              <em>{readableAddonLabel(addon.kind)}</em>
            {/if}
            {#each addon.roles.slice(0, addon.kind ? 1 : 2) as role}
              <em>{readableAddonLabel(role)}</em>
            {/each}
          </span>
        </span>
        <span class="addon-row__runtime">
          <Badge tone={addonStatusTone(addon)}>{addonStatusLabel(addon)}</Badge>
          {#if addon.worker_running}
            <small>{addon.worker_running} worker{addon.worker_running === 1 ? "" : "s"}</small>
          {/if}
        </span>
      </button>
    {:else}
      <div class="no-results">
        <Icon name="search" size={20} />
        <strong>No addon matches this view</strong>
        <p>Try a broader search or clear one of the filters.</p>
      </div>
    {/each}
    {#if visibleAddons.length < addons.length}
      <button class="show-more-addons" onclick={onshowmore}>
        Show {Math.min(batchSize, addons.length - visibleAddons.length)} more
        <span>{visibleAddons.length} of {addons.length} rendered</span>
      </button>
    {/if}
  </div>
</section>
