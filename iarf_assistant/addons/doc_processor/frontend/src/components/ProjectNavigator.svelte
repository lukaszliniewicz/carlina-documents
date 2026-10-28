<script lang="ts">
  import { Icon } from "@carlina/ui";
  import { createEventDispatcher } from "svelte";
  import type { DocumentProject } from "../lib/types";
  import { statusTone } from "../lib/utils";

  export let projects: DocumentProject[] = [];
  export let activeProjectId: string | null = null;
  export let creating = false;

  const dispatch = createEventDispatcher<{
    selectLibrary: void;
    selectProject: string;
    createProject: string;
  }>();

  let projectName = "";

  $: activeProject = projects.find((project) => project.id === activeProjectId) ?? null;

  function create() {
    const name = projectName.trim();
    if (!name) {
      return;
    }
    dispatch("createProject", name);
    projectName = "";
  }
</script>

<section class="dp-project-nav">
  <nav class="dp-project-list" aria-label="Document processor views">
    <button class:active={!activeProjectId} type="button" on:click={() => dispatch("selectLibrary")}>
      <Icon name="book" size={16} />
      <span>All Documents</span>
    </button>
    <details class:active={Boolean(activeProject)} class="dp-collections-disclosure">
      <summary title={activeProject ? `Collections: ${activeProject.name}` : "Collections"}>
        <Icon name="folder" size={16} />
        <span class="dp-collection-label">
          Collections
          {#if activeProject}<small>· {activeProject.name}</small>{/if}
        </span>
      </summary>
      <div class="dp-collections-content">
        <div class="dp-project-create">
          <input
            type="text"
            maxlength="255"
            placeholder="New collection..."
            bind:value={projectName}
            on:keydown={(event) => {
              if (event.key === "Enter") {
                create();
              }
            }}
          />
          <button class="button" type="button" title="Create collection" aria-label="Create collection" disabled={creating || !projectName.trim()} on:click={create}>
            {#if creating}<Icon name="refresh" size={15} class="dp-spin" />{:else}<Icon name="plus" size={15} />{/if}
          </button>
        </div>

        <div class="dp-project-collection-list" aria-label="Collections">
          {#each projects as project (project.id)}
            <button
              class:active={project.id === activeProjectId}
              type="button"
              title={project.name}
              on:click={() => dispatch("selectProject", project.id)}
            >
              <Icon name="folder" size={16} />
              <span>{project.name}</span>
              <b class="dp-status-dot {statusTone(project.latest_run?.status || 'none')}"></b>
            </button>
          {/each}
        </div>
      </div>
    </details>
  </nav>
</section>
