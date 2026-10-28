<script lang="ts">
  import type {
    FrontendNavigationItem,
    FrontendProductIdentity,
    FrontendUserSummary,
  } from "@carlina/sdk";
  import { Icon, IconButton } from "@carlina/ui";

  interface Props {
    activeItem?: FrontendNavigationItem;
    product: FrontendProductIdentity;
    user: FrontendUserSummary;
    pickers: Array<{
      componentId: string;
      label: string;
      detail: string;
    }>;
    onpicker: (componentId: string) => void;
    oncommand: () => void;
    onnotifications: () => void;
    onrestart: () => void;
    restartEnabled?: boolean;
    notificationsEnabled?: boolean;
  }

  let {
    activeItem,
    product,
    user,
    pickers,
    onpicker,
    oncommand,
    onnotifications,
    onrestart,
    restartEnabled = true,
    notificationsEnabled = true,
  }: Props = $props();
  let pickerMenuOpen = $state(false);

  const initials = $derived(
    user.display_name
      .split(/[\s@._-]+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((part) => part[0]?.toUpperCase())
      .join("") || "U",
  );
</script>

<header>
  <div class="header-leading">
    <div class="breadcrumb">
      <span>{product.short_name}</span>
      <span class="breadcrumb__rule" aria-hidden="true"></span>
      <strong>{activeItem?.label ?? "Workspace"}</strong>
    </div>
  </div>

  <div class="header-actions">
    {#if pickers.length}
      <div class="picker-menu">
        <button
          class="picker-trigger"
          type="button"
          aria-haspopup="menu"
          aria-expanded={pickerMenuOpen}
          onclick={() => (pickerMenuOpen = !pickerMenuOpen)}
        >
          <Icon name="plus" size={16} />
          <span>Insert context</span>
        </button>
        {#if pickerMenuOpen}
          <div class="picker-menu__panel" role="menu">
            <p>Insert into chat</p>
            {#each pickers as picker (picker.componentId)}
              <button
                type="button"
                role="menuitem"
                onclick={() => {
                  pickerMenuOpen = false;
                  onpicker(picker.componentId);
                }}
              >
                <span>
                  <strong>{picker.label}</strong>
                  <small>{picker.detail}</small>
                </span>
                <Icon name="plus" size={14} />
              </button>
            {/each}
          </div>
        {/if}
      </div>
    {/if}
    <button class="command-trigger" onclick={oncommand} title="Open command menu">
      <Icon name="search" size={16} />
      <span>Find anything</span>
      <kbd><span>⌘</span>K</kbd>
    </button>
    {#if user.is_admin && restartEnabled}
      <button
        class="restart-trigger"
        onclick={onrestart}
        aria-label="Restart server"
        title="Restart server"
      >
        <Icon name="refresh" size={16} />
        <span>Restart</span>
      </button>
    {/if}
    {#if notificationsEnabled}
      <IconButton icon="bell" label="Open notifications" onclick={onnotifications} />
    {/if}
    <a class="avatar" href="/settings" aria-label={`${initials}: open profile for ${user.display_name}`} title={user.display_name}>
      {initials}
    </a>
  </div>
</header>

<style>
  header {
    position: sticky;
    z-index: 30;
    top: 0;
    height: var(--shell-header-height);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--color-rule);
    padding: 0 clamp(0.8rem, 2.4vw, 1.75rem);
    background: color-mix(in srgb, var(--color-paper) 93%, transparent);
    backdrop-filter: blur(12px);
  }

  .header-leading,
  .header-actions,
  .breadcrumb {
    display: flex;
    align-items: center;
  }

  .header-leading {
    min-width: 0;
    gap: 0.6rem;
  }

  .breadcrumb {
    min-width: 0;
    gap: 0.65rem;
    color: var(--color-ink-muted);
    font-size: 0.69rem;
    letter-spacing: 0.05em;
    text-transform: uppercase;
  }

  .breadcrumb strong {
    overflow: hidden;
    color: var(--color-ink);
    font-weight: 700;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .breadcrumb__rule {
    width: 1.7rem;
    height: 1px;
    background: var(--color-rule-strong);
  }

  .header-actions {
    gap: 0.3rem;
  }

  .picker-menu {
    position: relative;
  }

  .picker-trigger,
  .picker-menu__panel > button {
    display: flex;
    align-items: center;
    border: 0;
    background: transparent;
    color: var(--color-ink-muted);
    cursor: pointer;
  }

  .picker-trigger {
    min-height: 2.5rem;
    gap: 0.45rem;
    padding: 0 0.65rem;
    font-size: 0.68rem;
    font-weight: 650;
  }

  .picker-trigger:hover,
  .picker-trigger[aria-expanded="true"] {
    background: var(--color-clay-muted);
    color: var(--color-ink);
  }

  .picker-menu__panel {
    position: absolute;
    z-index: 45;
    top: calc(100% + 0.4rem);
    right: 0;
    width: min(20rem, calc(100vw - 1.5rem));
    max-height: min(28rem, calc(100vh - 6rem));
    overflow-y: auto;
    border: 1px solid var(--color-rule-strong);
    padding: 0.35rem;
    background: var(--color-surface);
    box-shadow: 0 0.8rem 2.4rem
      color-mix(in srgb, var(--color-ink) 15%, transparent);
  }

  .picker-menu__panel > p {
    margin: 0;
    padding: 0.45rem 0.6rem;
    color: var(--color-clay);
    font-size: 0.55rem;
    font-weight: 750;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  .picker-menu__panel > button {
    width: 100%;
    min-height: 2.9rem;
    justify-content: space-between;
    gap: 0.75rem;
    padding: 0.45rem 0.6rem;
    text-align: left;
  }

  .picker-menu__panel > button:hover {
    background: var(--color-surface-muted);
    color: var(--color-ink);
  }

  .picker-menu__panel > button > span {
    min-width: 0;
    display: grid;
    gap: 0.1rem;
  }

  .picker-menu__panel strong,
  .picker-menu__panel small {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .picker-menu__panel strong {
    font-size: 0.68rem;
    font-weight: 650;
  }

  .picker-menu__panel small {
    color: var(--color-ink-muted);
    font-size: 0.59rem;
  }

  .command-trigger {
    height: 2.5rem;
    display: flex;
    align-items: center;
    gap: 0.55rem;
    border: 1px solid var(--color-rule);
    margin-right: 0.3rem;
    padding: 0 0.45rem 0 0.75rem;
    background: var(--color-surface);
    color: var(--color-ink-muted);
    cursor: pointer;
    font-size: 0.72rem;
  }

  .command-trigger:hover {
    border-color: var(--color-rule-strong);
    color: var(--color-ink);
  }

  .restart-trigger {
    height: 2.5rem;
    display: inline-flex;
    align-items: center;
    gap: 0.45rem;
    border: 1px solid transparent;
    padding: 0 0.65rem;
    background: transparent;
    color: var(--color-ink-muted);
    cursor: pointer;
    font-size: 0.68rem;
    font-weight: 650;
  }

  .restart-trigger:hover {
    border-color: var(--color-danger);
    background: var(--color-danger-muted);
    color: var(--color-danger);
  }

  kbd {
    min-width: 2.25rem;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 0.08rem;
    border-left: 1px solid var(--color-rule);
    padding-left: 0.45rem;
    color: var(--color-ink-muted);
    font-family: inherit;
    font-size: 0.61rem;
  }

  .avatar {
    width: 2.1rem;
    height: 2.1rem;
    display: grid;
    place-items: center;
    border-radius: 50%;
    margin-left: 0.2rem;
    background: var(--color-clay);
    color: var(--color-surface);
    font-size: 0.62rem;
    font-weight: 750;
    letter-spacing: 0.04em;
    text-decoration: none;
  }

  @media (max-width: 38rem) {
    .command-trigger span,
    .command-trigger kbd,
    .picker-trigger span,
    .restart-trigger span,
    .breadcrumb > span:first-child,
    .breadcrumb__rule {
      display: none;
    }

    .command-trigger,
    .picker-trigger,
    .restart-trigger {
      width: 2.5rem;
      justify-content: center;
      padding: 0;
    }
  }
</style>
