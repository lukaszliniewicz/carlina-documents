import { fireEvent, render } from "@testing-library/svelte";
import { describe, expect, it, vi } from "vitest";
import ProjectCard from "./ProjectCard.svelte";

describe("ProjectCard", () => {
  it("keeps active projects openable", async () => {
    const onOpen = vi.fn();
    const { getByRole } = render(ProjectCard, {
      props: {
        project: { id: 1, name: "Active", status: "translating", source_lang: "EN", target_lang: "PL" },
        onOpen
      }
    });
    const button = getByRole("button", { name: /processing.*open/i });
    expect(button.disabled).toBe(false);
    await fireEvent.click(button);
    expect(onOpen).toHaveBeenCalledOnce();
  });

  it("renders operation progress as a percentage without double scaling", () => {
    const { getByText, queryByText } = render(ProjectCard, {
      props: {
        project: {
          id: 2,
          name: "Working",
          status: "translating",
          source_lang: "EN",
          target_lang: "PL",
          latest_operation: { state: "running", progress: 50 }
        }
      }
    });
    expect(getByText(/Operation: running \(50%\)/)).toBeTruthy();
    expect(queryByText(/500%/)).toBeNull();
  });
});
