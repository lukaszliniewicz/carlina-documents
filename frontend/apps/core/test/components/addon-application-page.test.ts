import { render, waitFor } from "@testing-library/svelte";
import { describe, expect, it, vi } from "vitest";

import type { FrontendApiClient, FrontendBootstrap } from "@carlina/sdk";

const lifecycle = vi.hoisted(() => ({
  mount: vi.fn(),
  unmount: vi.fn(),
  updateCurrentPath: vi.fn(),
}));

vi.mock("@carlina/sdk", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@carlina/sdk")>();
  return {
    ...actual,
    addonApplicationMountKey: (match: { addon: { id: string } } | undefined) =>
      match?.addon.id ?? "",
    findAddonApplication: (_bootstrap: FrontendBootstrap, route: string) => {
      const id = route.startsWith("/addons/other") ? "other" : "compass";
      return {
        addon: { id, name: id === "compass" ? "Compass" : "Other" },
        basePath: `/addons/${id}`,
      };
    },
    mountAddonApplication: (...args: unknown[]) => {
      lifecycle.mount(...args);
      return Promise.resolve({
        unmount: lifecycle.unmount,
        updateCurrentPath: lifecycle.updateCurrentPath,
      });
    },
  };
});

import AddonApplicationPage from "../../src/routes/AddonApplicationPage.svelte";

describe("addon application route lifecycle", () => {
  it("retains one mount across nested routes and forwards path updates", async () => {
    const props = {
      api: {} as FrontendApiClient,
      bootstrap: {} as FrontendBootstrap,
      route: "/addons/compass/",
      navigate: vi.fn(() => "client" as const),
    };
    const page = render(AddonApplicationPage, { props });

    await waitFor(() => {
      expect(lifecycle.mount).toHaveBeenCalledTimes(1);
      expect(lifecycle.updateCurrentPath).toHaveBeenLastCalledWith(
        "/addons/compass/",
      );
    });

    await page.rerender({ ...props, route: "/addons/compass/tasks" });
    await waitFor(() => {
      expect(lifecycle.updateCurrentPath).toHaveBeenLastCalledWith(
        "/addons/compass/tasks",
      );
    });
    expect(lifecycle.mount).toHaveBeenCalledTimes(1);
    expect(lifecycle.unmount).not.toHaveBeenCalled();

    await page.rerender({ ...props, route: "/addons/other/" });
    await waitFor(() => {
      expect(lifecycle.mount).toHaveBeenCalledTimes(2);
      expect(lifecycle.unmount).toHaveBeenCalledTimes(1);
    });
  });
});
