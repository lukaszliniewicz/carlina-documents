import { describe, expect, it } from "vitest";
import { mergeSegmentPages, pageHasMore, readSegmentPage, resetSegmentPageState } from "./workspace-page.js";

describe("bounded translator workspace pages", () => {
  it("reads the route pagination shape without requiring a new envelope", () => {
    const page = readSegmentPage({ segments: [{ id: 1 }], pagination: { offset: 0, limit: 80, total: 100, has_more: true } });
    expect(page).toMatchObject({ offset: 0, limit: 80, total: 100, hasMore: true, paginated: true });
  });

  it("appends later pages by id", () => {
    expect(mergeSegmentPages([{ id: 1 }, { id: 2 }], [{ id: 3 }, { id: 2, version: 2 }], 2)).toEqual([
      { id: 1 }, { id: 2, version: 2 }, { id: 3 }
    ]);
  });

  it("resets structural-edit page state to the first bounded page", () => {
    expect(resetSegmentPageState()).toEqual({ offset: 0, hasMore: false, total: 0, visibleLimit: 80, segments: [] });
  });

  it("does not re-enable pagination after refresh loaded the complete total", () => {
    expect(pageHasMore({ paginated: true, hasMore: true, total: 160 }, 160)).toBe(false);
    expect(pageHasMore({ paginated: true, hasMore: true, total: 200 }, 160)).toBe(true);
  });
});
