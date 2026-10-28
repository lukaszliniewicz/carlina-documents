import { fireEvent, render, screen, waitFor } from "@testing-library/svelte";
import { describe, expect, it, vi } from "vitest";

import AddonActionDialog from "../../src/routes/settings/addon-manager/AddonActionDialog.svelte";
import AddonLocalInstallDialog from "../../src/routes/settings/addon-manager/AddonLocalInstallDialog.svelte";

describe("addon lifecycle dialogs", () => {
  it("keeps destructive confirmation disabled until the exact phrase is entered", async () => {
    const onconfirm = vi.fn();
    render(AddonActionDialog, {
      props: {
        action: {
          title: "Purge Email data?",
          description: "Create a safety backup, then remove retained data.",
          confirmLabel: "Create backup and purge data",
          danger: true,
          requiresIdentifier: true,
          confirmationPhrase: "PURGE email",
        },
        confirmation: "",
        pending: false,
        onclose: vi.fn(),
        onconfirm,
      },
    });

    const confirm = screen.getByRole("button", {
      name: "Create backup and purge data",
    }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    await fireEvent.input(screen.getByRole("textbox"), {
      target: { value: "email" },
    });
    expect(confirm.disabled).toBe(true);
    await fireEvent.input(screen.getByRole("textbox"), {
      target: { value: "PURGE email" },
    });
    expect(confirm.disabled).toBe(false);
    await fireEvent.click(confirm);
    expect(onconfirm).toHaveBeenCalledOnce();
  });

  it("requires both a ZIP and explicit native-code approval", async () => {
    const oninstall = vi.fn();
    render(AddonLocalInstallDialog, {
      props: {
        open: true,
        file: null,
        trustApproved: false,
        dependencyNotices: [],
        dependencyReviewApproved: false,
        pending: false,
        onclose: vi.fn(),
        oninstall,
      },
    });

    const install = screen.getByRole("button", {
      name: "Install and activate",
    }) as HTMLButtonElement;
    expect(install.disabled).toBe(true);
    const input = screen.getByLabelText("Addon ZIP") as HTMLInputElement;
    const file = new File(["archive"], "email.zip", {
      type: "application/zip",
    });
    await fireEvent.change(input, { target: { files: [file] } });
    expect(install.disabled).toBe(true);
    await fireEvent.click(
      screen.getByRole("checkbox", { name: /approve full host-code access/i }),
    );
    await waitFor(() => expect(install.disabled).toBe(false));
    await fireEvent.click(install);
    expect(oninstall).toHaveBeenCalledOnce();
  });
});
