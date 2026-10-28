<script lang="ts">
  import { onMount } from "svelte";
  import type {
    FrontendApiClient,
    FrontendChatDirectory,
    FrontendChatSummary,
    FrontendNavigationItem,
    FrontendProductIdentity,
    FrontendUserSummary,
  } from "@carlina/sdk";
  import { Icon, type IconName } from "@carlina/ui";

  interface Props {
    navigation: FrontendNavigationItem[];
    product: FrontendProductIdentity;
    user: FrontendUserSummary;
    api: FrontendApiClient;
    chatEnabled: boolean;
    currentPath: string;
    collapsed: boolean;
    buildId: string | null;
    applicationLabel?: string;
    homeHref?: string;
    onnavigate: (item: FrontendNavigationItem, event: MouseEvent) => void;
    onnavigatehref: (href: string, event: MouseEvent) => void;
    ontoggle: () => void;
  }

  let {
    navigation,
    product,
    user,
    api,
    chatEnabled,
    currentPath,
    collapsed,
    buildId,
    applicationLabel = "Workbench",
    homeHref = "/",
    onnavigate,
    onnavigatehref,
    ontoggle,
  }: Props = $props();

  let recentChats = $state<FrontendChatSummary[]>([]);
  let recentLoading = $state(false);

  const primary = $derived(navigation.filter((item) => item.section === "primary"));
  const utility = $derived(navigation.filter((item) => item.section !== "primary"));
  const initials = $derived(
    user.display_name
      .split(/[\s@._-]+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((part) => part[0]?.toUpperCase())
      .join("") || "U",
  );

  function iconName(value: string): IconName {
    const supported: IconName[] = [
      "activity",
      "book",
      "chat",
      "logs",
      "settings",
      "workspaces",
    ];
    return supported.includes(value as IconName)
      ? (value as IconName)
      : "workspaces";
  }

  function isActive(item: FrontendNavigationItem): boolean {
    const path = currentPath.split(/[?#]/, 1)[0].replace(/\/+$/, "") || "/";
    const href = item.href.split(/[?#]/, 1)[0].replace(/\/+$/, "") || "/";
    return path === href;
  }

  function isChatActive(id: string): boolean {
    const url = new URL(currentPath, "https://iarf.local");
    return url.pathname === "/" && url.searchParams.get("chat") === id;
  }

  async function loadRecentChats() {
    if (!chatEnabled || recentLoading) return;
    recentLoading = true;
    try {
      const payload = await api.request<FrontendChatDirectory>(
        "/api/frontend/chats?limit=10",
      );
      recentChats = payload.items;
    } catch {
      recentChats = [];
    } finally {
      recentLoading = false;
    }
  }

  onMount(() => {
    if (!chatEnabled) return;
    void loadRecentChats();
    const refresh = () => void loadRecentChats();
    window.addEventListener("iarf:chats-changed", refresh);
    return () => window.removeEventListener("iarf:chats-changed", refresh);
  });
</script>

<aside class:collapsed aria-label="Main navigation">
  <div class="brand">
    <a href={homeHref} aria-label={`${product.name} ${applicationLabel} home`}>
      <span class="brand__mark">{product.mark}</span>
      <span class="brand__text">
        <strong>{product.name}</strong>
        <small>{applicationLabel}</small>
      </span>
    </a>
  </div>

  <nav aria-label="Core destinations">
    <p class="nav-label">Desk</p>
    <ul>
      {#each primary as item (item.id)}
        <li>
          <a
            href={item.href}
            class:active={isActive(item)}
            aria-current={isActive(item) ? "page" : undefined}
            title={collapsed ? item.label : undefined}
            onclick={(event) => onnavigate(item, event)}
          >
            <Icon name={iconName(item.icon)} size={18} />
            <span>{item.label}</span>
          </a>
        </li>
      {/each}
    </ul>

    {#if chatEnabled && !collapsed}
      <div class="recent-chats">
        <div class="recent-chats__heading">
          <p class="nav-label">Recent</p>
          <a
            href="/"
            title="New conversation"
            onclick={(event) => onnavigatehref("/", event)}
          >
            <Icon name="plus" size={14} />
            <span class="sr-only">New conversation</span>
          </a>
        </div>
        {#if recentLoading && !recentChats.length}
          <p class="recent-chats__loading">Loading conversations…</p>
        {:else}
          <ul>
            {#each recentChats as chat (chat.id)}
              <li>
                <a
                  class="recent-chat"
                  class:active={isChatActive(chat.id)}
                  href={`/?chat=${encodeURIComponent(chat.id)}`}
                  aria-current={isChatActive(chat.id) ? "page" : undefined}
                  onclick={(event) =>
                    onnavigatehref(
                      `/?chat=${encodeURIComponent(chat.id)}`,
                      event,
                    )}
                >
                  <span>{chat.title}</span>
                  <small>{chat.message_count}</small>
                </a>
              </li>
            {/each}
          </ul>
        {/if}
      </div>
    {/if}

    <p class="nav-label utility-label">System</p>
    <ul>
      {#each utility as item (item.id)}
        <li>
          <a
            href={item.href}
            class:active={isActive(item)}
            aria-current={isActive(item) ? "page" : undefined}
            title={collapsed ? item.label : undefined}
            onclick={(event) => onnavigate(item, event)}
          >
            <Icon name={iconName(item.icon)} size={18} />
            <span>{item.label}</span>
          </a>
        </li>
      {/each}
    </ul>
  </nav>

  <div class="sidebar-footer">
    <a
      class="identity"
      href="/settings"
      title={collapsed ? user.display_name : undefined}
    >
      <span class="identity__mark">{initials}</span>
      <span class="identity__copy">
        <strong>{user.display_name}</strong>
        <small>{user.is_admin ? "Administrator" : "Member"}</small>
      </span>
    </a>
    <div class="build">
      <span>Core</span>
      <span>{buildId ? buildId.slice(0, 7) : "local"}</span>
    </div>
    <button class="collapse-toggle" onclick={ontoggle} aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}>
      <Icon name={collapsed ? "chevron-right" : "chevron-left"} size={16} />
      <span>Collapse</span>
    </button>
  </div>
</aside>

<style>
  aside {
    position: fixed;
    z-index: 40;
    inset: 0 auto 0 0;
    width: var(--shell-sidebar-width);
    display: flex;
    flex-direction: column;
    border-right: 1px solid var(--color-rule-strong);
    background:
      linear-gradient(180deg, var(--color-surface) 0, var(--color-paper) 72%);
    transition: width var(--shell-transition), transform var(--shell-transition);
  }

  .brand {
    height: var(--shell-header-height);
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 1px solid var(--color-rule);
    padding: 0 0.7rem;
  }

  .brand > a {
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 0.75rem;
    text-decoration: none;
  }

  .brand__mark {
    width: 2.35rem;
    height: 2.35rem;
    display: grid;
    flex: 0 0 auto;
    place-items: center;
    border: 1px solid var(--color-rule-strong);
    color: var(--color-clay);
    font-family: var(--font-editorial);
    font-size: 1.3rem;
  }

  .brand__text,
  .identity__copy {
    min-width: 0;
    display: grid;
    line-height: 1.12;
  }

  .brand__text strong {
    font-family: var(--font-editorial);
    font-size: 1rem;
    font-weight: 600;
    letter-spacing: 0.04em;
  }

  .brand__text small {
    color: var(--color-ink-muted);
    font-size: 0.61rem;
    letter-spacing: 0.13em;
    text-transform: uppercase;
  }

  nav {
    min-height: 0;
    flex: 1;
    overflow-y: auto;
    padding: 1.35rem 0.65rem;
    scrollbar-color: transparent transparent;
  }

  nav::-webkit-scrollbar-thumb {
    background: transparent;
  }

  aside:hover nav,
  nav:focus-within {
    scrollbar-color: var(--scrollbar-thumb) transparent;
  }

  aside:hover nav::-webkit-scrollbar-thumb,
  nav:focus-within::-webkit-scrollbar-thumb {
    background: var(--scrollbar-thumb);
    background-clip: padding-box;
  }

  aside:hover nav::-webkit-scrollbar-thumb:hover,
  nav:focus-within::-webkit-scrollbar-thumb:hover {
    background: var(--scrollbar-thumb-hover);
    background-clip: padding-box;
  }

  .nav-label {
    margin: 0 0 0.45rem;
    padding: 0 0.65rem;
    color: var(--color-ink-muted);
    font-size: 0.59rem;
    font-weight: 750;
    letter-spacing: 0.17em;
    text-transform: uppercase;
  }

  .utility-label {
    margin-top: 1.5rem;
  }

  .recent-chats {
    margin-top: 1.35rem;
    border-top: 1px solid var(--color-rule);
    padding-top: 1rem;
  }

  .recent-chats__heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding-right: 0.35rem;
  }

  .recent-chats__heading .nav-label {
    margin: 0;
  }

  .recent-chats__heading > a {
    width: 1.8rem;
    min-height: 1.8rem;
    justify-content: center;
    border: 0;
    padding: 0;
  }

  .recent-chats__loading {
    margin: 0.65rem;
    color: var(--color-ink-muted);
    font-size: 0.62rem;
  }

  nav a.recent-chat {
    min-height: 2rem;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 0.5rem;
    padding-inline: 0.65rem;
    color: var(--color-ink-muted);
    font-family: var(--font-editorial);
    font-size: 0.72rem;
    font-weight: 500;
  }

  .recent-chat span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .recent-chat small {
    font-family: var(--font-sans);
    font-size: 0.52rem;
  }

  .recent-chat.active {
    box-shadow: inset 2px 0 0 var(--color-clay) !important;
  }

  ul {
    display: grid;
    gap: 0.18rem;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  nav a {
    min-height: 2.65rem;
    display: flex;
    align-items: center;
    gap: 0.75rem;
    border: 1px solid transparent;
    padding: 0 0.68rem;
    color: var(--color-ink-soft);
    font-size: 0.79rem;
    font-weight: 560;
    text-decoration: none;
    transition:
      border-color var(--shell-transition),
      background var(--shell-transition),
      color var(--shell-transition);
  }

  nav a:hover {
    border-color: var(--color-rule);
    background: color-mix(in srgb, var(--color-surface-muted) 70%, transparent);
    color: var(--color-ink);
  }

  nav a.active {
    border-color: var(--color-rule-strong);
    background: var(--color-clay-muted);
    box-shadow: inset 3px 0 0 var(--color-clay);
    color: var(--color-ink);
  }

  .sidebar-footer {
    border-top: 1px solid var(--color-rule);
    padding: 0.65rem;
  }

  .identity {
    min-height: 3.15rem;
    display: flex;
    align-items: center;
    gap: 0.72rem;
    padding: 0.45rem;
    color: inherit;
    text-decoration: none;
  }

  .identity:hover {
    background: var(--color-surface-muted);
  }

  .identity__mark {
    width: 2rem;
    height: 2rem;
    display: grid;
    flex: 0 0 auto;
    place-items: center;
    border-radius: 50%;
    background: var(--color-lichen);
    color: var(--color-surface);
    font-size: 0.65rem;
    font-weight: 750;
    letter-spacing: 0.04em;
  }

  .identity__copy strong {
    overflow: hidden;
    font-size: 0.73rem;
    font-weight: 650;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .identity__copy small {
    color: var(--color-ink-muted);
    font-size: 0.62rem;
  }

  .build {
    display: flex;
    justify-content: space-between;
    border-top: 1px solid var(--color-rule);
    margin-top: 0.45rem;
    padding: 0.65rem 0.45rem 0.2rem;
    color: var(--color-ink-muted);
    font-family: ui-monospace, "Cascadia Code", monospace;
    font-size: 0.58rem;
    letter-spacing: 0.04em;
  }

  .collapse-toggle {
    width: 100%;
    min-height: 2.25rem;
    display: flex;
    align-items: center;
    gap: 0.65rem;
    border: 0;
    margin-top: 0.35rem;
    padding: 0 0.48rem;
    background: transparent;
    color: var(--color-ink-muted);
    cursor: pointer;
    font-size: 0.68rem;
  }

  .collapse-toggle:hover {
    background: var(--color-surface-muted);
    color: var(--color-ink);
  }

  aside.collapsed {
    width: var(--shell-sidebar-rail-width);
  }

  .collapsed .brand {
    justify-content: center;
  }

  .collapsed .brand__text,
  .collapsed .nav-label,
  .collapsed nav a span,
  .collapsed .identity__copy,
  .collapsed .build,
  .collapsed .collapse-toggle span {
    display: none;
  }

  .collapsed nav {
    padding-inline: 0.55rem;
  }

  .collapsed nav a,
  .collapsed .identity,
  .collapsed .collapse-toggle {
    justify-content: center;
    padding-inline: 0;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }

  @media (max-width: 64rem) {
    aside,
    aside.collapsed {
      width: min(18rem, calc(100vw - 3rem));
      transform: translateX(-100%);
    }

    .collapsed .brand {
      justify-content: space-between;
    }

    .collapsed .brand__text,
    .collapsed .nav-label,
    .collapsed nav a span,
    .collapsed .identity__copy,
    .collapsed .build,
    .collapsed .collapse-toggle span {
      display: initial;
    }

    .collapsed nav a,
    .collapsed .identity,
    .collapsed .collapse-toggle {
      justify-content: flex-start;
      padding-inline: 0.68rem;
    }

    .collapse-toggle {
      display: none;
    }
  }
</style>
