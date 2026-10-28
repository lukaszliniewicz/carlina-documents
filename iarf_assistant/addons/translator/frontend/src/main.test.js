import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const svelte = vi.hoisted(() => ({
  mount: vi.fn((component, options) => ({ component, options })),
  unmount: vi.fn(() => Promise.resolve())
}));

vi.mock("svelte", () => svelte);
vi.mock("./projects/ProjectsApp.svelte", () => ({ default: "ProjectsApp" }));
vi.mock("./workspace/WorkspaceGate.svelte", () => ({ default: "WorkspaceGate" }));

import { mount, workspaceProjectId } from "./main.js";

describe("Translator application routing", () => {
  beforeEach(() => {
    svelte.mount.mockClear();
    svelte.unmount.mockClear();
    window.history.replaceState({}, "", "/addons/translator/");
  });

  afterEach(() => {
    document.body.replaceChildren();
  });

  it("extracts a workspace project id only from the workspace route", () => {
    expect(workspaceProjectId("/addons/translator/workspace/project%201")).toBe("project 1");
    expect(workspaceProjectId("/addons/translator/")).toBeNull();
  });

  it("remounts the workspace when the shell changes the current path", async () => {
    const target = document.createElement("div");
    document.body.append(target);
    const application = mount(target, {
      addonId: "translator",
      currentPath: "/addons/translator/"
    });

    expect(svelte.mount).toHaveBeenCalledWith("ProjectsApp", expect.anything());

    await application.updateCurrentPath("/addons/translator/workspace/project-1");

    expect(svelte.unmount).toHaveBeenCalledTimes(1);
    expect(svelte.mount).toHaveBeenLastCalledWith(
      "WorkspaceGate",
      expect.objectContaining({
        props: expect.objectContaining({
          bootstrapData: expect.objectContaining({ projectId: "project-1" })
        })
      })
    );
  });

  it("does not remount when only the same workspace path is reported again", async () => {
    const target = document.createElement("div");
    document.body.append(target);
    const application = mount(target, {
      addonId: "translator",
      currentPath: "/addons/translator/workspace/project-1"
    });

    await application.updateCurrentPath("/addons/translator/workspace/project-1");

    expect(svelte.unmount).not.toHaveBeenCalled();
    expect(svelte.mount).toHaveBeenCalledTimes(1);
  });

  it("coalesces concurrent path changes and mounts only the newest workspace", async () => {
    let releaseUnmount;
    svelte.unmount.mockImplementationOnce(
      () => new Promise((resolve) => {
        releaseUnmount = resolve;
      })
    );
    const target = document.createElement("div");
    document.body.append(target);
    const application = mount(target, {
      addonId: "translator",
      currentPath: "/addons/translator/"
    });

    const first = application.updateCurrentPath(
      "/addons/translator/workspace/project-a"
    );
    const second = application.updateCurrentPath(
      "/addons/translator/workspace/project-b"
    );
    expect(svelte.unmount).toHaveBeenCalledTimes(1);

    releaseUnmount();
    await Promise.all([first, second]);

    expect(svelte.mount).toHaveBeenCalledTimes(2);
    expect(svelte.mount).toHaveBeenLastCalledWith(
      "WorkspaceGate",
      expect.objectContaining({
        props: expect.objectContaining({
          bootstrapData: expect.objectContaining({ projectId: "project-b" })
        })
      })
    );
  });

  it("invalidates an in-flight path change before application teardown", async () => {
    let releaseUnmount;
    svelte.unmount.mockImplementationOnce(
      () => new Promise((resolve) => {
        releaseUnmount = resolve;
      })
    );
    const target = document.createElement("div");
    document.body.append(target);
    const application = mount(target, {
      addonId: "translator",
      currentPath: "/addons/translator/"
    });

    const transition = application.updateCurrentPath(
      "/addons/translator/workspace/project-a"
    );
    const teardown = application.unmount();
    releaseUnmount();
    await Promise.all([transition, teardown]);

    expect(svelte.mount).toHaveBeenCalledTimes(1);
    expect(target.hasAttribute("data-addon-id")).toBe(false);
    expect(target.childNodes).toHaveLength(0);
  });
});
