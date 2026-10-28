import { fireEvent, render, screen } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AddonMountContext } from "@carlina/sdk";
import { DynamicWorkbench } from "@carlina/workspace-ui";

const context = {
  addonId: "scraping",
  currentPath: "/debug",
  navigate: vi.fn(),
} as unknown as AddonMountContext;

afterEach(() => vi.unstubAllGlobals());

describe("dynamic workbench availability controls", () => {
  it("disables unavailable providers and hides local controls for Jina", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            actions: [
              {
                id: "content",
                label: "Get web content",
                fields: [
                  { id: "url", label: "URL", type: "url", required: true },
                  {
                    id: "provider",
                    label: "Provider",
                    type: "select",
                    default: "auto",
                    options: [
                      { value: "auto", label: "Automatic local routing" },
                      {
                        value: "patchright",
                        label: "Patchright",
                        disabled: true,
                        disabledReason: "Pixi is missing.",
                      },
                      { value: "jina", label: "Jina Reader" },
                    ],
                  },
                  {
                    id: "parsing_strategy",
                    label: "Parsing Strategy",
                    type: "select",
                    default: "custom",
                    options: ["custom"],
                    hiddenWhen: { provider: ["jina"] },
                  },
                ],
                notices: [
                  {
                    message: "Jina Reader fetches and converts content remotely.",
                    visibleWhen: { provider: ["jina"] },
                  },
                ],
              },
            ],
          }),
          { headers: { "content-type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ success: true }), {
          headers: { "content-type": "application/json" },
        }),
      );
    vi.stubGlobal(
      "fetch",
      fetchMock,
    );

    render(DynamicWorkbench, {
      props: {
        context,
        title: "Scraping Laboratory",
        description: "Provider controls",
        schemaEndpoint: "/api/debug/schema",
        runEndpoint: "/api/debug/run",
      },
    });

    const url = await screen.findByLabelText("URL");
    expect(url.closest("label")?.classList.contains("wide")).toBe(true);

    const provider = await screen.findByLabelText("Provider");
    const patchright = Array.from(
      (provider as HTMLSelectElement).options,
    ).find((option) => option.value === "patchright");
    expect(patchright?.disabled).toBe(true);
    expect(screen.getByText("Patchright: Pixi is missing.")).toBeTruthy();

    await fireEvent.change(provider, { target: { value: "jina" } });
    expect(screen.queryByText("Parsing Strategy")).toBeNull();
    expect(
      screen.getByText("Jina Reader fetches and converts content remotely."),
    ).toBeTruthy();

    await fireEvent.input(url, {
      target: { value: "https://example.com" },
    });
    await fireEvent.click(
      screen.getByRole("button", { name: "Run Get web content" }),
    );

    const requestBody = JSON.parse(
      String(fetchMock.mock.calls[1]?.[1]?.body),
    ) as { params: Record<string, unknown> };
    expect(requestBody.params).toEqual({
      provider: "jina",
      url: "https://example.com",
    });
  });
});
