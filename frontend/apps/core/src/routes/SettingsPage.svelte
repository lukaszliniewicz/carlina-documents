<script lang="ts">
  import type { Component } from "svelte";
  import type {
    FrontendApiClient,
    FrontendBootstrap,
  } from "@carlina/sdk";

  import SettingsNav from "./settings/SettingsNav.svelte";
  import {
    settingsRouteRedirect,
    settingsSectionFromRoute,
  } from "../lib/model-settings.ts";
  import {
    frontendApplication,
    frontendSettingsRoute,
  } from "../lib/application-profile.ts";

  interface Props {
    api: FrontendApiClient;
    bootstrap: FrontendBootstrap;
    route: string;
    navigate: (
      href: string,
      clientRoute?: boolean,
      options?: { replace?: boolean },
    ) => "client" | "document";
    refreshBootstrap: () => Promise<FrontendBootstrap>;
  }

  type SettingsSection = ReturnType<typeof settingsSectionFromRoute>;
  type SettingsSectionComponent = Component<Props>;
  type SettingsSectionModule = { default: unknown };

  const sectionLoaders: Record<
    SettingsSection,
    () => Promise<SettingsSectionModule>
  > = {
    profile: () => import("./ProfilePage.svelte"),
    guidance: () => import("./settings/GuidanceSettingsPage.svelte"),
    policy: () => import("./settings/AssistantPolicyPage.svelte"),
    models: () => import("./settings/ModelSettingsPage.svelte"),
    routing: () => import("./settings/RoutingSettingsPage.svelte"),
    sharing: () => import("./settings/CredentialSharingPage.svelte"),
    addons: () => import("./settings/AddonManagerPage.svelte"),
    administration: () => import("./settings/AdministrationPage.svelte"),
  };

  const sectionLabels: Record<SettingsSection, string> = {
    profile: "profile",
    guidance: "guidance",
    policy: "assistant policy",
    models: "model settings",
    routing: "shared routing",
    sharing: "credential sharing",
    addons: "addon desk",
    administration: "administration",
  };

  let {
    api,
    bootstrap,
    route,
    navigate,
    refreshBootstrap,
  }: Props = $props();

  const documents = $derived(frontendApplication(bootstrap).profile === "documents");
  const legacyRouteRedirect = $derived(
    frontendSettingsRoute(bootstrap, route) ?? settingsRouteRedirect(route),
  );
  const active = $derived(
    settingsSectionFromRoute(legacyRouteRedirect ?? route),
  );
  let SectionPage = $state<SettingsSectionComponent | null>(null);
  let sectionLoadError = $state("");

  $effect(() => {
    if (legacyRouteRedirect) {
      navigate(legacyRouteRedirect, true, { replace: true });
    }
  });

  $effect(() => {
    const section = active;
    let current = true;
    SectionPage = null;
    sectionLoadError = "";
    void sectionLoaders[section]()
      .then((module) => {
        if (current) {
          SectionPage = module.default as SettingsSectionComponent;
        }
      })
      .catch((reason: unknown) => {
        if (!current) return;
        sectionLoadError =
          reason instanceof Error
            ? reason.message
            : `The ${sectionLabels[section]} view could not be loaded.`;
      });
    return () => {
      current = false;
    };
  });

  const oauthNotice = $derived.by(() => {
    const parameters = new URL(route, "https://iarf.local").searchParams;
    const status = parameters.get("oauth_status");
    if (status === "success") {
      return {
        tone: "success",
        message: "The external account was connected successfully.",
      } as const;
    }
    if (status === "error") {
      const detail = (parameters.get("error") ?? "").slice(0, 500);
      return {
        tone: "error",
        message: detail
          ? `The external account could not be connected: ${detail}`
          : "The external account could not be connected.",
      } as const;
    }
    return null;
  });
</script>

<div class="settings-route">
  <SettingsNav
    {active}
    {navigate}
    isAdmin={bootstrap.user.is_admin}
    {documents}
  />
  {#if oauthNotice}
    <p
      class="oauth-notice"
      class:oauth-notice--error={oauthNotice.tone === "error"}
      role={oauthNotice.tone === "error" ? "alert" : "status"}
    >
      {oauthNotice.message}
    </p>
  {/if}
  {#if SectionPage}
    <SectionPage
      {api}
      {bootstrap}
      {route}
      {navigate}
      {refreshBootstrap}
    />
  {:else if sectionLoadError}
    <section class="settings-route-state" role="alert">
      <strong>{sectionLabels[active]} unavailable</strong>
      <span>{sectionLoadError}</span>
    </section>
  {:else}
    <section class="settings-route-state" aria-busy="true" role="status">
      <strong>Loading {sectionLabels[active]}</strong>
      <span>Preparing this settings workspace…</span>
    </section>
  {/if}
</div>

<style>
  .settings-route {
    min-height: 100%;
  }

  .oauth-notice {
    width: min(calc(100% - 2rem), 92rem);
    border-left: 2px solid var(--color-lichen);
    margin: 1rem auto 0;
    padding: 0.75rem 0.9rem;
    background: var(--color-lichen-muted);
    color: var(--color-lichen);
    font-size: 0.72rem;
  }

  .oauth-notice--error {
    border-color: var(--color-danger);
    background: var(--color-danger-muted);
    color: var(--color-danger);
  }

  .settings-route-state {
    width: min(calc(100% - 2rem), 92rem);
    display: grid;
    gap: 0.35rem;
    border: 1px solid var(--color-rule);
    margin: 1rem auto;
    padding: 1rem;
    background: var(--color-surface);
    color: var(--color-ink-muted);
    font-size: 0.75rem;
  }

  .settings-route-state strong {
    color: var(--color-ink);
    font-family: var(--font-editorial);
    font-size: 1rem;
    font-weight: 550;
  }
</style>
