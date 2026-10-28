<script lang="ts">
  import {
    addonApplicationEnabled,
    shouldUseClientNavigation,
    type FrontendApiClient,
    type FrontendBootstrap,
  } from "@carlina/sdk";
  import { Badge, Button, Icon, PageHeader } from "@carlina/ui";
  import McpConnectionPanel from "../components/McpConnectionPanel.svelte";

  interface Props {
    bootstrap: Pick<FrontendBootstrap, "addons" | "feature_flags"> & Partial<Pick<FrontendBootstrap, "user">>;
    api?: FrontendApiClient;
    navigate: (href: string, clientRoute?: boolean) => unknown;
  }

  let { bootstrap, navigate, api }: Props = $props();
  let mcpSetupOpen = $state(false);
  const workflows = $derived([
    {
      id: "doc_processor", title: "Extract and review", action: "Open extraction",
      description: "Import a PDF, EPUB or DOCX. Repair page joins, lists, tables and linked notes, then export the text.",
      href: "/addons/doc_processor/",
    },
    {
      id: "translator", title: "Translate", action: "Open translation",
      description: "Translate an imported document with aligned sentences, context and glossaries. Review the result and export it.",
      href: "/addons/translator/",
    },
  ].map((workflow) => {
    const addon = bootstrap.addons.find((item) => item.id === workflow.id);
    return { ...workflow, available: Boolean(addon && addonApplicationEnabled(addon, bootstrap)) };
  }));
  const setupNeeded = $derived(workflows.some((workflow) => !workflow.available));

  function open(href: string, event: MouseEvent): void {
    if (!shouldUseClientNavigation(event)) return;
    event.preventDefault();
    navigate(href, true);
  }
</script>

<div class="documents-home">
  <div class="documents-heading">
    <PageHeader
      eyebrow="Documents"
      title="From document to readable text"
      description="Extract, repair and translate, with EPUB, DOCX and PDF exports. Your documents and results stay in this application's data folder."
    />
  </div>

  {#if setupNeeded}
    <section class="setup-notice" aria-label="Document tools setup">
      <div>
        <strong>Set up your document tools</strong>
        <p>Activate Document Processor and Translator in tool settings. Review their dependency notices before installing the conversion tools.</p>
      </div>
      <a href="/settings/addons" onclick={(event) => open("/settings/addons", event)}>
        Set up tools <Icon name="arrow-up-right" size={16} />
      </a>
    </section>
  {/if}

  <section class="workflow-grid" aria-label="Document workflows">
    {#each workflows as workflow (workflow.id)}
      <article>
        <div class="workflow-heading">
          <Icon name="book" size={22} />
          <Badge tone={workflow.available ? "success" : "neutral"}>
            {workflow.available ? "Tool available" : "Needs setup"}
          </Badge>
        </div>
        <h2>{workflow.title}</h2>
        <p>{workflow.description}</p>
        {#if workflow.available}
          <a href={workflow.href} onclick={(event) => open(workflow.href, event)}>
            {workflow.action} <Icon name="arrow-up-right" size={16} />
          </a>
        {:else}
          <span class="unavailable">Available after tool setup</span>
        {/if}
      </article>
    {/each}
  </section>

  <section class="model-options" aria-label="Model connections">
    <div>
      <h2>Choose how the model works</h2>
      <p>Connect an API provider for direct extraction and translation, or connect your AI client through MCP to run passive jobs.</p>
    </div>
    <div class="model-actions">
      <a href="/settings/models" onclick={(event) => open("/settings/models", event)}>
        Model connections <Icon name="arrow-up-right" size={16} />
      </a>
      <Button variant="outline" aria-expanded={mcpSetupOpen} aria-controls="mcp-setup" onclick={() => { mcpSetupOpen = !mcpSetupOpen; }}>Connect AI client</Button>
    </div>
  </section>
  {#if mcpSetupOpen}
    <section id="mcp-setup" class="mcp-setup" aria-label="Connect an AI client">
      {#if api && bootstrap.user?.is_admin}
        <McpConnectionPanel {api} />
      {:else}
        <p>An administrator can create an MCP connection for this account.</p>
      {/if}
    </section>
  {/if}
</div>

<style>
  .documents-home { min-width: 0; padding-block-start: clamp(2rem, 4vw, 3.5rem); }
  .documents-heading, .workflow-grid, .model-options, .setup-notice, .mcp-setup {
    width: min(100% - 2 * clamp(1rem, 4vw, 3.5rem), 76rem);
    margin-inline: auto;
  }
  .workflow-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 20rem), 1fr));
    gap: 1.25rem;
    margin-block: 2rem;
  }
  article { border: 1px solid var(--color-rule); padding: clamp(1.25rem, 3vw, 2rem); }
  .workflow-heading { display: flex; align-items: center; justify-content: space-between; gap: 1rem; color: var(--color-clay); }
  h2 { font-family: var(--font-editorial); font-size: 1.45rem; font-weight: 550; margin-block: 1.25rem 0.5rem; }
  p { color: var(--color-ink-muted); font-size: 0.85rem; line-height: 1.65; margin-block: 0.5rem 1.5rem; }
  a { display: inline-flex; align-items: center; gap: 0.5rem; color: var(--color-clay); font-size: 0.82rem; font-weight: 650; text-underline-offset: 0.25rem; }
  a:focus-visible { outline: 2px solid var(--color-clay); outline-offset: 4px; }
  .unavailable { color: var(--color-ink-muted); font-size: 0.82rem; }
  .setup-notice, .model-options { display: flex; align-items: center; justify-content: space-between; gap: 1.5rem; }
  .setup-notice { border-inline-start: 3px solid var(--color-clay); background: var(--color-paper); padding: 1.25rem; margin-block-start: 1.5rem; }
  .setup-notice p, .model-options p { margin-block-end: 0; max-width: 42rem; }
  .setup-notice a, .model-options a { flex-shrink: 0; }
  .model-options { border-block-start: 1px solid var(--color-rule); padding-block: 1rem 2.5rem; }
  .model-options h2 { font-size: 1.15rem; }
  .model-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 1rem; }
  .mcp-setup { padding-block-end: 2.5rem; }
  @media (max-width: 42rem) {
    .setup-notice, .model-options { align-items: flex-start; flex-direction: column; }
  }
</style>
