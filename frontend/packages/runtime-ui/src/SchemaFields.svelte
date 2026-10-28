<script lang="ts">
  import type { FrontendRunField } from "@carlina/sdk";

  interface Props {
    fields: FrontendRunField[];
    emptyText?: string;
  }

  let {
    fields,
    emptyText = "No fields were recorded for this step.",
  }: Props = $props();

  function simpleValue(value: unknown): string | null {
    if (value === null || value === undefined || value === "") return null;
    if (typeof value === "boolean") return value ? "Yes" : "No";
    if (typeof value === "string" || typeof value === "number") {
      return String(value);
    }
    if (
      Array.isArray(value) &&
      value.every(
        (item) =>
          item === null ||
          ["string", "number", "boolean"].includes(typeof item),
      )
    ) {
      return value.map(String).join(", ");
    }
    return null;
  }

  function structuredRows(value: unknown): Array<{ label: string; value: string }> {
    const rows: Array<{ label: string; value: string }> = [];
    const visit = (item: unknown, path: string[], depth: number) => {
      if (rows.length >= 36) return;
      if (item === null || item === undefined) {
        rows.push({ label: path.join(" / ") || "Value", value: "Not recorded" });
        return;
      }
      if (typeof item !== "object") {
        rows.push({
          label: path.join(" / ") || "Value",
          value: typeof item === "boolean" ? (item ? "Yes" : "No") : String(item),
        });
        return;
      }
      if (depth >= 4) {
        rows.push({
          label: path.join(" / ") || "Value",
          value: "Additional nested detail",
        });
        return;
      }
      const entries = Array.isArray(item)
        ? item.map((entry, index) => [String(index + 1), entry] as const)
        : Object.entries(item as Record<string, unknown>);
      if (!entries.length) {
        rows.push({ label: path.join(" / ") || "Value", value: "Empty" });
        return;
      }
      for (const [key, entry] of entries) {
        const label = key
          .replace(/[_-]+/g, " ")
          .replace(/^\w/, (letter) => letter.toUpperCase());
        visit(entry, [...path, label], depth + 1);
      }
    };
    visit(value, [], 0);
    return rows;
  }
</script>

{#if fields.length}
  <dl class="schema-fields">
    {#each fields as field (field.key)}
      <div>
        <dt>
          <span>{field.label}</span>
          <small>{field.type}{field.required ? " · required" : ""}</small>
        </dt>
        <dd>
          {#if simpleValue(field.value) !== null}
            <p>{simpleValue(field.value)}</p>
          {:else}
            <details>
              <summary>{field.summary}</summary>
              <dl class="nested-value">
                {#each structuredRows(field.value) as row, index (`${row.label}:${index}`)}
                  <div>
                    <dt>{row.label}</dt>
                    <dd>{row.value}</dd>
                  </div>
                {/each}
              </dl>
            </details>
          {/if}
          {#if field.description}<small>{field.description}</small>{/if}
        </dd>
      </div>
    {/each}
  </dl>
{:else}
  <p class="empty">{emptyText}</p>
{/if}

<style>
  .schema-fields {
    display: grid;
    gap: 1px;
    margin: 0;
    background: var(--color-rule);
  }

  .schema-fields > div {
    min-width: 0;
    display: grid;
    grid-template-columns: minmax(8rem, 0.38fr) minmax(0, 1fr);
    background: var(--color-surface);
  }

  dt,
  dd {
    min-width: 0;
    margin: 0;
    padding: 0.72rem 0.8rem;
  }

  dt {
    display: grid;
    align-content: start;
    gap: 0.22rem;
    border-right: 1px solid var(--color-rule);
  }

  dt span {
    color: var(--color-ink);
    font-size: 0.72rem;
    font-weight: 680;
  }

  small {
    color: var(--color-ink-muted);
    font-size: 0.59rem;
    line-height: 1.45;
  }

  dd {
    color: var(--color-ink-soft);
    font-size: 0.69rem;
  }

  dd p {
    overflow-wrap: anywhere;
    margin: 0 0 0.25rem;
    line-height: 1.55;
    white-space: pre-wrap;
  }

  details summary {
    cursor: pointer;
    color: var(--color-ink-soft);
    line-height: 1.5;
  }

  .nested-value {
    max-height: 18rem;
    overflow: auto;
    display: grid;
    gap: 1px;
    margin: 0.7rem 0 0;
    border-left: 2px solid var(--color-clay);
    padding-left: 0.55rem;
    background: var(--color-rule);
  }

  .nested-value > div {
    display: grid;
    grid-template-columns: minmax(6rem, 0.35fr) minmax(0, 1fr);
    gap: 0.5rem;
    padding: 0.5rem;
    background: var(--color-surface-muted);
  }

  .nested-value dt {
    color: var(--color-ink-muted);
    font-size: 0.57rem;
  }

  .nested-value dd {
    overflow-wrap: anywhere;
    padding: 0;
    color: var(--color-ink-soft);
    font-size: 0.61rem;
    line-height: 1.5;
  }

  .empty {
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.7rem;
  }

  @media (max-width: 34rem) {
    .schema-fields > div {
      grid-template-columns: 1fr;
    }

    dt {
      border-right: 0;
      border-bottom: 1px solid var(--color-rule);
    }
  }
</style>
