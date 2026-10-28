<script lang="ts">
  import type { FrontendNavigationItem } from "@carlina/sdk";
  import { Icon, type IconName } from "@carlina/ui";

  interface Props {
    navigation: FrontendNavigationItem[];
    currentPath: string;
    onnavigate: (item: FrontendNavigationItem, event: MouseEvent) => void;
  }

  let { navigation, currentPath, onnavigate }: Props = $props();
  const items = $derived(
    navigation.filter((item) => item.section === "primary").slice(0, 4),
  );

  function iconName(value: string): IconName {
    return (["chat", "workspaces", "activity", "book"].includes(value)
      ? value
      : "workspaces") as IconName;
  }

  function isActive(item: FrontendNavigationItem): boolean {
    return (currentPath.split(/[?#]/, 1)[0].replace(/\/+$/, "") || "/") ===
      (item.href.split(/[?#]/, 1)[0].replace(/\/+$/, "") || "/");
  }
</script>

<nav aria-label="Mobile destinations">
  {#each items as item (item.id)}
    <a
      href={item.href}
      class:active={isActive(item)}
      aria-current={isActive(item) ? "page" : undefined}
      onclick={(event) => onnavigate(item, event)}
    >
      <Icon name={iconName(item.icon)} size={17} />
      <span>{item.label}</span>
    </a>
  {/each}
</nav>

<style>
  nav {
    position: fixed;
    z-index: 35;
    inset: auto 0 0;
    height: 3.75rem;
    display: none;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    border-top: 1px solid var(--color-rule-strong);
    background: var(--color-surface);
  }

  a {
    min-width: 0;
    display: grid;
    place-content: center;
    justify-items: center;
    gap: 0.24rem;
    border-right: 1px solid var(--color-rule);
    color: var(--color-ink-muted);
    font-size: 0.58rem;
    font-weight: 650;
    text-decoration: none;
  }

  a:last-child {
    border-right: 0;
  }

  a.active {
    background: var(--color-clay-muted);
    color: var(--color-ink);
    box-shadow: inset 0 3px 0 var(--color-clay);
  }

  a span {
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  @media (max-width: 64rem) {
    nav {
      display: grid;
    }
  }
</style>
