<script lang="ts" module>
  export type IconName =
    | "activity"
    | "arrow-left"
    | "arrow-right"
    | "arrow-down"
    | "arrow-up"
    | "arrow-up-right"
    | "attach"
    | "bell"
    | "book"
    | "brain"
    | "chat"
    | "check"
    | "chevron-left"
    | "chevron-right"
    | "close"
    | "code"
    | "command"
    | "copy"
    | "crop"
    | "download"
    | "edit"
    | "eye"
    | "file"
    | "folder"
    | "fork"
    | "logs"
    | "menu"
    | "microphone"
    | "more"
    | "image"
    | "pause"
    | "play"
    | "plus"
    | "refresh"
    | "search"
    | "send"
    | "settings"
    | "shield"
    | "stop"
    | "trash"
    | "upload"
    | "user"
    | "volume"
    | "warning"
    | "workspaces";
</script>

<script lang="ts">
  interface Props {
    name: IconName;
    size?: number;
    class?: string;
    label?: string;
  }

  let {
    name,
    size = 18,
    class: className = "",
    label,
  }: Props = $props();
</script>

<svg
  class={className}
  width={size}
  height={size}
  viewBox="0 0 24 24"
  fill="none"
  stroke="currentColor"
  stroke-width="1.7"
  stroke-linecap="square"
  stroke-linejoin="miter"
  aria-hidden={label ? undefined : "true"}
  aria-label={label}
  role={label ? "img" : undefined}
  focusable="false"
>
  {#if name === "activity"}
    <path d="M3 12h4l2.2-6 4.1 12 2.1-6H21" />
  {:else if name === "arrow-left"}
    <path d="m11 5-7 7 7 7M4 12h16" />
  {:else if name === "arrow-right"}
    <path d="m13 5 7 7-7 7M4 12h16" />
  {:else if name === "arrow-down"}
    <path d="m5 13 7 7 7-7M12 4v16" />
  {:else if name === "arrow-up"}
    <path d="m5 11 7-7 7 7M12 20V4" />
  {:else if name === "arrow-up-right"}
    <path d="M7 17 17 7M8 7h9v9" />
  {:else if name === "attach"}
    <path d="m20.5 11.5-8.9 8.9a6 6 0 0 1-8.5-8.5l9.6-9.6a4 4 0 0 1 5.7 5.7l-9.7 9.7a2 2 0 0 1-2.8-2.8l8.9-8.9" />
  {:else if name === "bell"}
    <path d="M18 9a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4" />
  {:else if name === "book"}
    <path d="M4 4.5A2.5 2.5 0 0 1 6.5 2H11v17H6.5A2.5 2.5 0 0 0 4 21.5zM20 4.5A2.5 2.5 0 0 0 17.5 2H13v17h4.5a2.5 2.5 0 0 1 2.5 2.5z" />
  {:else if name === "brain"}
    <path d="M9.5 4.5A3 3 0 0 0 4 6v2a3 3 0 0 0-1 5.2V15a3 3 0 0 0 5 2.2V20h3V5.5a2 2 0 0 0-1.5-1zM14.5 4.5A3 3 0 0 1 20 6v2a3 3 0 0 1 1 5.2V15a3 3 0 0 1-5 2.2V20h-3V5.5a2 2 0 0 1 1.5-1z" />
  {:else if name === "chat"}
    <path d="M4 4h16v12H9l-5 4z" />
    <path d="M8 8h8M8 12h5" />
  {:else if name === "check"}
    <path d="m5 12 4 4L19 6" />
  {:else if name === "chevron-left"}
    <path d="m15 5-7 7 7 7" />
  {:else if name === "chevron-right"}
    <path d="m9 5 7 7-7 7" />
  {:else if name === "close"}
    <path d="M5 5l14 14M19 5 5 19" />
  {:else if name === "code"}
    <path d="m8 5-6 7 6 7M16 5l6 7-6 7M14 3l-4 18" />
  {:else if name === "command"}
    <path d="M9 6V5a3 3 0 1 0-3 3h12a3 3 0 1 0-3-3v14a3 3 0 1 0 3-3H6a3 3 0 1 0 3 3z" />
  {:else if name === "copy"}
    <path d="M8 8h12v12H8zM4 16H3V4h12v1" />
  {:else if name === "crop"}
    <path d="M7 3v14a2 2 0 0 0 2 2h12M3 7h14a2 2 0 0 1 2 2v12" />
  {:else if name === "download"}
    <path d="M12 3v12m-5-5 5 5 5-5M4 20h16" />
  {:else if name === "edit"}
    <path d="m4 16-1 5 5-1L19 9l-4-4zM13.5 6.5l4 4M3 21h18" />
  {:else if name === "eye"}
    <path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12z" />
    <circle cx="12" cy="12" r="2.5" />
  {:else if name === "file"}
    <path d="M5 2h9l5 5v15H5zM14 2v6h5M8 13h8M8 17h6" />
  {:else if name === "folder"}
    <path d="M3 5h7l2 2h9v12H3z" />
  {:else if name === "fork"}
    <circle cx="6" cy="5" r="2" />
    <circle cx="18" cy="5" r="2" />
    <circle cx="12" cy="19" r="2" />
    <path d="M6 7v2c0 3 2 4 6 4s6-1 6-4V7M12 13v4" />
  {:else if name === "logs"}
    <path d="M5 3h14v18H5zM8 7h8M8 11h8M8 15h5" />
  {:else if name === "menu"}
    <path d="M4 7h16M4 12h16M4 17h16" />
  {:else if name === "microphone"}
    <rect x="8" y="3" width="8" height="12" rx="4" />
    <path d="M5 11a7 7 0 0 0 14 0M12 18v3M8 21h8" />
  {:else if name === "more"}
    <circle cx="5" cy="12" r="1" fill="currentColor" stroke="none" />
    <circle cx="12" cy="12" r="1" fill="currentColor" stroke="none" />
    <circle cx="19" cy="12" r="1" fill="currentColor" stroke="none" />
  {:else if name === "image"}
    <rect x="3" y="4" width="18" height="16" />
    <circle cx="9" cy="9" r="2" />
    <path d="m4 18 5-5 3 3 3-4 5 6" />
  {:else if name === "pause"}
    <path d="M8 5v14M16 5v14" />
  {:else if name === "play"}
    <path d="m8 5 11 7-11 7z" />
  {:else if name === "plus"}
    <path d="M12 4v16M4 12h16" />
  {:else if name === "refresh"}
    <path d="M20 7v5h-5M4 17v-5h5" />
    <path d="M6.1 8A7 7 0 0 1 18 6l2 6M17.9 16A7 7 0 0 1 6 18l-2-6" />
  {:else if name === "search"}
    <circle cx="10.5" cy="10.5" r="6.5" />
    <path d="m15.5 15.5 5 5" />
  {:else if name === "send"}
    <path d="m3 11 18-8-8 18-2-8zM11 13l4-4" />
  {:else if name === "settings"}
    <circle cx="12" cy="12" r="3" />
    <path d="M12 2v3M12 19v3M4.9 4.9 7 7M17 17l2.1 2.1M2 12h3M19 12h3M4.9 19.1 7 17M17 7l2.1-2.1" />
  {:else if name === "shield"}
    <path d="M12 2 20 5v6c0 5-3.2 8.5-8 11-4.8-2.5-8-6-8-11V5zM9 12l2 2 4-5" />
  {:else if name === "stop"}
    <rect x="5" y="5" width="14" height="14" />
  {:else if name === "trash"}
    <path d="M4 7h16M9 3h6l1 4H8zM6 7l1 14h10l1-14M10 11v6M14 11v6" />
  {:else if name === "upload"}
    <path d="M12 17V5m-5 5 5-5 5 5M4 20h16" />
  {:else if name === "user"}
    <circle cx="12" cy="8" r="4" />
    <path d="M4 21a8 8 0 0 1 16 0" />
  {:else if name === "volume"}
    <path d="M4 9h4l5-4v14l-5-4H4zM17 9a4 4 0 0 1 0 6M19 6a8 8 0 0 1 0 12" />
  {:else if name === "warning"}
    <path d="M12 3 2 21h20zM12 9v5M12 18h.01" />
  {:else if name === "workspaces"}
    <path d="M3 4h8v7H3zM13 4h8v4h-8zM3 13h8v7H3zM13 10h8v10h-8z" />
  {/if}
</svg>
