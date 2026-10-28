<script lang="ts">
  import { onMount } from "svelte";
  import type { FrontendApiClient } from "@carlina/sdk";

  interface Props {
    api: FrontendApiClient;
    documents: boolean;
  }

  interface DistributionInfo {
    application_profile: string;
    application_version?: string;
    source_revision?: string;
    source: {
      available: boolean;
      sha256?: string;
      download_path?: string;
    };
    companions?: {
      available: boolean;
      coverage?: string;
      application_version?: string;
      source_revision?: string;
      application_source_sha256?: string;
      downloads?: unknown;
    };
  }

  interface CompanionLink {
    label: string;
    url: string;
    filename: string;
  }

  function downloadUrl(value: unknown, filename: string): string | undefined {
    if (typeof value !== "string" || value.length > 2048 || /[^\x21-\x7e]|[\\?#]/.test(value)) return;
    try {
      const url = new URL(value);
      const local = /^(127\.)\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(url.hostname) || url.hostname === "[::1]";
      if (
        (url.protocol !== "https:" && !(url.protocol === "http:" && local)) ||
        !url.hostname || url.username || url.password || url.search || url.hash
      ) return;
      const path = decodeURIComponent(url.pathname);
      const unsafePath = [...path].some((character) => {
        const code = character.charCodeAt(0);
        return code <= 0x20 || (code >= 0x7f && code <= 0x9f) || character === "\\";
      });
      if (unsafePath || path.split("/").some((part) => part === "." || part === "..")) return;
      if (path.split("/").at(-1) === filename) return value;
    } catch {
      // Malformed URLs are not downloads.
    }
  }

  function companionLinks(info: DistributionInfo, sourceSha: string): CompanionLink[] {
    const metadata = info.companions;
    if (
      metadata?.available !== true || metadata.coverage !== "selected" ||
      !info.application_version || !/^[0-9a-f]{40}$/.test(info.source_revision ?? "") ||
      metadata.application_version !== info.application_version ||
      metadata.source_revision !== info.source_revision ||
      metadata.application_source_sha256 !== sourceSha ||
      !Array.isArray(metadata.downloads) || metadata.downloads.length > 2
    ) return [];
    const links: CompanionLink[] = [];
    const kinds = new Set<string>();
    for (const download of metadata.downloads) {
      if (typeof download !== "object" || download === null || Array.isArray(download)) return [];
      const { kind, filename, byte_length, sha256, download_url, coverage } = download as Record<string, unknown>;
      if (
        (kind !== "dependency_sources" && kind !== "dependency_notices") || kinds.has(kind) ||
        typeof filename !== "string" || filename.length > 200 || filename.includes("..") ||
        !/^[A-Za-z0-9][A-Za-z0-9._+-]*\.zip$/.test(filename) ||
        typeof byte_length !== "number" || !Number.isSafeInteger(byte_length) || byte_length <= 0 || byte_length > 4 * 1024 ** 3 ||
        typeof sha256 !== "string" || !/^[0-9a-f]{64}$/.test(sha256) || coverage !== "selected"
      ) return [];
      const url = downloadUrl(download_url, filename);
      if (!url) return [];
      kinds.add(kind);
      links.push({ label: kind === "dependency_sources" ? "Dependency sources" : "Third-party notices", url, filename });
    }
    return links;
  }

  let { api, documents }: Props = $props();
  let sourcePath = $state("");
  let downloads = $state<CompanionLink[]>([]);

  onMount(() => {
    if (!documents) return;
    const controller = new AbortController();
    void api.request<DistributionInfo>("/api/application-distribution", {
      signal: controller.signal,
    }).then((info) => {
      const source = info.source;
      if (
        !controller.signal.aborted &&
        info.application_profile === "documents" &&
        source?.available === true &&
        typeof source.sha256 === "string" &&
        /^[0-9a-f]{64}$/.test(source.sha256) &&
        source.download_path === `/api/application-source/${source.sha256}`
      ) {
        sourcePath = source.download_path;
        downloads = companionLinks(info, source.sha256);
      }
    }).catch(() => {
      // Older previews may have no bundled source offer.
    });
    return () => controller.abort();
  });
</script>

{#if sourcePath}
  <aside class="application-source" aria-label="Source and licence files">
    <a href={sourcePath} download>Source code</a>
    {#each downloads as download (download.label)}
      <a href={download.url} download={download.filename} rel="noopener noreferrer" title="Selected files for this release">{download.label}</a>
    {/each}
  </aside>
{/if}

<style>
  .application-source {
    display: flex;
    flex-wrap: wrap;
    column-gap: 1rem;
    justify-content: flex-end;
    padding: 0 clamp(1rem, 3vw, 2rem);
    border-bottom: 1px solid var(--color-rule);
  }

  a {
    display: inline-flex;
    align-items: center;
    min-height: 2.75rem;
    color: var(--color-ink-muted);
    font-size: 0.78rem;
    text-underline-offset: 0.2rem;
  }

  a:focus-visible {
    outline: 2px solid var(--color-clay);
    outline-offset: 2px;
  }
</style>
