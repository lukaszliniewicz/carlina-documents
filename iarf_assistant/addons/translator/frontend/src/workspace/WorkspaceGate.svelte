<script>
  import { onMount } from "svelte";
  import { createApiClient, isAbortError } from "../lib/api.js";
  import { createAbortController } from "../lib/request-state.js";
  import BlockWorkspaceApp from "./BlockWorkspaceApp.svelte";
  import LegacyWorkspaceApp from "./LegacyWorkspaceApp.svelte";
  import { DOCUMENT_BLOCK_ENGINE, LEGACY_ENGINE } from "./block-workspace-api.js";

  export let context = null;
  export let bootstrapData = null;

  const bootstrap = bootstrapData || (typeof window !== "undefined" ? window.TRANSLATOR_BOOTSTRAP || {} : {});
  const projectId = bootstrap.projectId;
  const lifecycleController = createAbortController(context?.signal || null);

  let state = "loading";
  let project = null;
  let error = "";
  let planningTimer = null;
  let retrying = false;
  let resolvedBootstrap = {
    ...bootstrap,
    accessRole: bootstrap.accessRole || (bootstrap.guestMode ? "viewer" : "owner"),
    shareSessionCsrf: bootstrap.shareSessionCsrf || ""
  };

  function clientForResolvedAccess() {
    return createApiClient({
      shareToken: resolvedBootstrap.shareToken || "",
      shareGrantToken: resolvedBootstrap.shareGrantToken || "",
      shareSessionCsrf: resolvedBootstrap.shareSessionCsrf || "",
      addonApi: context?.api || null,
      signal: context?.signal || null
    });
  }

  function fragmentSecret() {
    if (typeof window === "undefined") return "";
    const raw = String(window.location.hash || "").replace(/^#/, "");
    if (!raw) return "";
    const values = new URLSearchParams(raw);
    return String(values.get("access") || "").trim();
  }

  function clearFragment() {
    if (typeof window === "undefined" || !window.location.hash) return;
    window.history.replaceState(window.history.state, "", `${window.location.pathname}${window.location.search}`);
  }

  async function establishReviewSession() {
    if (!resolvedBootstrap.reviewMode) return;
    const secret = fragmentSecret();
    const endpoint = secret
      ? "/addons/translator/api/share/redeem"
      : "/addons/translator/api/share/session";
    const options = {
      method: "POST",
      signal: lifecycleController.signal,
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(secret
        ? { project_id: projectId, secret }
        : { project_id: projectId })
    };
    const response = await fetch(endpoint, options);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload?.error || (secret
        ? "This review link is invalid, expired, or revoked."
        : "This browser no longer has an active review session. Open the original capability link again."));
    }
    const role = String(payload?.role || "");
    if (!["viewer", "reviewer"].includes(role) || typeof payload?.csrf !== "string" || !payload.csrf) {
      throw new Error("The review session returned an invalid capability contract.");
    }
    resolvedBootstrap = {
      ...resolvedBootstrap,
      guestMode: true,
      accessRole: role,
      accessLabel: String(payload?.label || ""),
      shareSessionCsrf: payload.csrf
    };
    if (secret) clearFragment();
  }

  async function loadProject() {
    if (!projectId) {
      state = "error";
      error = "Translator workspace is missing a project identifier.";
      return;
    }
    try {
      await establishReviewSession();
      const client = clientForResolvedAccess();
      const payload = await client.fetchJson(`/addons/translator/api/projects/${encodeURIComponent(projectId)}`, { signal: lifecycleController.signal });
      const engine = Object.prototype.hasOwnProperty.call(payload || {}, "engine_contract")
        ? payload.engine_contract
        : LEGACY_ENGINE;
      if (engine !== LEGACY_ENGINE && engine !== DOCUMENT_BLOCK_ENGINE) {
        state = "error";
        error = "Translator workspace integrity error: the project has no recognized engine contract.";
        return;
      }
      project = payload;
      const operation = project?.latest_operation || project?.operation;
      if (engine === DOCUMENT_BLOCK_ENGINE && operation?.kind === "plan_document_blocks" && operation.state !== "completed") {
        state = "planning";
        if (["queued", "running", "cancel_requested"].includes(operation.state)) {
          clearTimeout(planningTimer);
          planningTimer = setTimeout(loadProject, 3000);
        }
      } else {
        state = engine;
      }
    } catch (caught) {
      if (isAbortError(caught)) return;
      state = "error";
      error = caught.message || "Unable to load the translator project.";
    }
  }

  async function retryPlanning() {
    if (retrying || resolvedBootstrap.guestMode) return;
    retrying = true; error = "";
    try {
      await clientForResolvedAccess().fetchJson(`/addons/translator/api/projects/${encodeURIComponent(projectId)}/source-planning/retry`, {
        method: "POST", signal: lifecycleController.signal,
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({})
      });
      await loadProject();
    } catch (caught) {
      if (!isAbortError(caught)) error = caught.message || "Unable to retry source planning.";
    } finally { retrying = false; }
  }

  onMount(() => {
    loadProject();
    return () => { clearTimeout(planningTimer); lifecycleController.abort(); };
  });
</script>

{#if state === "loading"}
  <main class="gate-state" role="status">Checking translation engine...</main>
{:else if state === "planning"}
  {@const operation = project?.latest_operation || project?.operation || {}}
  <main class="gate-state" role="status">
    <h1>Preparing the translation source</h1>
    <p>{operation.message || "The captured document is queued for sentence segmentation and block planning."}</p>
    <p>The source snapshot is saved. This page will open the block workspace when planning finishes.</p>
    {#if error}<p role="alert">{error}</p>{/if}
    {#if operation.state === "failed" && !resolvedBootstrap.guestMode}
      <button type="button" disabled={retrying} on:click={retryPlanning}>Retry source planning</button>
    {:else if ["cancelled", "partial"].includes(operation.state)}
      <p>Source planning is stopped. No translation has started.</p>
    {/if}
  </main>
{:else if state === "error"}
  <main class="gate-state error" role="alert">
    <h1>Workspace unavailable</h1>
    <p>{error}</p>
    <button type="button" on:click={loadProject}>Retry</button>
  </main>
{:else if state === LEGACY_ENGINE}
  <LegacyWorkspaceApp {context} bootstrapData={resolvedBootstrap} />
{:else if state === DOCUMENT_BLOCK_ENGINE}
  <BlockWorkspaceApp {context} bootstrapData={resolvedBootstrap} {project} />
{/if}

<style>
  .gate-state { min-height:100%; display:grid; place-content:center; gap:.7rem; padding:2rem; color:var(--color-ink-muted,#667); background:var(--color-paper,#f8f8f8); text-align:center; }
  .gate-state h1 { margin:0; color:var(--color-ink,#222); font-size:1.25rem; }
  .gate-state p { max-width:52ch; margin:0; }
  .gate-state.error { color:var(--color-danger,#b33); }
  button { width:fit-content; justify-self:center; border:1px solid var(--color-rule,#ddd); padding:.45rem .7rem; background:var(--color-surface,#fff); cursor:pointer; font:inherit; }
</style>
