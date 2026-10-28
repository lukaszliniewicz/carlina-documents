<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import type { FrontendApiClient } from "@carlina/sdk";
  import { Badge, Button } from "@carlina/ui";
  import {
    createMcpConnection,
    readMcpConnections,
    revokeMcpConnection,
    type IssuedMcpConnection,
    type McpConnections,
  } from "../lib/mcp-connections.ts";

  interface Props { api: FrontendApiClient; }
  let { api }: Props = $props();
  let connections = $state<McpConnections | null>(null);
  let loading = $state(true);
  let busy = $state(false);
  let label = $state("Local AI client");
  let folder = $state("");
  let issued = $state<IssuedMcpConnection | null>(null);
  let reveal = $state(false);
  let error = $state("");
  let copyNotice = $state("");
  let disposed = false;
  let readController: AbortController | undefined;
  const activeConnections = $derived(connections?.keys.filter((key) => key.status === "active") ?? []);

  function message(cause: unknown, fallback: string): string {
    return cause instanceof Error ? cause.message : fallback;
  }

  async function refresh(): Promise<void> {
    readController?.abort();
    const controller = new AbortController();
    readController = controller;
    loading = true;
    try {
      const result = await readMcpConnections(api, controller.signal);
      if (!disposed && !controller.signal.aborted) connections = result;
    } catch (cause) {
      if (!disposed && !controller.signal.aborted) {
        error = message(cause, "Unable to read MCP connections.");
      }
    } finally {
      if (!disposed && !controller.signal.aborted) loading = false;
    }
  }

  async function create(event: SubmitEvent): Promise<void> {
    event.preventDefault();
    if (busy || !connections?.gateway.ready || !label.trim()) return;
    busy = true;
    error = "";
    copyNotice = "";
    try {
      const result = await createMcpConnection(api, label, folder);
      if (disposed) return;
      issued = result;
      reveal = false;
    } catch (cause) {
      if (!disposed) error = message(cause, "Unable to create the MCP connection.");
    } finally {
      // A lost response may still have created a key. Read back the list instead
      // of automatically retrying a write or losing a successfully issued token.
      if (!disposed) {
        await refresh();
        busy = false;
      }
    }
  }

  async function revoke(keyId: string): Promise<void> {
    if (busy) return;
    busy = true;
    error = "";
    try {
      await revokeMcpConnection(api, keyId);
      if (disposed) return;
      if (issued?.key_id === keyId) { issued = null; reveal = false; copyNotice = ""; }
      await refresh();
    } catch (cause) {
      if (!disposed) error = message(cause, "Unable to revoke the MCP connection.");
    } finally {
      if (!disposed) busy = false;
    }
  }

  async function copy(value: string, name: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(value);
      if (!disposed) copyNotice = `${name} copied.`;
    } catch {
      if (!disposed) copyNotice = "Copy is unavailable. Select the field and copy it manually.";
    }
  }

  onMount(() => { void refresh(); });
  onDestroy(() => {
    disposed = true;
    readController?.abort();
    issued = null;
  });
</script>

<div class="mcp-panel" aria-label="MCP client setup">
  <div class="panel-heading">
    <div>
      <h3>Connect your AI client</h3>
      <p>Use a client that supports MCP over HTTP. It supplies the model for extraction and translation; Carlina saves, validates and exports the results.</p>
    </div>
    <Badge tone={connections?.gateway.ready ? "success" : "neutral"}>
      {loading ? "Checking listener" : connections?.gateway.ready ? "Local MCP ready" : "Listener unavailable"}
    </Badge>
  </div>

  {#if error}<p class="error" role="alert">{error}</p>{/if}
  {#if !loading && !connections?.gateway.ready}
    <div class="listener-help">
      <p>The local listener is not running. If you disabled it, start Documents with <code>CARLINA_MCP_ENABLED=1</code>, then refresh.</p>
      <Button variant="outline" size="sm" disabled={busy} onclick={() => { error = ""; void refresh(); }}>Refresh connection</Button>
    </div>
  {/if}

  {#if connections?.gateway.ready && connections.gateway.url}
    <div class="copy-field">
      <label for="mcp-server-url">Server URL</label>
      <div class="field-action">
        <input id="mcp-server-url" value={connections.gateway.url} readonly />
        <Button variant="outline" size="sm" onclick={() => void copy(connections!.gateway.url!, "Server URL")}>Copy URL</Button>
      </div>
    </div>
    {#if issued}
      <section class="issued" aria-label="New MCP connection">
        <strong>Connection created</strong>
        <p>Add the server URL and this bearer token to your client's MCP settings. Save the token now: it will not be shown again after leaving this page.</p>
        <label for="mcp-bearer">Bearer token</label>
        <div class="field-action">
          <input id="mcp-bearer" type={reveal ? "text" : "password"} value={issued.raw_token} readonly autocomplete="off" />
          <Button variant="outline" size="sm" onclick={() => void copy(issued!.raw_token, "Bearer token")}>Copy token</Button>
        </div>
        <div class="token-actions">
          <Button variant="quiet" size="sm" aria-pressed={reveal} onclick={() => { reveal = !reveal; }}>{reveal ? "Hide token" : "Show token"}</Button>
          <Button variant="quiet" size="sm" onclick={() => { issued = null; reveal = false; copyNotice = ""; }}>Done saving token</Button>
        </div>
        <p class="next-step">In your client, ask it to import a source document from the authorized folder, extract and review it, then translate or export it. Confirm its executor capabilities when it starts passive work.</p>
      </section>
    {:else}
      <form onsubmit={create}>
        <div class="form-grid">
          <div>
            <label for="mcp-label">Connection name</label>
            <input id="mcp-label" bind:value={label} required maxlength="120" disabled={busy} autocomplete="off" />
          </div>
          <div>
            <label for="mcp-folder">Source folder <span>(optional)</span></label>
            <input id="mcp-folder" bind:value={folder} disabled={busy} placeholder="Absolute path to a folder on this computer" autocomplete="off" aria-describedby="mcp-folder-help" />
          </div>
        </div>
        <p id="mcp-folder-help">This client can import files inside the folder you authorize. Leave it empty to work only with documents already imported into this account.</p>
        <Button type="submit" disabled={busy || loading || !label.trim()}>{busy ? "Creating connection…" : "Create connection"}</Button>
      </form>
    {/if}
  {/if}
  <p class="copy-notice" role="status">{copyNotice}</p>

  {#if activeConnections.length}
    <section class="connection-list" aria-label="Active MCP connections">
      <h4>Your connections</h4>
      <p>Each connection can run document, translation and passive work as your account. Revoke a key to disconnect its client.</p>
      <ul>
        {#each activeConnections as key (key.key_id)}
          <li>
            <div>
              <strong>{key.label}</strong>
              <span>{key.allowed_roots.length ? key.allowed_roots.join(", ") : "Existing documents only"}</span>
            </div>
            <Button variant="quiet" size="sm" disabled={busy} aria-label={`Revoke ${key.label}`} onclick={() => void revoke(key.key_id)}>Revoke</Button>
          </li>
        {/each}
      </ul>
    </section>
  {/if}
</div>

<style>
  .mcp-panel { border: 1px solid var(--color-rule); background: var(--color-paper); padding: clamp(1.25rem, 3vw, 2rem); }
  .panel-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 1.5rem; }
  h3, h4 { font-family: var(--font-editorial); font-weight: 550; margin: 0 0 0.5rem; }
  h3 { font-size: 1.4rem; }
  h4 { font-size: 1.05rem; }
  p { color: var(--color-ink-muted); font-size: 0.82rem; line-height: 1.65; margin-block: 0.5rem 1rem; max-width: 47rem; }
  label { display: block; font-size: 0.76rem; font-weight: 650; margin-block-end: 0.5rem; }
  label span { color: var(--color-ink-muted); font-weight: 400; }
  input { width: 100%; min-width: 0; border: 1px solid var(--color-rule-strong); background: var(--color-surface); color: var(--color-ink); padding: 0.65rem; font: inherit; font-size: 0.82rem; }
  input:focus-visible { outline: 2px solid var(--color-clay); outline-offset: 2px; }
  .field-action { display: flex; align-items: center; gap: 0.6rem; }
  .field-action input { flex: 1; }
  .copy-field, form, .issued { margin-block-start: 1.5rem; }
  .form-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 2fr); gap: 1rem; }
  .issued { border-inline-start: 3px solid var(--color-clay); padding-inline-start: 1rem; }
  .issued > strong { color: var(--color-clay); }
  .token-actions { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-block-start: 0.5rem; }
  .copy-notice { min-height: 1.4em; margin-block-end: 0; }
  .connection-list { border-block-start: 1px solid var(--color-rule); margin-block-start: 1.25rem; padding-block-start: 1.25rem; }
  ul { list-style: none; margin: 0; padding: 0; }
  li { display: flex; align-items: center; justify-content: space-between; gap: 1rem; padding-block: 0.75rem; }
  li div { min-width: 0; }
  li strong { display: block; font-size: 0.82rem; overflow-wrap: anywhere; }
  li span { display: block; color: var(--color-ink-muted); font-size: 0.76rem; overflow-wrap: anywhere; margin-block-start: 0.3rem; }
  .error { color: var(--color-danger); }
  @media (max-width: 42rem) {
    .panel-heading { flex-direction: column; gap: 0.5rem; }
    .form-grid { grid-template-columns: minmax(0, 1fr); }
    .field-action { flex-wrap: wrap; }
    .field-action input { flex-basis: 100%; }
  }
</style>
