<script lang="ts">
  import { onMount } from "svelte";
  import type {
    FrontendApiClient,
    FrontendBootstrap,
    FrontendProfile,
  } from "@carlina/sdk";
  import { Badge, Button, Icon, PageHeader } from "@carlina/ui";

  interface Props {
    api: FrontendApiClient;
    bootstrap: FrontendBootstrap;
    route: string;
    navigate: (href: string, clientRoute?: boolean) => void;
  }

  let { api }: Props = $props();

  let profile = $state<FrontendProfile | null>(null);
  let preferences = $state("");
  let styleReference = $state("");
  let loading = $state(true);
  let saving = $state(false);
  let error = $state("");
  let savedMessage = $state("");
  const textLimit = 20_000;

  onMount(() => {
    void loadProfile();
  });

  async function loadProfile() {
    loading = true;
    error = "";
    try {
      profile = await api.request<FrontendProfile>("/api/frontend/profile");
      preferences = profile.preferences;
      styleReference = profile.style_reference;
    } catch (reason) {
      error =
        reason instanceof Error ? reason.message : "Your profile could not be loaded.";
    } finally {
      loading = false;
    }
  }

  async function saveProfile() {
    saving = true;
    error = "";
    savedMessage = "";
    try {
      const response = await api.request<{
        success: boolean;
        profile: FrontendProfile;
      }>("/api/frontend/profile", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          preferences,
          style_reference: styleReference,
        }),
      });
      profile = response.profile;
      preferences = response.profile.preferences;
      styleReference = response.profile.style_reference;
      savedMessage = "Profile preferences saved.";
    } catch (reason) {
      error =
        reason instanceof Error ? reason.message : "Your profile could not be saved.";
    } finally {
      saving = false;
    }
  }

</script>

<div class="profile-page">
  <PageHeader
    eyebrow="Personal / Working context"
    title="Profile & preferences"
    description="Keep the context that should consistently shape your conversations and review how this installation protects it."
  />

  {#if loading}
    <section class="profile-loading" aria-label="Loading profile" aria-busy="true">
      <span></span><span></span><span></span>
    </section>
  {:else if profile}
    <div class="profile-layout">
      <aside class="profile-summary">
        <span class="profile-mark" aria-hidden="true">
          {profile.email.slice(0, 1).toUpperCase()}
        </span>
        <p class="profile-label">Signed in as</p>
        <h2>{profile.email}</h2>
        <Badge tone={profile.is_admin ? "warning" : "neutral"}>
          {profile.is_admin ? "Administrator" : "Member"}
        </Badge>
        <div class="summary-note">
          <Icon name="book" size={18} />
          <p>
            These notes become working context for the assistant. Keep them
            useful, current, and free of secrets.
          </p>
        </div>
      </aside>

      <form onsubmit={(event) => {
        event.preventDefault();
        void saveProfile();
      }}>
        <section class="field-section">
          <div class="field-number">01</div>
          <div class="field-heading">
            <div>
              <p>General preferences</p>
              <h2>How should the assistant work with you?</h2>
            </div>
            <span>{preferences.length.toLocaleString()} / {textLimit.toLocaleString()}</span>
          </div>
          <label for="profile-preferences">
            Preferences
            <textarea
              id="profile-preferences"
              bind:value={preferences}
              maxlength={textLimit}
              rows="8"
              placeholder="For example: Prefer concise answers, use SI units, and surface assumptions before recommendations."
            ></textarea>
          </label>
          <p class="field-help">
            Focus on stable working preferences rather than instructions for one task.
          </p>
        </section>

        <section class="field-section">
          <div class="field-number">02</div>
          <div class="field-heading">
            <div>
              <p>Style reference</p>
              <h2>What should your writing sound like?</h2>
            </div>
            <span>{styleReference.length.toLocaleString()} / {textLimit.toLocaleString()}</span>
          </div>
          <label for="profile-style-reference">
            Style notes and examples
            <textarea
              id="profile-style-reference"
              bind:value={styleReference}
              maxlength={textLimit}
              rows="11"
              placeholder="Describe voice, rhythm, vocabulary, and formatting. Add a short representative sample if useful."
            ></textarea>
          </label>
          <p class="field-help">
            A few precise principles usually work better than a long generic style guide.
          </p>
        </section>

        <section class="field-section security-section">
          <div class="field-number">03</div>
          <div class="field-heading security-heading">
            <div>
              <p>Personal security</p>
              <h2>How is protected data secured?</h2>
            </div>
            <Badge tone={profile.security.persistent ? "success" : "danger"}>
              {profile.security.persistent ? "Persistent key" : "Key unavailable"}
            </Badge>
          </div>

          <dl class="security-facts">
            <div>
              <dt>Envelope</dt>
              <dd>Version {profile.security.envelope_version} authenticated encryption</dd>
            </div>
            <div>
              <dt>Key backend</dt>
              <dd>{profile.security.key_backend.replaceAll("-", " ")}</dd>
            </div>
            <div>
              <dt>Active key</dt>
              <dd>{profile.security.active_key_id}</dd>
            </div>
            <div>
              <dt>Background work</dt>
              <dd>{profile.security.automation_ready ? "Available" : "Needs unlock"}</dd>
            </div>
          </dl>

          <div class="security-action">
            <Icon name="shield" size={18} />
            <p>{profile.security.note}</p>
            <p>
              Recovery material is never included in an ordinary backup. Export it
              separately from the Carlina command line and store it offline.
            </p>
          </div>
        </section>

        <footer class="form-footer">
          <div aria-live="polite">
            {#if error}
              <p class="form-message form-message--error">{error}</p>
            {:else if savedMessage}
              <p class="form-message">
                <Icon name="check" size={15} />
                {savedMessage}
              </p>
            {:else}
              <p class="form-hint">Changes are applied to future conversations.</p>
            {/if}
          </div>
          <Button type="submit" disabled={saving}>
            {saving ? "Saving…" : "Save preferences"}
          </Button>
        </footer>
      </form>
    </div>
  {:else}
    <section class="profile-error" role="alert">
      <Icon name="activity" size={22} />
      <h2>Profile unavailable</h2>
      <p>{error}</p>
      <Button variant="outline" onclick={() => void loadProfile()}>Try again</Button>
    </section>
  {/if}
</div>

<style>
  .profile-page {
    width: min(100%, 88rem);
    margin: 0 auto;
    padding: clamp(1.4rem, 4vw, 3.5rem);
  }

  .profile-layout {
    display: grid;
    grid-template-columns: minmax(14rem, 0.34fr) minmax(0, 1fr);
    border: 1px solid var(--color-rule-strong);
    margin-top: 1.5rem;
    background: var(--color-surface);
  }

  .profile-summary {
    border-right: 1px solid var(--color-rule-strong);
    padding: clamp(1.25rem, 3vw, 2.3rem);
    background:
      linear-gradient(145deg, var(--color-clay-muted), var(--color-paper) 58%);
  }

  .profile-mark {
    width: 4.25rem;
    height: 4.25rem;
    display: grid;
    place-items: center;
    border-radius: 50%;
    background: var(--color-clay);
    color: var(--color-surface);
    font-family: var(--font-editorial);
    font-size: 1.75rem;
  }

  .profile-label {
    margin: 2rem 0 0.35rem;
    color: var(--color-clay);
    font-size: 0.58rem;
    font-weight: 800;
    letter-spacing: 0.15em;
    text-transform: uppercase;
  }

  .profile-summary h2 {
    margin: 0 0 0.75rem;
    overflow-wrap: anywhere;
    font-family: var(--font-editorial);
    font-size: clamp(1.25rem, 2.6vw, 1.8rem);
    font-weight: 520;
  }

  .summary-note {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    gap: 0.7rem;
    border-top: 1px solid var(--color-rule-strong);
    margin-top: 2.2rem;
    padding-top: 1rem;
    color: var(--color-ink-muted);
  }

  .summary-note p {
    margin: 0;
    font-size: 0.72rem;
    line-height: 1.6;
  }

  form {
    min-width: 0;
  }

  .field-section {
    position: relative;
    border-bottom: 1px solid var(--color-rule-strong);
    padding: clamp(1.25rem, 3vw, 2.3rem);
  }

  .field-number {
    position: absolute;
    top: 1rem;
    right: 1.1rem;
    color: var(--color-rule-strong);
    font-family: var(--font-editorial);
    font-size: 0.72rem;
  }

  .field-heading {
    display: flex;
    align-items: end;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 1.2rem;
    padding-right: 2rem;
  }

  .field-heading p {
    margin: 0 0 0.3rem;
    color: var(--color-clay);
    font-size: 0.57rem;
    font-weight: 800;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }

  .field-heading h2 {
    margin: 0;
    font-family: var(--font-editorial);
    font-size: clamp(1.25rem, 2.5vw, 1.65rem);
    font-weight: 520;
  }

  .field-heading > span {
    flex: 0 0 auto;
    color: var(--color-ink-muted);
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.58rem;
  }

  label {
    display: grid;
    gap: 0.45rem;
    color: var(--color-ink-soft);
    font-size: 0.67rem;
    font-weight: 700;
    letter-spacing: 0.04em;
  }

  textarea {
    width: 100%;
    resize: vertical;
    border: 1px solid var(--color-rule);
    padding: 0.9rem 1rem;
    background: var(--color-paper);
    color: var(--color-ink);
    font-size: 0.82rem;
    line-height: 1.65;
  }

  textarea:hover {
    border-color: var(--color-rule-strong);
  }

  textarea:focus {
    border-color: var(--color-focus);
    outline: 2px solid var(--color-focus);
    outline-offset: 2px;
  }

  textarea::placeholder {
    color: var(--color-ink-muted);
  }

  .field-help {
    margin: 0.55rem 0 0;
    color: var(--color-ink-muted);
    font-size: 0.68rem;
    line-height: 1.5;
  }

  .security-heading {
    align-items: center;
  }

  .security-facts {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 1px;
    border: 1px solid var(--color-rule);
    margin: 0;
    background: var(--color-rule);
  }

  .security-facts div {
    min-height: 6rem;
    display: grid;
    align-content: center;
    gap: 0.35rem;
    padding: 1rem;
    background: var(--color-paper);
  }

  .security-facts dt,
  .security-facts dd {
    margin: 0;
  }

  .security-facts dt {
    color: var(--color-clay);
    font-size: 0.58rem;
    font-weight: 800;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .security-facts dd {
    overflow-wrap: anywhere;
    color: var(--color-ink-muted);
    font-size: 0.68rem;
    line-height: 1.55;
  }

  .security-action {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    border-top: 1px solid var(--color-rule);
    margin-top: 1rem;
    padding-top: 1rem;
  }

  .security-action p {
    flex: 1;
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.68rem;
    line-height: 1.5;
  }

  .form-footer {
    min-height: 5rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.9rem clamp(1.25rem, 3vw, 2.3rem);
    background: var(--color-paper);
  }

  .form-hint,
  .form-message {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    margin: 0;
    color: var(--color-ink-muted);
    font-size: 0.7rem;
  }

  .form-message {
    color: var(--color-lichen);
  }

  .form-message--error {
    color: var(--color-danger);
  }

  .profile-loading {
    display: grid;
    gap: 1px;
    border: 1px solid var(--color-rule);
    margin-top: 1.5rem;
    background: var(--color-rule);
  }

  .profile-loading span {
    height: 10rem;
    background: linear-gradient(
      90deg,
      var(--color-paper),
      var(--color-surface),
      var(--color-paper)
    );
    background-size: 200% 100%;
    animation: loading 1.5s ease-in-out infinite;
  }

  @keyframes loading {
    to {
      background-position: -200% 0;
    }
  }

  .profile-error {
    display: grid;
    justify-items: start;
    gap: 0.65rem;
    border-block: 1px solid var(--color-rule);
    margin-top: 1.5rem;
    padding: 2rem;
  }

  .profile-error h2,
  .profile-error p {
    margin: 0;
  }

  .profile-error h2 {
    font-family: var(--font-editorial);
    font-weight: 520;
  }

  .profile-error p {
    color: var(--color-danger);
    font-size: 0.75rem;
  }

  @media (max-width: 54rem) {
    .profile-layout {
      grid-template-columns: 1fr;
    }

    .profile-summary {
      border-right: 0;
      border-bottom: 1px solid var(--color-rule-strong);
    }
  }

  @media (max-width: 34rem) {
    .profile-page {
      padding-inline: 0.75rem;
    }

    .field-heading,
    .form-footer {
      align-items: stretch;
      flex-direction: column;
    }

    .security-facts {
      grid-template-columns: 1fr;
    }
  }
</style>
