<script lang="ts">
  import type { UiComponentMountContext } from "@carlina/sdk";
  import { onMount } from "svelte";

  interface SegmenterInput {
    text?: string;
    threshold?: number;
    title?: string;
  }

  interface SplitResponse {
    sentences?: string[];
    error?: string;
  }

  interface HealthResponse {
    status?: string;
    healthy?: boolean;
    [key: string]: unknown;
  }

  let { context }: { context: UiComponentMountContext } = $props();

  const input = $derived((context.input ?? {}) as SegmenterInput);
  const title = $derived(input.title?.trim() || "Sentence laboratory");
  const titleId = `sentence-segmenter-${Math.random().toString(36).slice(2)}`;

  let text = $state("");
  let sentences = $state<string[]>([]);
  let processing = $state(false);
  let health = $state<"checking" | "ready" | "unavailable">("checking");
  let error = $state("");
  const characterCount = $derived(text.length);
  const wordCount = $derived(
    text.trim() ? text.trim().split(/\s+/u).length : 0,
  );

  onMount(() => {
    text = input.text || "";
    void checkHealth();
    if (text.trim()) void split();
  });

  async function checkHealth(): Promise<void> {
    try {
      const payload = await context.api.request<HealthResponse>("/api/health", {
        signal: context.signal,
      });
      health =
        payload.healthy === false || payload.status === "error"
          ? "unavailable"
          : "ready";
    } catch {
      health = "unavailable";
    }
  }

  async function split(): Promise<void> {
    const source = text.trim();
    if (!source) return;
    processing = true;
    error = "";
    try {
      const payload = await context.api.request<SplitResponse>("/api/split", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          text: source,
          threshold: input.threshold,
        }),
        signal: context.signal,
      });
      if (payload.error) throw new Error(payload.error);
      sentences = (payload.sentences ?? [])
        .filter((sentence): sentence is string => typeof sentence === "string")
        .map((sentence) => sentence.trim())
        .filter(Boolean);
    } catch (caught) {
      if (!context.signal.aborted) {
        error =
          caught instanceof Error
            ? caught.message
            : "The text could not be segmented.";
      }
    } finally {
      processing = false;
    }
  }

  function useResult(): void {
    if (!sentences.length) return;
    context.close({
      sentences,
      insert_text: sentences.join("\n"),
    });
  }
</script>

<section class="ss-tester" aria-labelledby={titleId}>
  <header class="ss-tester__header">
    <div>
      <p class="ss-tester__eyebrow">Multilingual sentence segmentation</p>
      <h1 id={titleId}>{title}</h1>
      <p>
        Inspect how prose is divided before using the result in another
        workflow.
      </p>
    </div>
    <button type="button" class="ss-tester__quiet" onclick={() => context.close()}>
      Close
    </button>
  </header>

  <div class="ss-tester__status">
    <p>
      <span
        class:ready={health === "ready"}
        class:unavailable={health === "unavailable"}
        aria-hidden="true"
      ></span>
      Service {health === "checking"
        ? "checking"
        : health === "ready"
          ? "ready"
          : "not responding"}
    </p>
    <span>{wordCount} words · {characterCount} characters</span>
  </div>

  <div class="ss-tester__body">
    <form
      onsubmit={(event) => {
        event.preventDefault();
        void split();
      }}
    >
      <div class="ss-tester__section-heading">
        <p>Source text</p>
        <button type="button" disabled={!text} onclick={() => {
          text = "";
          sentences = [];
          error = "";
        }}>Clear</button>
      </div>
      <textarea
        bind:value={text}
        spellcheck="true"
        placeholder="Paste a paragraph in any supported language…"
      ></textarea>
      <button
        type="submit"
        class="ss-tester__run"
        disabled={!text.trim() || processing}
      >
        {processing ? "Segmenting…" : "Split into sentences"}
      </button>
    </form>

    <main aria-busy={processing}>
      <div class="ss-tester__section-heading">
        <p>Result</p>
        <span>{sentences.length} sentence{sentences.length === 1 ? "" : "s"}</span>
      </div>

      {#if error}
        <p class="ss-tester__error" role="alert">{error}</p>
      {:else if processing}
        <div class="ss-tester__state" role="status">
          <span></span>
          <p>The language model is marking boundaries…</p>
        </div>
      {:else if !sentences.length}
        <div class="ss-tester__state">
          <strong>No segmented text yet.</strong>
          <p>The resulting sentences will appear here as an ordered proof.</p>
        </div>
      {:else}
        <ol class="ss-tester__sentences">
          {#each sentences as sentence, index}
            <li>
              <span>{String(index + 1).padStart(2, "0")}</span>
              <p>{sentence}</p>
            </li>
          {/each}
        </ol>
      {/if}
    </main>
  </div>

  <footer class="ss-tester__footer">
    <p>
      {sentences.length
        ? "The result is structured and ready to reuse."
        : "Nothing leaves this window until you use the result."}
    </p>
    <div>
      <button type="button" class="ss-tester__quiet" onclick={() => context.close()}>
        Cancel
      </button>
      <button
        type="button"
        class="ss-tester__primary"
        disabled={!sentences.length}
        onclick={useResult}
      >
        Use result
      </button>
    </div>
  </footer>
</section>
