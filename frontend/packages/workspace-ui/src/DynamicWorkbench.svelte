<script lang="ts">
  import { onMount } from "svelte";
  import type { AddonMountContext } from "@carlina/sdk";

  import StructuredResult from "./StructuredResult.svelte";
  import { requestAddon } from "./request";
  import type {
    DynamicAction,
    DynamicField,
    DynamicNotice,
    DynamicSchema,
    WorkspaceOption,
  } from "./types";

  let {
    context,
    title,
    description,
    schemaEndpoint,
    runEndpoint,
  }: {
    context: AddonMountContext;
    title: string;
    description: string;
    schemaEndpoint: string;
    runEndpoint: string;
  } = $props();

  let schema = $state<DynamicSchema | null>(null);
  let selectedId = $state("");
  let values = $state<Record<string, unknown>>({});
  let loading = $state(true);
  let running = $state(false);
  let error = $state("");
  let result = $state<unknown>(undefined);
  const selected = $derived(
    schema?.actions.find((action) => action.id === selectedId),
  );

  function normalizeOptions(field: DynamicField) {
    return (field.options ?? []).map((option) =>
      typeof option === "string"
        ? { label: option, value: option }
        : option,
    );
  }

  function matchesCondition(
    condition: Record<string, string | string[]> | undefined,
  ): boolean {
    if (!condition) return false;
    return Object.entries(condition).every(([fieldId, expected]) => {
      const expectedValues = Array.isArray(expected) ? expected : [expected];
      return expectedValues.includes(String(values[fieldId] ?? ""));
    });
  }

  function fieldIsVisible(field: DynamicField): boolean {
    return !matchesCondition(field.hiddenWhen);
  }

  function optionIsDisabled(option: WorkspaceOption): boolean {
    if (option.disabled) return true;
    return Boolean(
      option.allowedProviders?.length &&
        !option.allowedProviders.includes(String(values.provider ?? "auto")),
    );
  }

  function optionDisabledReason(option: WorkspaceOption): string {
    if (option.disabled) return option.disabledReason ?? "This option is unavailable.";
    return option.incompatibleReason ?? "This option is not compatible with the selected provider.";
  }

  function unavailableOptions(field: DynamicField): WorkspaceOption[] {
    return normalizeOptions(field).filter(optionIsDisabled);
  }

  function noticeIsVisible(notice: DynamicNotice): boolean {
    return matchesCondition(notice.visibleWhen);
  }

  function choose(action: DynamicAction): void {
    if (action.disabled) return;
    selectedId = action.id;
    values = Object.fromEntries(
      action.fields.map((field) => [
        field.id,
        field.default ?? (field.type === "checkbox" ? false : field.type === "multicheck" ? [] : ""),
      ]),
    );
    result = undefined;
    error = "";
  }

  async function load(): Promise<void> {
    loading = true;
    error = "";
    try {
      schema = (await requestAddon(context, schemaEndpoint)) as DynamicSchema;
      const first = schema.actions.find((action) => !action.disabled);
      if (first) choose(first);
    } catch (reason) {
      error = reason instanceof Error ? reason.message : "The schema could not load.";
    } finally {
      loading = false;
    }
  }

  async function run(): Promise<void> {
    if (!selected) return;
    running = true;
    error = "";
    result = undefined;
    const submittedValues = Object.fromEntries(
      selected.fields
        .filter(fieldIsVisible)
        .map((field) => [field.id, values[field.id]]),
    );
    try {
      result = await requestAddon(context, runEndpoint, {
        body: JSON.stringify({ action: selected.id, params: submittedValues }),
        method: "POST",
      });
    } catch (reason) {
      error = reason instanceof Error ? reason.message : "The action failed.";
    } finally {
      running = false;
    }
  }

  onMount(load);
</script>

<main class="ws-shell ws-shell--dynamic">
  <header class="ws-header">
    <div><p class="ws-eyebrow">Provider laboratory</p><h1>{title}</h1><p>{description}</p></div>
    <button class="quiet" type="button" disabled={loading} onclick={() => void load()}>Refresh schema</button>
  </header>

  {#if loading}
    <div class="ws-loading">Reading available provider capabilities…</div>
  {:else if !schema}
    <div class="ws-alert"><strong>Schema unavailable</strong><p>{error}</p></div>
  {:else}
    <div class="ws-dynamic-layout">
      <aside>
        <p class="ws-eyebrow">Actions</p>
        {#each schema.actions as action}
          <button
            class:active={action.id === selectedId}
            type="button"
            disabled={action.disabled}
            title={action.disabledReason}
            onclick={() => choose(action)}
          >
            <span>{action.group ?? "Action"}</span><strong>{action.label}</strong>
            {#if action.disabled}<small>{action.disabledReason}</small>{/if}
          </button>
        {/each}
      </aside>
      <form class="ws-form" onsubmit={(event) => { event.preventDefault(); void run(); }}>
        {#if selected}
          <div class="ws-section-heading">
            <span>01</span><div><h2>{selected.label}</h2><p>{selected.description}</p></div>
          </div>
          <div class="ws-fields">
            {#each selected.fields as field}
              {#if fieldIsVisible(field)}
              <label class:wide={["textarea", "urls", "cookies", "proxy", "url"].includes(field.type)}>
                {#if field.type === "checkbox"}
                  <span class="ws-check">
                    <input aria-label={field.label} type="checkbox" checked={Boolean(values[field.id])} onchange={(event) => values = {...values, [field.id]: event.currentTarget.checked}} />
                    <strong>{field.label}</strong>
                  </span>
                {:else if field.type === "multicheck"}
                  <span>{field.label}</span>
                  <div class="ws-multicheck">
                    {#each normalizeOptions(field) as option}
                      <label>
                        <input
                          type="checkbox"
                          checked={(values[field.id] as string[] ?? []).includes(option.value)}
                          onchange={(event) => {
                            const current = (values[field.id] as string[] ?? []);
                            values = {...values, [field.id]: event.currentTarget.checked ? [...current, option.value] : current.filter((item) => item !== option.value)};
                          }}
                        />
                        {option.label}
                      </label>
                    {/each}
                  </div>
                {:else if field.type === "select"}
                  <span>{field.label}</span>
                  <select aria-label={field.label} value={String(values[field.id] ?? "")} onchange={(event) => values = {...values, [field.id]: event.currentTarget.value}}>
                    {#each normalizeOptions(field) as option}
                      <option value={option.value} disabled={optionIsDisabled(option)}>{option.label}</option>
                    {/each}
                  </select>
                  {#if unavailableOptions(field).length}
                    <small class="ws-option-availability">
                      {#each unavailableOptions(field) as option}
                        <span>{option.label}: {optionDisabledReason(option)}</span>
                      {/each}
                    </small>
                  {/if}
                {:else if ["textarea", "urls", "cookies", "proxy"].includes(field.type)}
                  <span>{field.label}</span>
                  <textarea
                    aria-label={field.label}
                    rows={field.rows ?? 7}
                    placeholder={field.placeholder ?? (field.type === "urls" ? "One URL per line" : field.type === "cookies" || field.type === "proxy" ? "Enter structured JSON" : "")}
                    required={field.required}
                    value={String(values[field.id] ?? "")}
                    oninput={(event) => values = {...values, [field.id]: event.currentTarget.value}}
                  ></textarea>
                {:else}
                  <span>{field.label}</span>
                  <input
                    aria-label={field.label}
                    type={field.type === "number" ? "number" : field.type === "url" ? "url" : "text"}
                    min={field.min}
                    max={field.max}
                    placeholder={field.placeholder}
                    required={field.required}
                    value={String(values[field.id] ?? "")}
                    oninput={(event) => values = {...values, [field.id]: field.type === "number" ? Number(event.currentTarget.value) : event.currentTarget.value}}
                  />
                {/if}
                {#if field.description}<small>{field.description}</small>{/if}
              </label>
              {/if}
            {/each}
          </div>
          {#each selected.notices ?? [] as notice}
            {#if noticeIsVisible(notice)}
              <p class="ws-field-notice" role="status">{notice.message}</p>
            {/if}
          {/each}
          <footer class="ws-actions"><span></span><button class="primary" type="submit" disabled={running}>{running ? "Running…" : `Run ${selected.label}`}</button></footer>
        {/if}
      </form>
      <section class="ws-output">
        <div class="ws-section-heading"><span>02</span><div><h2>Response</h2><p>Timing, request details, and provider output.</p></div></div>
        {#if error}<div class="ws-alert"><strong>Needs attention</strong><p>{error}</p></div>
        {:else if result !== undefined}<div class="ws-result"><StructuredResult value={result} /></div>
        {:else}<div class="ws-empty"><span>Ready</span><h3>Select and run an action</h3><p>The response is grouped into readable fields and nested records.</p></div>{/if}
      </section>
    </div>
  {/if}
</main>
