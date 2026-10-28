import { fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { describe, expect, it, vi } from "vitest";

import type {
  FrontendApiClient,
  FrontendBootstrap,
  FrontendMemberDirectory,
} from "@carlina/sdk";

import LogsPage from "../../src/routes/LogsPage.svelte";
import WorkspacesPage from "../../src/routes/WorkspacesPage.svelte";
import MembersAdministrationPanel from "../../src/routes/settings/administration/MembersAdministrationPanel.svelte";

describe("responsive page accessibility", () => {
  it("expands the log workbench and restores scrolling when Escape exits", async () => {
    const source = {
      id: "core",
      name: "Core service",
      description: "Service activity",
      category: "application",
      size: 32,
      modified_at: "2026-08-11T08:00:00Z",
      exists: true,
      readable: true,
      downloadable: true,
      clearable: true,
    };
    const api = {
      request: vi.fn((url: string) => {
        if (url === "/api/log-sources") return Promise.resolve({ sources: [source] });
        return Promise.resolve({
          text: "first line\nsecond line",
          next_cursor: 0,
          source,
          warning: null,
        });
      }),
    } as unknown as FrontendApiClient;

    render(LogsPage, {
      props: {
        api,
        bootstrap: {} as FrontendBootstrap,
        route: "/logs",
        navigate: vi.fn(),
      },
    });

    const expand = await screen.findByRole("button", {
      name: "Expand log viewer",
    });
    await fireEvent.click(expand);
    expect(
      document
        .querySelector(".log-workbench")
        ?.classList.contains("log-workbench--expanded"),
    ).toBe(true);
    expect(document.body.style.overflow).toBe("hidden");

    await fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Expand log viewer" }),
      ).toBeTruthy();
      expect(document.body.style.overflow).toBe("");
    });
  });

  it("labels compact member actions by account identity", () => {
    const value = {
      members: [
        {
          id: 1,
          email: "member@example.test",
          is_admin: false,
          is_current: false,
          can_change_role: true,
          can_delete: true,
        },
      ],
      summary: { total: 1, administrators: 0, members: 1, truncated: false },
      limits: {
        members: 100,
        password_characters: { minimum: 12, maximum: 128 },
      },
      actions: { collection_url: "/api/members" },
    } as FrontendMemberDirectory;

    render(MembersAdministrationPanel, {
      props: {
        api: {} as FrontendApiClient,
        value,
        onrefresh: vi.fn().mockResolvedValue(undefined),
      },
    });

    expect(
      screen
        .getByRole("button", { name: "Edit member@example.test" })
        .getAttribute("title"),
    ).toBe("Edit member");
    expect(
      screen
        .getByRole("button", { name: "Remove member@example.test" })
        .getAttribute("title"),
    ).toBe("Remove member");
  });

  it("uses a status dot with accessible status text on workspace cards", () => {
    render(WorkspacesPage, {
      props: {
        api: {} as FrontendApiClient,
        bootstrap: {
          ui_contributions: [
            {
              id: "writing-room",
              addon_id: "writing_tools",
              title: "Writing room",
              description: "Draft work.",
              section: "Workspaces",
              status: "active",
              routes: ["/workspaces/writing"],
            },
          ],
        } as FrontendBootstrap,
        route: "/workspaces",
        navigate: vi.fn(),
      },
    });

    const status = screen.getByTitle("active");
    expect(status.querySelector(".workspace-status__dot--active")).not.toBeNull();
    expect(status.querySelector(".sr-only")?.textContent).toBe("active");
  });
});
