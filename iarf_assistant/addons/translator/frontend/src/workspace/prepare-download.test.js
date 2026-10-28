import { afterEach, describe, expect, it, vi } from "vitest";
import { prepareDownload } from "@carlina/ui";

afterEach(() => vi.unstubAllGlobals());

describe("prepared document downloads", () => {
  it("rejects foreign origins and credentials before fetching", async () => {
    const fetcher = vi.fn();
    await expect(prepareDownload("https://foreign.invalid/book.epub", "book.epub", { fetcher })).rejects.toThrow("Carlina instance");
    await expect(prepareDownload(`${window.location.protocol}//user:pass@${window.location.host}/book`, "book.epub", { fetcher })).rejects.toThrow("Carlina instance");
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("rejects errors, empty artifacts, and aborted retrievals", async () => {
    await expect(prepareDownload("/book", "book.epub", { fetcher: async () => new Response("failure", { status: 500 }) })).rejects.toThrow("500");
    await expect(prepareDownload("/book", "book.epub", { fetcher: async () => new Response("") })).rejects.toThrow("empty");
    const controller = new AbortController();
    controller.abort();
    await expect(prepareDownload("/book", "book.epub", { signal: controller.signal, fetcher: async () => new Response("book") })).rejects.toMatchObject({ name: "AbortError" });
  });

  it("keeps a disposable artifact without triggering navigation", async () => {
    const createObjectURL = vi.fn(() => "blob:prepared");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", Object.assign(class extends URL {}, { createObjectURL, revokeObjectURL }));
    const fetcher = vi.fn(async () => new Response("book"));
    const artifact = await prepareDownload("/book", "A/B.epub", { fetcher });
    expect(fetcher).toHaveBeenCalledWith(new URL("/book", window.location.origin).href, expect.objectContaining({ credentials: "same-origin", redirect: "error" }));
    expect(artifact.filename).toBe("A_B.epub");
    expect(artifact.url).toBe("blob:prepared");
    artifact.dispose();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:prepared");
  });
});
