import { fireEvent, render, screen } from "@testing-library/svelte";
import { describe, expect, it, vi } from "vitest";

import type { FrontendAddonManagerSummary } from "@carlina/sdk";

import AddonDirectoryList from "../../src/routes/settings/addon-manager/AddonDirectoryList.svelte";
import AddonManagerOverview from "../../src/routes/settings/addon-manager/AddonManagerOverview.svelte";

function addon(index: number): FrontendAddonManagerSummary {
  const id = `addon_${String(index).padStart(2, "0")}`;
  return {
    detail_level: "summary",
    detail_url: `/api/addons/${id}/detail`,
    id,
    name: `Addon ${index}`,
    version: "1.0.0",
    author: "Carlina Project",
    description: "Component test package",
    roles: ["workspace"],
    categories: ["test"],
    provider_domains: [],
    addon_type: ["application"],
    dependencies: [],
    connection_count: 0,
    consumer_binding_count: 0,
    provider_binding_count: 0,
    search_terms: [],
    is_active: index % 2 === 0,
    status: { state: index % 2 === 0 ? "active" : "inactive", reasons: [] },
    frontend_tier: "compiled_workspace",
    frontend_status: "built",
    frontend_rebuild_needed: false,
    worker_running: 0,
    has_settings: false,
    has_prompts: false,
    delegatable: false,
    inferred_hub: {
      relation: "standalone",
      hub_id: null,
      hub_label: null,
      spoke_count: 0,
    },
  };
}

describe("addon directory presentation", () => {
  it("renders only the progressive slice and delegates selection, clearing, and expansion", async () => {
    const addons = Array.from({ length: 30 }, (_, index) => addon(index + 1));
    const onselect = vi.fn();
    const onclear = vi.fn();
    const onshowmore = vi.fn();
    const { container } = render(AddonDirectoryList, {
      props: {
        addons,
        totalCount: addons.length,
        visibleAddons: addons.slice(0, 25),
        selectedId: "addon_03",
        hasFilters: true,
        batchSize: 25,
        onselect,
        onclear,
        onshowmore,
      },
    });

    expect(container.querySelectorAll(".addon-row")).toHaveLength(25);
    expect(screen.getByRole("heading", { name: "30 of 30" })).toBeTruthy();
    expect(screen.queryByText("Addon 30")).toBeNull();
    const selected = screen.getByText("Addon 3").closest("button");
    expect(selected?.getAttribute("aria-pressed")).toBe("true");

    await fireEvent.click(screen.getByText("Addon 4").closest("button")!);
    expect(onselect).toHaveBeenCalledWith("addon_04");
    await fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(onclear).toHaveBeenCalledOnce();
    await fireEvent.click(screen.getByRole("button", { name: /Show 5 more/ }));
    expect(onshowmore).toHaveBeenCalledOnce();
  });

  it("keeps summary filters keyboard-readable through pressed state", async () => {
    render(AddonManagerOverview, {
      props: {
        summary: {
          total: 30,
          active: 18,
          inactive: 12,
          attention: 2,
          runningWorkers: 4,
        },
        statusFilter: "all",
        kindFilter: "all",
        doctor: null,
        doctorLoading: false,
        refreshing: false,
        restartRequired: false,
        isAdmin: false,
        ondoctor: vi.fn(),
        onrefresh: vi.fn(),
        oncatalog: vi.fn(),
        oninstall: vi.fn(),
        onrestart: vi.fn(),
      },
    });

    const attention = screen.getByRole("button", { name: /Needs attention/ });
    expect(attention.getAttribute("aria-pressed")).toBe("false");
    await fireEvent.click(attention);
    expect(attention.getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "Applications" })).toBeTruthy();
  });
});
