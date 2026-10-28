import { render, screen, waitFor } from "@testing-library/svelte";
import { describe, expect, it, vi } from "vitest";
import type { FrontendApiClient } from "@carlina/sdk";

import ApplicationSourceLink from "../../src/components/ApplicationSourceLink.svelte";

const digest = "a".repeat(64);
const sourcePath = `/api/application-source/${digest}`;
const revision = "b".repeat(40);

function distributionWithCompanions() {
  return {
    application_profile: "documents",
    application_version: "2.3.4",
    source_revision: revision,
    source: { available: true, sha256: digest, download_path: sourcePath },
    companions: {
      available: true,
      coverage: "selected",
      application_version: "2.3.4",
      source_revision: revision,
      application_source_sha256: digest,
      downloads: [
        { kind: "dependency_sources", filename: "sources.zip", byte_length: 123, sha256: "c".repeat(64), download_url: "https://downloads.example.test/sources.zip", coverage: "selected" },
        { kind: "dependency_notices", filename: "notices.zip", byte_length: 456, sha256: "d".repeat(64), download_url: "https://downloads.example.test/notices.zip", coverage: "selected" },
      ],
    },
  };
}

function apiFor(request: ReturnType<typeof vi.fn>): FrontendApiClient {
  return { request } as unknown as FrontendApiClient;
}

describe("Application source access", () => {
  it("offers the exact bundled source download in Documents", async () => {
    const request = vi.fn().mockResolvedValue({
      application_profile: "documents",
      source: { available: true, sha256: digest, download_path: sourcePath },
    });
    render(ApplicationSourceLink, { props: { api: apiFor(request), documents: true } });
    const link = await screen.findByRole("link", { name: "Source code" });
    expect(link.getAttribute("href")).toBe(sourcePath);
    expect(link.hasAttribute("download")).toBe(true);
    expect(request).toHaveBeenCalledWith("/api/application-distribution", {
      signal: expect.any(AbortSignal),
    });
  });

  it("does not probe bundled source in Workbench", () => {
    const request = vi.fn();
    render(ApplicationSourceLink, { props: { api: apiFor(request), documents: false } });
    expect(request).not.toHaveBeenCalled();
    expect(screen.queryByRole("link", { name: "Source code" })).toBeNull();
  });

  it("offers dependency sources and notices bound to this application source", async () => {
    const request = vi.fn().mockResolvedValue(distributionWithCompanions());
    render(ApplicationSourceLink, { props: { api: apiFor(request), documents: true } });
    const sources = await screen.findByRole("link", { name: "Dependency sources" });
    const notices = screen.getByRole("link", { name: "Third-party notices" });
    expect(sources.getAttribute("href")).toBe("https://downloads.example.test/sources.zip");
    expect(notices.getAttribute("href")).toBe("https://downloads.example.test/notices.zip");
    expect(sources.getAttribute("rel")).toBe("noopener noreferrer");
    expect(screen.getByRole("link", { name: "Source code" }).getAttribute("href")).toBe(sourcePath);
  });

  it.each([
    { application_version: "2.3.5" },
    { source_revision: "e".repeat(40) },
    { application_source_sha256: "f".repeat(64) },
    { coverage: "complete" },
    { available: false },
  ])("keeps the application source when companion metadata is unbound", async (change) => {
    const info = distributionWithCompanions();
    const request = vi.fn().mockResolvedValue({ ...info, companions: { ...info.companions, ...change } });
    render(ApplicationSourceLink, { props: { api: apiFor(request), documents: true } });
    await screen.findByRole("link", { name: "Source code" });
    expect(screen.queryByRole("link", { name: "Dependency sources" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Third-party notices" })).toBeNull();
  });

  it.each([
    "javascript:alert('sources.zip')",
    "http://downloads.example.test/sources.zip",
    "https://user:password@downloads.example.test/sources.zip",
    "https://downloads.example.test/sources.zip?token=private",
    "https://downloads.example.test/sources.zip#fragment",
    "https://downloads.example.test/wrong.zip",
    "https://downloads.example.test/%00/sources.zip",
    "https://downloads.example.test/encoded%2f..%2fsources.zip",
  ])("rejects unsafe or unbound companion download URLs", async (url) => {
    const info = distributionWithCompanions();
    info.companions.downloads[0].download_url = url;
    const request = vi.fn().mockResolvedValue(info);
    render(ApplicationSourceLink, { props: { api: apiFor(request), documents: true } });
    await screen.findByRole("link", { name: "Source code" });
    expect(screen.queryByRole("link", { name: "Dependency sources" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Third-party notices" })).toBeNull();
  });

  it("supports numeric loopback download links in a local release preview", async () => {
    const info = distributionWithCompanions();
    info.companions.downloads[0].download_url = "http://127.0.0.1:8080/sources.zip";
    const request = vi.fn().mockResolvedValue(info);
    render(ApplicationSourceLink, { props: { api: apiFor(request), documents: true } });
    const link = await screen.findByRole("link", { name: "Dependency sources" });
    expect(link.getAttribute("href")).toBe("http://127.0.0.1:8080/sources.zip");
  });

  it.each([
    { source: { available: false } },
    { source: { available: true, sha256: digest, download_path: "https://example.test/source" } },
    { source: { available: true, sha256: "invalid", download_path: sourcePath } },
  ])("does not offer unavailable or unbound source", async (result) => {
    const request = vi.fn().mockResolvedValue({ application_profile: "documents", ...result });
    render(ApplicationSourceLink, { props: { api: apiFor(request), documents: true } });
    await waitFor(() => expect(request).toHaveBeenCalledOnce());
    expect(screen.queryByRole("link", { name: "Source code" })).toBeNull();
  });

  it("aborts its metadata request when the shell is removed", () => {
    const request = vi.fn().mockReturnValue(new Promise(() => {}));
    const { unmount } = render(ApplicationSourceLink, { props: { api: apiFor(request), documents: true } });
    const signal = request.mock.calls[0]?.[1]?.signal as AbortSignal;
    expect(signal.aborted).toBe(false);
    unmount();
    expect(signal.aborted).toBe(true);
  });
});
