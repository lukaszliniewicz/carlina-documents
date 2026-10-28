<script lang="ts">
  import { shouldUseClientNavigation } from "@carlina/sdk";
  import { Icon } from "@carlina/ui";
  import type { SettingsSection } from "../../lib/model-settings.ts";

  interface Props {
    active: SettingsSection;
    isAdmin: boolean;
    documents?: boolean;
    navigate: (
      href: string,
      clientRoute?: boolean,
      options?: { replace?: boolean },
    ) => "client" | "document";
  }

  let { active, isAdmin, documents = false, navigate }: Props = $props();
  let links = $state<HTMLDivElement>();
  const sections = $derived([
    { id: "profile", label: "Profile", href: "/settings" },
    ...(isAdmin && !documents ? [
      { id: "guidance", label: "Guidance", href: "/settings/guidance" },
      { id: "policy", label: "Policy", href: "/settings/policy" },
    ] : []),
    { id: "models", label: "Models", href: "/settings/models" },
    { id: "routing", label: "Routing", href: "/settings/routing" },
    { id: "sharing", label: "Sharing", href: "/settings/sharing" },
    { id: "addons", label: "Addons", href: "/settings/addons" },
    ...(isAdmin && !documents ? [
      { id: "administration", label: "Administration", href: "/settings/administration" },
    ] : []),
  ]);

  function revealActiveLink(): void {
    if (
      !links ||
      typeof window === "undefined" ||
      !window.matchMedia("(max-width: 68rem)").matches
    ) {
      return;
    }
    links
      .querySelector<HTMLAnchorElement>('a[aria-current="page"]')
      ?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  function scrollLinks(event: KeyboardEvent): void {
    if (!links || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      return;
    }
    event.preventDefault();
    if (event.key === "Home" || event.key === "End") {
      links.scrollTo({ left: event.key === "Home" ? 0 : links.scrollWidth, behavior: "smooth" });
      return;
    }
    links.scrollBy({
      left: event.key === "ArrowLeft" ? -links.clientWidth * 0.7 : links.clientWidth * 0.7,
      behavior: "smooth",
    });
  }

  $effect(() => {
    active;
    if (typeof window === "undefined") return;
    const frame = window.requestAnimationFrame(revealActiveLink);
    return () => window.cancelAnimationFrame(frame);
  });

  function activate(href: string, event: MouseEvent): void {
    if (!shouldUseClientNavigation(event)) return;
    event.preventDefault();
    navigate(href, true);
  }
</script>

<nav aria-label="Settings sections">
  <div class="settings-nav__heading">
    <span>Settings desk</span>
    <p>{documents ? "Models, access, and document tools" : "Personal context, intelligence, access, and capabilities"}</p>
  </div>
  <div
    class="settings-nav__links"
    role="group"
    bind:this={links}
  >
    {#each sections as section, index (section.id)}
      <a
        href={section.href}
        class:active={active === section.id}
        aria-current={active === section.id ? "page" : undefined}
        onclick={(event) => activate(section.href, event)}
        onkeydown={scrollLinks}
      >
        <span>{String(index + 1).padStart(2, "0")}</span>
        {section.label}
        {#if section.id === "administration"}
          <Icon name="shield" size={14} />
        {/if}
      </a>
    {/each}
  </div>
</nav>

<style>
  nav {
    width: min(100%, 100rem);
    min-width: 0;
    min-height: 4.3rem;
    display: flex;
    align-items: stretch;
    justify-content: space-between;
    gap: 1.5rem;
    border-bottom: 1px solid var(--color-rule-strong);
    margin: 0 auto;
    padding-inline: clamp(1.4rem, 4vw, 3.5rem);
    background: var(--color-paper);
  }

  .settings-nav__heading {
    display: flex;
    align-items: baseline;
    gap: 0.75rem;
    padding-block: 1.25rem 0.85rem;
  }

  .settings-nav__heading span {
    color: var(--color-clay);
    font-size: 0.62rem;
    font-weight: 800;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }

  .settings-nav__heading p {
    margin: 0;
    color: var(--color-ink-muted);
    font-family: var(--font-editorial);
    font-size: 0.9rem;
  }

  .settings-nav__links {
    min-width: 0;
    display: flex;
    align-items: stretch;
  }

  a {
    min-width: 7rem;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.42rem;
    border-inline-start: 1px solid var(--color-rule);
    padding: 0.8rem 1rem;
    color: var(--color-ink-muted);
    font-size: 0.72rem;
    font-weight: 680;
    text-decoration: none;
  }

  a:last-child {
    border-inline-end: 1px solid var(--color-rule);
  }

  a span {
    color: var(--color-ink-muted);
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.58rem;
  }

  a:hover {
    background: var(--color-surface);
    color: var(--color-ink);
  }

  a.active {
    box-shadow: inset 0 -3px 0 var(--color-clay);
    background: var(--color-surface);
    color: var(--color-ink);
  }

  a:focus-visible {
    outline: 2px solid var(--color-focus);
    outline-offset: -2px;
  }

  @media (max-width: 82rem) {
    .settings-nav__heading p {
      display: none;
    }
  }

  @media (max-width: 68rem) {
    nav {
      display: block;
      padding-inline: 0;
    }

    .settings-nav__heading {
      padding-inline: 1rem;
    }

    .settings-nav__links {
      max-width: 100%;
      overflow-x: auto;
      overscroll-behavior-inline: contain;
      border-top: 1px solid var(--color-rule);
      scrollbar-width: none;
      touch-action: pan-x;
    }

    .settings-nav__links::-webkit-scrollbar {
      display: none;
    }

    a {
      flex: 0 0 auto;
    }
  }

  @media (max-width: 34rem) {
    .settings-nav__heading p {
      display: none;
    }

    a {
      min-width: 6.5rem;
      flex-direction: column;
      gap: 0.15rem;
      padding: 0.52rem 0.7rem;
    }
  }
</style>
