<script lang="ts">
  import { onMount } from "svelte";
  import type { FrontendAddonDoctorReport } from "@carlina/sdk";
  import { Button, Icon, PageHeader } from "@carlina/ui";

  import type {
    AddonDirectoryStatus,
    AddonDirectorySummary,
  } from "../../../lib/addon-manager.ts";

  interface Props {
    summary: AddonDirectorySummary;
    statusFilter: AddonDirectoryStatus;
    kindFilter: string;
    doctor: FrontendAddonDoctorReport | null;
    doctorLoading: boolean;
    refreshing: boolean;
    restartRequired: boolean;
    isAdmin: boolean;
    ondoctor: () => void;
    onrefresh: () => void;
    oncatalog: () => void;
    oninstall: () => void;
    onrestart: () => void;
  }

  let {
    summary,
    statusFilter = $bindable(),
    kindFilter = $bindable(),
    doctor,
    doctorLoading,
    refreshing,
    restartRequired,
    isAdmin,
    ondoctor,
    onrefresh,
    oncatalog,
    oninstall,
    onrestart,
  }: Props = $props();
  let packageViewsElement = $state<HTMLElement>();

  const packageViews = [
    ["all", "All"],
    ["application", "Applications"],
    ["toolkit", "Toolkits"],
    ["connector", "Connections"],
    ["engine", "Engines"],
    ["component", "Advanced"],
  ] as const;

  function revealActivePackageView(): void {
    packageViewsElement
      ?.querySelector<HTMLButtonElement>("button.active")
      ?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  function scrollPackageViews(event: KeyboardEvent): void {
    if (
      !packageViewsElement ||
      !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)
    ) {
      return;
    }
    event.preventDefault();
    if (event.key === "Home" || event.key === "End") {
      packageViewsElement.scrollTo({
        left: event.key === "Home" ? 0 : packageViewsElement.scrollWidth,
        behavior: "smooth",
      });
      return;
    }
    packageViewsElement.scrollBy({
      left:
        (event.key === "ArrowLeft" ? -1 : 1) *
        packageViewsElement.clientWidth *
        0.7,
      behavior: "smooth",
    });
  }

  onMount(() => {
    const frame = window.requestAnimationFrame(revealActivePackageView);
    return () => window.cancelAnimationFrame(frame);
  });

  $effect(() => {
    kindFilter;
    if (typeof window === "undefined") return;
    const frame = window.requestAnimationFrame(revealActivePackageView);
    return () => window.cancelAnimationFrame(frame);
  });
</script>

<PageHeader
  eyebrow="System / Capability registry"
  title="Addon desk"
  description="Inspect what is installed, understand relationships and runtime health, and make deliberate lifecycle changes without reading manifest JSON."
>
  {#snippet actions()}
    <Button variant="outline" disabled={doctorLoading} onclick={ondoctor}>
      <Icon name="activity" size={15} />
      {doctorLoading ? "Checking…" : "Run Doctor"}
    </Button>
    {#if isAdmin}
      <Button variant="outline" disabled={refreshing} onclick={onrefresh}>
        <Icon name="refresh" size={15} />
        {refreshing ? "Refreshing…" : "Refresh registry"}
      </Button>
      <Button variant="outline" onclick={oncatalog}>
        <Icon name="download" size={15} />
        Browse catalog
      </Button>
      <Button onclick={oninstall}>
        <Icon name="plus" size={15} />
        Install local ZIP
      </Button>
    {/if}
  {/snippet}
</PageHeader>

<section class="registry-summary" aria-label="Addon summary">
  <button
    class:active={statusFilter === "all"}
    aria-pressed={statusFilter === "all"}
    onclick={() => (statusFilter = "all")}
  >
    <span>Installed</span>
    <strong>{summary.total}</strong>
    <small>discovered packages</small>
  </button>
  <button
    class:active={statusFilter === "active"}
    aria-pressed={statusFilter === "active"}
    onclick={() => (statusFilter = "active")}
  >
    <span>Available now</span>
    <strong>{summary.active}</strong>
    <small>{summary.runningWorkers} workers running</small>
  </button>
  <button
    class:active={statusFilter === "inactive"}
    aria-pressed={statusFilter === "inactive"}
    onclick={() => (statusFilter = "inactive")}
  >
    <span>Inactive</span>
    <strong>{summary.inactive}</strong>
    <small>kept off the live registry</small>
  </button>
  <button
    class:active={statusFilter === "attention"}
    aria-pressed={statusFilter === "attention"}
    onclick={() => (statusFilter = "attention")}
  >
    <span>Needs attention</span>
    <strong>{summary.attention}</strong>
    <small>dependency or build state</small>
  </button>
</section>

<nav
  class="package-views"
  aria-label="Addon package views"
  bind:this={packageViewsElement}
>
  {#each packageViews as packageView}
    <button
      class:active={kindFilter === packageView[0]}
      aria-pressed={kindFilter === packageView[0]}
      onclick={() => (kindFilter = packageView[0])}
      onkeydown={scrollPackageViews}
    >
      {packageView[1]}
    </button>
  {/each}
</nav>

{#if doctor && (doctor.summary.errors || doctor.summary.warnings)}
  <section
    class="doctor-summary"
    class:doctor-summary--error={doctor.summary.errors > 0}
    role="status"
  >
    <Icon name="activity" size={18} />
    <div>
      <strong>Addon Doctor found {doctor.summary.errors} error{doctor.summary.errors === 1 ? "" : "s"} and {doctor.summary.warnings} warning{doctor.summary.warnings === 1 ? "" : "s"}</strong>
      <p>{doctor.runtime.container_detail}</p>
    </div>
  </section>
{/if}

{#if restartRequired}
  <section class="restart-banner" role="status">
    <span><Icon name="refresh" size={18} /></span>
    <div>
      <strong>Restart pending</strong>
      <p>
        Registry state is current. Restart once to load or release Flask routes
        and finish the lifecycle change.
      </p>
    </div>
    {#if isAdmin}
      <Button variant="outline" onclick={onrestart}>Restart server</Button>
    {/if}
  </section>
{/if}
