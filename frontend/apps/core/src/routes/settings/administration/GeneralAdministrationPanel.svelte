<script lang="ts">
  import { untrack } from "svelte";
  import type {
    FrontendAdministrationGeneral,
    FrontendAdministrationMutationResponse,
    FrontendApiClient,
  } from "@carlina/sdk";
  import { Badge, Button, Icon } from "@carlina/ui";

  interface Props {
    api: FrontendApiClient;
    value: FrontendAdministrationGeneral;
    updateUrl: string;
    onrefresh: () => Promise<void>;
  }

  let { api, value, updateUrl, onrefresh }: Props = $props();

  let applicationUrl = $state(
    untrack(() => value.application_url),
  );
  let policyMarkdown = $state(
    untrack(() => value.site_policy.markdown),
  );
  let current = $state(untrack(() => value));
  let saving = $state(false);
  let error = $state("");
  let notice = $state("");

  const dirty = $derived(
    applicationUrl.trim().replace(/\/+$/, "") !==
      current.application_url.trim().replace(/\/+$/, "") ||
      policyMarkdown.trim() !== current.site_policy.markdown.trim(),
  );
  const policyTooLong = $derived(
    policyMarkdown.length > current.limits.policy_characters,
  );

  function discard(): void {
    applicationUrl = current.application_url;
    policyMarkdown = current.site_policy.markdown;
    error = "";
    notice = "Unsaved publishing edits were discarded.";
  }

  async function save(): Promise<void> {
    if (!dirty || policyTooLong || current.site_policy.truncated) return;
    saving = true;
    error = "";
    notice = "";
    try {
      const response =
        await api.request<FrontendAdministrationMutationResponse>(
          updateUrl,
          {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              application_url: applicationUrl,
              site_policy_markdown: policyMarkdown,
            }),
          },
        );
      if (response.general) {
        current = response.general;
        applicationUrl = response.general.application_url;
        policyMarkdown = response.general.site_policy.markdown;
      }
      notice = response.message || "Publishing settings updated.";
      await onrefresh();
    } catch (reason) {
      error =
        reason instanceof Error
          ? reason.message
          : "Publishing settings were not saved.";
    } finally {
      saving = false;
    }
  }
</script>

<section class="general-panel" aria-labelledby="general-heading">
  <header class="section-intro">
    <div>
      <p>Public footing</p>
      <h2 id="general-heading">Address, policy, session continuity</h2>
    </div>
    <span>
      Keep public links canonical and institutional policy readable without
      bringing secrets into the browser.
    </span>
  </header>

  {#if error}
    <p class="panel-message panel-message--error" role="alert">{error}</p>
  {:else if notice}
    <p class="panel-message" role="status">
      <Icon name="check" size={14} />
      {notice}
    </p>
  {/if}

  {#if current.site_policy.truncated}
    <section class="limit-warning" role="alert">
      <Icon name="shield" size={18} />
      <div>
        <strong>The stored policy exceeds the safe editor limit.</strong>
        <p>
          Saving is disabled so this view cannot overwrite an incomplete copy.
          Shorten the value through a maintenance workflow before returning.
        </p>
      </div>
    </section>
  {/if}

  <div class="general-grid">
    <form
      onsubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <section class="editor-section">
        <div class="section-number">01</div>
        <div class="field-heading">
          <div>
            <p>Canonical address</p>
            <h3>Application URL</h3>
          </div>
          <Badge tone={applicationUrl.trim() ? "success" : "warning"}>
            {applicationUrl.trim() ? "Configured" : "Missing"}
          </Badge>
        </div>
        <label for="administration-application-url">
          Public base URL
          <input
            id="administration-application-url"
            type="url"
            bind:value={applicationUrl}
            maxlength="2048"
            placeholder="https://assistant.example.org"
            autocomplete="url"
          />
        </label>
        <p class="field-help">
          Used for links that leave the application, including email and OAuth
          callbacks. Queries, fragments, and embedded credentials are rejected.
        </p>
      </section>

      <section class="editor-section">
        <div class="section-number">02</div>
        <div class="field-heading">
          <div>
            <p>Published terms</p>
            <h3>Site policy</h3>
          </div>
          <span class:over-limit={policyTooLong}>
            {policyMarkdown.length.toLocaleString()} /
            {current.limits.policy_characters.toLocaleString()}
          </span>
        </div>
        <label for="administration-site-policy">
          Policy in Markdown
          <textarea
            id="administration-site-policy"
            bind:value={policyMarkdown}
            maxlength={current.limits.policy_characters + 1}
            rows="16"
            placeholder="# Terms and privacy"
          ></textarea>
        </label>
        <div class="policy-links">
          <p class="field-help">
            A non-empty policy publishes the existing minimal public terms page.
          </p>
          {#if current.site_policy.published}
            <a href={current.site_policy.preview_url} target="_blank">
              Preview policy
              <Icon name="arrow-up-right" size={13} />
            </a>
          {/if}
        </div>
      </section>

      <footer>
        <Button
          type="button"
          variant="quiet"
          disabled={!dirty || saving}
          onclick={discard}
        >
          Discard edits
        </Button>
        <Button
          type="submit"
          disabled={
            !dirty ||
            saving ||
            policyTooLong ||
            current.site_policy.truncated
          }
        >
          <Icon name="check" size={14} />
          {saving ? "Saving…" : "Save publishing"}
        </Button>
      </footer>
    </form>

    <aside class="security-reading">
      <div class="security-mark">
        <Icon name="shield" size={24} />
      </div>
      <p>Session signing</p>
      <h3>{current.session_security.label}</h3>
      <Badge
        tone={
          current.session_security.recommended
            ? "success"
            : current.session_security.persistent
              ? "neutral"
              : "warning"
        }
      >
        {current.session_security.recommended
          ? "Installer managed"
          : current.session_security.persistent
            ? "Persistent"
            : "Restart sensitive"}
      </Badge>
      <p class="security-copy">{current.session_security.note}</p>

      <dl>
        <div>
          <dt>Secret value</dt>
          <dd>Never returned</dd>
        </div>
        <div>
          <dt>Browser rotation</dt>
          <dd>Unavailable by design</dd>
        </div>
        <div>
          <dt>Preferred source</dt>
          <dd>Installer environment</dd>
        </div>
      </dl>

      {#if !current.session_security.persistent}
        <div class="security-advice">
          <strong>Sessions will be lost after a restart.</strong>
          <p>
            Run the setup or repair launcher to create a durable environment
            secret, then restart the server.
          </p>
        </div>
      {/if}
    </aside>
  </div>
</section>

<style>
  .general-panel {
    border-inline: 1px solid var(--color-rule-strong);
    border-bottom: 1px solid var(--color-rule-strong);
    background: var(--color-paper);
  }

  .section-intro {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(16rem, 0.6fr);
    gap: 2rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 1.4rem;
  }

  .section-intro p,
  .section-intro h2,
  .section-intro > span {
    margin: 0;
  }

  .section-intro p,
  .field-heading p,
  .security-reading > p:first-of-type {
    color: var(--color-clay);
    font-size: 0.58rem;
    font-weight: 800;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  .section-intro h2 {
    margin-top: 0.25rem;
    font-family: var(--font-editorial);
    font-size: clamp(1.4rem, 2.7vw, 2rem);
    font-weight: 520;
  }

  .section-intro > span {
    align-self: end;
    color: var(--color-ink-muted);
    font-size: 0.73rem;
    line-height: 1.6;
  }

  .general-grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(16rem, 0.34fr);
  }

  form {
    min-width: 0;
    border-right: 1px solid var(--color-rule-strong);
  }

  .editor-section {
    position: relative;
    border-bottom: 1px solid var(--color-rule);
    padding: clamp(1.15rem, 3vw, 2rem);
  }

  .section-number {
    position: absolute;
    top: 1rem;
    right: 1rem;
    color: var(--color-rule-strong);
    font-family: var(--font-editorial);
    font-size: 0.7rem;
  }

  .field-heading {
    display: flex;
    align-items: end;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 1.1rem;
    padding-right: 1.8rem;
  }

  .field-heading p,
  .field-heading h3 {
    margin: 0;
  }

  .field-heading h3 {
    margin-top: 0.25rem;
    font-family: var(--font-editorial);
    font-size: 1.35rem;
    font-weight: 520;
  }

  .field-heading > span {
    color: var(--color-ink-muted);
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.6rem;
  }

  .field-heading > span.over-limit {
    color: var(--color-danger);
  }

  label {
    display: grid;
    gap: 0.45rem;
    color: var(--color-ink-soft);
    font-size: 0.67rem;
    font-weight: 720;
  }

  input,
  textarea {
    width: 100%;
    border: 1px solid var(--color-rule);
    padding: 0.78rem 0.85rem;
    background: var(--color-surface);
    color: var(--color-ink);
    font: inherit;
    font-size: 0.78rem;
  }

  textarea {
    resize: vertical;
    font-family: ui-monospace, "Cascadia Code", monospace;
    line-height: 1.55;
  }

  input:focus,
  textarea:focus {
    border-color: var(--color-focus);
    outline: 2px solid var(--color-focus);
    outline-offset: 2px;
  }

  .field-help,
  .policy-links p {
    margin: 0.5rem 0 0;
    color: var(--color-ink-muted);
    font-size: 0.68rem;
    line-height: 1.55;
  }

  .policy-links {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
  }

  .policy-links a {
    flex: 0 0 auto;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    color: var(--color-clay);
    font-size: 0.68rem;
    font-weight: 700;
    text-decoration: none;
  }

  form footer {
    display: flex;
    justify-content: flex-end;
    gap: 0.55rem;
    padding: 0.9rem 1.15rem;
    background: var(--color-surface);
  }

  .security-reading {
    min-width: 0;
    padding: clamp(1.2rem, 3vw, 2rem);
    background:
      linear-gradient(145deg, var(--color-lichen-muted), transparent 56%),
      var(--color-surface);
  }

  .security-mark {
    width: 3.25rem;
    height: 3.25rem;
    display: grid;
    place-items: center;
    border: 1px solid var(--color-lichen);
    color: var(--color-lichen);
  }

  .security-reading > p:first-of-type {
    margin: 1.4rem 0 0.3rem;
  }

  .security-reading h3 {
    margin: 0 0 0.65rem;
    font-family: var(--font-editorial);
    font-size: 1.4rem;
    font-weight: 520;
  }

  .security-copy {
    margin: 1.2rem 0;
    color: var(--color-ink-muted);
    font-size: 0.72rem;
    line-height: 1.6;
  }

  dl {
    margin: 0;
    border-block: 1px solid var(--color-rule);
  }

  dl div {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.65rem 0;
    font-size: 0.65rem;
  }

  dl div:last-child {
    border-bottom: 0;
  }

  dt {
    color: var(--color-ink-muted);
  }

  dd {
    margin: 0;
    text-align: right;
  }

  .security-advice {
    border-left: 3px solid var(--color-clay);
    margin-top: 1.2rem;
    padding-left: 0.85rem;
  }

  .security-advice strong,
  .security-advice p {
    margin: 0;
    font-size: 0.68rem;
  }

  .security-advice p {
    margin-top: 0.3rem;
    color: var(--color-ink-muted);
    line-height: 1.5;
  }

  .panel-message,
  .limit-warning {
    margin: 0;
    border-bottom: 1px solid var(--color-rule);
    padding: 0.8rem 1.4rem;
    font-size: 0.7rem;
  }

  .panel-message {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    color: var(--color-lichen);
  }

  .panel-message--error {
    color: var(--color-danger);
  }

  .limit-warning {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 0.75rem;
    background: var(--color-clay-muted);
  }

  .limit-warning strong,
  .limit-warning p {
    margin: 0;
  }

  .limit-warning p {
    margin-top: 0.25rem;
    color: var(--color-ink-muted);
    font-size: 0.68rem;
    line-height: 1.5;
  }

  @media (max-width: 62rem) {
    .general-grid {
      grid-template-columns: 1fr;
    }

    form {
      border-right: 0;
      border-bottom: 1px solid var(--color-rule-strong);
    }
  }

  @media (max-width: 42rem) {
    .section-intro {
      grid-template-columns: 1fr;
      gap: 0.75rem;
    }

    .field-heading,
    .policy-links {
      align-items: flex-start;
      flex-direction: column;
    }

    .policy-links a {
      margin-top: 0.4rem;
    }
  }
</style>
