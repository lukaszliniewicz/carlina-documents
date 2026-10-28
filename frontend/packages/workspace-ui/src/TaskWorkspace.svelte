<script lang="ts">
  import type { AddonMountContext } from "@carlina/sdk";

  import StructuredResult from "./StructuredResult.svelte";
  import { requestAddon, streamAddon } from "./request";
  import type { WorkspaceDefinition, WorkspaceField } from "./types";

  let {
    context,
    definition,
  }: { context: AddonMountContext; definition: WorkspaceDefinition } = $props();

  function initialValues(current: WorkspaceDefinition): Record<string, unknown> {
    return Object.fromEntries(
      current.fields.map((field) => [
        field.id,
        field.defaultValue ?? (field.type === "checkbox" ? false : ""),
      ]),
    );
  }

  // The definition is immutable for one mounted addon application.
  // svelte-ignore state_referenced_locally
  let values = $state<Record<string, unknown>>(initialValues(definition));
  let files = $state<Record<string, File[]>>({});
  let running = $state(false);
  let error = $state("");
  let result = $state<unknown>(undefined);
  let events = $state<Record<string, unknown>[]>([]);
  let progress = $state(0);
  let status = $state("Ready");
  let componentPending = $state<Record<string, boolean>>({});

  function setValue(field: WorkspaceField, value: unknown): void {
    values = { ...values, [field.id]: value };
  }

  function normalizedPayload(): Record<string, unknown> {
    const payload: Record<string, unknown> = {};
    for (const field of definition.fields) {
      if (field.type === "file") continue;
      const value = values[field.id];
      payload[field.id] =
        field.type === "number" && value !== ""
          ? Number(value)
          : value;
    }
    return payload;
  }

  function componentValueLabel(value: unknown): string {
    if (!value) return "Nothing selected";
    if (Array.isArray(value)) {
      return `${value.length} item${value.length === 1 ? "" : "s"} selected`;
    }
    if (typeof value === "object") {
      const record = value as Record<string, unknown>;
      const items = Array.isArray(record.items) ? record.items : [];
      if (items.length > 1) return `${items.length} items selected`;
      for (const key of ["name", "label", "title", "id"]) {
        if (typeof record[key] === "string" && record[key]) {
          return String(record[key]);
        }
      }
    }
    return String(value);
  }

  async function chooseComponentValue(field: WorkspaceField): Promise<void> {
    if (!field.componentId || componentPending[field.id]) return;
    componentPending = { ...componentPending, [field.id]: true };
    error = "";
    try {
      const result = await context.uiComponents.open(
        field.componentId,
        field.componentInput ?? {},
        { signal: context.signal },
      );
      if (result !== undefined) setValue(field, result);
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === "AbortError") return;
      error =
        reason instanceof Error
          ? reason.message
          : `${field.label} could not be selected.`;
    } finally {
      componentPending = { ...componentPending, [field.id]: false };
    }
  }

  function multipartBody(payload: unknown): FormData {
    const form = new FormData();
    const normalized = payload as Record<string, unknown>;
    for (const [key, value] of Object.entries(normalized)) {
      if (Array.isArray(value)) {
        for (const item of value) form.append(key, String(item));
      } else if (value !== undefined && value !== null) {
        form.append(key, String(value));
      }
    }
    for (const [key, selected] of Object.entries(files)) {
      for (const file of selected) form.append(key, file, file.name);
    }
    return form;
  }

  function makeBody(payload: unknown): BodyInit {
    return definition.encoding === "multipart"
      ? multipartBody(payload)
      : JSON.stringify(payload);
  }

  function consumeEvent(event: Record<string, unknown>): void {
    events = [...events, event].slice(-250);
    if (typeof event.percent === "number") progress = event.percent;
    const message = event.message ?? event.text ?? event.status;
    if (message) status = String(message);
    if (event.type === "error" || event.error) {
      error = String(event.message ?? event.error);
    }
    if ("result" in event) result = event.result;
    else if ("output" in event) result = event.output;
    else if (event.type === "finish") result = event;
  }

  async function submit(): Promise<void> {
    const missingComponent = definition.fields.find(
      (field) =>
        field.type === "component" && field.required && !values[field.id],
    );
    if (missingComponent) {
      error = `Choose ${missingComponent.label.toLowerCase()} before starting.`;
      status = "Needs attention";
      return;
    }
    running = true;
    error = "";
    result = undefined;
    events = [];
    progress = 0;
    status = "Starting…";
    const base = normalizedPayload();
    const payload = definition.preparePayload?.(base, files) ?? base;
    try {
      if (definition.response === "sse") {
        await streamAddon(
          context,
          definition.endpoint,
          makeBody(payload),
          consumeEvent,
        );
        if (!error) status = status === "Starting…" ? "Complete" : status;
      } else {
        const method = definition.method ?? "POST";
        result =
          method === "GET"
            ? await requestAddon(context, definition.endpoint, {
                method,
                query: payload as Record<string, unknown>,
              })
            : await requestAddon(context, definition.endpoint, {
                body: makeBody(payload),
                method,
              });
        status = "Complete";
        progress = 100;
      }
    } catch (reason) {
      if (reason instanceof DOMException && reason.name === "AbortError") return;
      error = reason instanceof Error ? reason.message : "The request failed.";
      status = "Needs attention";
    } finally {
      running = false;
    }
  }

  async function runAction(action: NonNullable<WorkspaceDefinition["secondaryActions"]>[number]): Promise<void> {
    running = true;
    error = "";
    try {
      const payload = action.payload?.(normalizedPayload()) ?? normalizedPayload();
      result =
        (action.method ?? "POST") === "GET"
          ? await requestAddon(context, action.endpoint, {
              query: payload as Record<string, unknown>,
            })
          : await requestAddon(context, action.endpoint, {
              body: JSON.stringify(payload),
              method: action.method ?? "POST",
            });
      status = `${action.label} complete`;
    } catch (reason) {
      error = reason instanceof Error ? reason.message : "The request failed.";
    } finally {
      running = false;
    }
  }
</script>

<main class="ws-shell">
  <header class="ws-header">
    <div>
      <p class="ws-eyebrow">{definition.eyebrow ?? "Automation workspace"}</p>
      <h1>{definition.title}</h1>
      <p>{definition.description}</p>
    </div>
    <div class="ws-status" class:active={running}>
      <span></span>
      <div><small>Workspace status</small><strong>{status}</strong></div>
    </div>
  </header>

  <div class="ws-layout">
    <form class="ws-form" onsubmit={(event) => { event.preventDefault(); void submit(); }}>
      <div class="ws-section-heading">
        <span>01</span>
        <div><h2>Configure</h2><p>Set the inputs for this run.</p></div>
      </div>
      <div class="ws-fields">
        {#each definition.fields as field}
          <label class:wide={field.type === "textarea" || field.type === "file" || field.type === "component"}>
            {#if field.type === "checkbox"}
              <span class="ws-check">
                <input
                  type="checkbox"
                  checked={Boolean(values[field.id])}
                  onchange={(event) => setValue(field, event.currentTarget.checked)}
                />
                <strong>{field.label}</strong>
              </span>
            {:else}
              <span>{field.label}{#if field.required}<em>required</em>{/if}</span>
              {#if field.type === "component"}
                <div class="ws-component-field">
                  <button
                    type="button"
                    class="quiet"
                    disabled={running || componentPending[field.id]}
                    onclick={() => void chooseComponentValue(field)}
                  >
                    {componentPending[field.id]
                      ? "Opening…"
                      : values[field.id]
                        ? "Change selection"
                        : "Choose"}
                  </button>
                  <strong>{componentValueLabel(values[field.id])}</strong>
                </div>
              {:else if field.type === "textarea"}
                <textarea
                  rows={field.rows ?? 6}
                  placeholder={field.placeholder}
                  required={field.required}
                  value={String(values[field.id] ?? "")}
                  oninput={(event) => setValue(field, event.currentTarget.value)}
                ></textarea>
              {:else if field.type === "select"}
                <select
                  required={field.required}
                  value={String(values[field.id] ?? "")}
                  onchange={(event) => setValue(field, event.currentTarget.value)}
                >
                  {#each field.options ?? [] as option}
                    <option value={option.value}>{option.label}</option>
                  {/each}
                </select>
              {:else if field.type === "file"}
                <input
                  type="file"
                  accept={field.accept}
                  multiple={field.multiple}
                  required={field.required}
                  onchange={(event) =>
                    (files = {
                      ...files,
                      [field.id]: Array.from(event.currentTarget.files ?? []),
                    })}
                />
              {:else}
                <input
                  type={field.type}
                  min={field.min}
                  max={field.max}
                  step={field.step}
                  placeholder={field.placeholder}
                  required={field.required}
                  value={String(values[field.id] ?? "")}
                  oninput={(event) => setValue(field, event.currentTarget.value)}
                />
              {/if}
            {/if}
            {#if field.description}<small>{field.description}</small>{/if}
          </label>
        {/each}
      </div>
      <footer class="ws-actions">
        <div>
          {#each definition.secondaryActions ?? [] as action}
            <button type="button" class="quiet" disabled={running} onclick={() => void runAction(action)}>
              {action.label}
            </button>
          {/each}
        </div>
        <button type="submit" class="primary" disabled={running}>
          {running ? "Working…" : definition.submitLabel}
        </button>
      </footer>
    </form>

    <section class="ws-output" aria-live="polite" aria-busy={running}>
      <div class="ws-section-heading">
        <span>02</span>
        <div><h2>{definition.resultTitle ?? "Run preview"}</h2><p>Readable progress and results.</p></div>
      </div>
      {#if running || progress > 0}
        <div class="ws-progress">
          <div><span style={`width: ${Math.max(2, progress)}%`}></span></div>
          <small>{Math.round(progress)}%</small>
        </div>
      {/if}
      {#if error}
        <div class="ws-alert"><strong>Needs attention</strong><p>{error}</p></div>
      {/if}
      {#if result !== undefined}
        <div class="ws-result"><StructuredResult value={result} /></div>
      {:else if events.length}
        <ol class="ws-timeline">
          {#each events as event}
            <li class:error={event.type === "error"}>
              <span></span>
              <div>
                <strong>{String(event.type ?? "Update").replace(/_/g, " ")}</strong>
                <p>{String(event.message ?? event.text ?? event.status ?? "Progress updated")}</p>
              </div>
            </li>
          {/each}
        </ol>
      {:else if !error}
        <div class="ws-empty">
          <span>Ready</span>
          <h3>Your result will appear here</h3>
          <p>Start a run to see progress, generated files, and important metadata without digging through JSON.</p>
        </div>
      {/if}
    </section>
  </div>
</main>
