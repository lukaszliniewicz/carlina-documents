import { render, screen } from "@testing-library/svelte";
import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  FrontendApiClient,
  FrontendProductIdentity,
} from "@carlina/sdk";

import Header from "../../src/components/Header.svelte";
import Sidebar from "../../src/components/Sidebar.svelte";
import SettingsNav from "../../src/routes/settings/SettingsNav.svelte";
import DocumentsHomePage from "../../src/routes/DocumentsHomePage.svelte";

const product = { name: "Carlina", short_name: "Carlina", mark: "C" } as FrontendProductIdentity;
const user = { id: "1", display_name: "Reader", is_admin: true };

afterEach(() => vi.unstubAllGlobals());

describe("Documents shell", () => {
  it("names its application and avoids requesting the absent chat directory", () => {
    const request = vi.fn();
    render(Sidebar, { props: {
      navigation: [], product, user,
      api: { request } as unknown as FrontendApiClient,
      chatEnabled: false,
      currentPath: "/addons/doc_processor/",
      collapsed: false, buildId: "synthetic",
      applicationLabel: "Documents", homeHref: "/addons/doc_processor/",
      onnavigate: vi.fn(), onnavigatehref: vi.fn(), ontoggle: vi.fn(),
    } });
    expect(screen.getByRole("link", { name: "Carlina Documents home" }).getAttribute("href"))
      .toBe("/addons/doc_processor/");
    expect(screen.queryByText("Recent")).toBeNull();
    expect(request).not.toHaveBeenCalled();
  });

  it("does not offer unavailable restart or placeholder notification actions", () => {
    render(Header, { props: {
      product, user, pickers: [], restartEnabled: false, notificationsEnabled: false,
      onpicker: vi.fn(), oncommand: vi.fn(), onnotifications: vi.fn(), onrestart: vi.fn(),
    } });
    expect(screen.queryByRole("button", { name: "Restart server" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Open notifications" })).toBeNull();
    expect(screen.getByRole("button", { name: /^Find anything/ })).toBeTruthy();
  });

  it("keeps supported model and tool settings accessible to an administrator", () => {
    vi.stubGlobal("matchMedia", () => ({ matches: false }));
    render(SettingsNav, { props: {
      active: "models", isAdmin: true, documents: true, navigate: vi.fn(),
    } });
    expect(screen.getAllByRole("link").map((link) => link.getAttribute("href")))
      .toEqual(["/settings", "/settings/models", "/settings/routing", "/settings/sharing", "/settings/addons"]);
    expect(screen.getByRole("link", { name: "02 Models" }).getAttribute("aria-current"))
      .toBe("page");
    expect(screen.queryByText("Policy")).toBeNull();
  });

  it("offers setup instead of dead product links on a fresh installation", () => {
    render(DocumentsHomePage, { props: {
      bootstrap: { addons: [], feature_flags: {} },
      navigate: vi.fn(),
    } });
    expect(screen.getByRole("link", { name: "Set up tools" }).getAttribute("href"))
      .toBe("/settings/addons");
    expect(screen.queryByRole("link", { name: "Open extraction" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Open translation" })).toBeNull();
    expect(screen.getByRole("link", { name: "Model connections" }).getAttribute("href"))
      .toBe("/settings/models");
  });
});
