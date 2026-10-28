export const DEFAULT_SEGMENT_PAGE_SIZE = 80;

export function resetSegmentPageState() {
  return {
    offset: 0,
    hasMore: false,
    total: 0,
    visibleLimit: DEFAULT_SEGMENT_PAGE_SIZE,
    segments: []
  };
}

/** Accept both the explicit segment_page shape and the route's pagination key. */
export function readSegmentPage(payload) {
  const page = payload?.segment_page || payload?.pagination || null;
  const rows = Array.isArray(payload?.segments)
    ? payload.segments
    : (Array.isArray(page?.items) ? page.items : (Array.isArray(page?.segments) ? page.segments : []));
  const offset = Number.isFinite(Number(page?.offset)) ? Number(page.offset) : 0;
  const limit = Number.isFinite(Number(page?.limit)) ? Number(page.limit) : rows.length;
  const total = Number.isFinite(Number(page?.total)) ? Number(page.total) : rows.length;
  const paginated = Boolean(page && ("offset" in page || "limit" in page || "has_more" in page));
  return {
    segments: rows,
    offset,
    limit,
    total,
    hasMore: paginated ? Boolean(page.has_more ?? offset + rows.length < total) : false,
    paginated
  };
}

export function mergeSegmentPages(existing, incoming, offset = 0) {
  if (!offset) return [...incoming];
  const byId = new Map((existing || []).map((segment) => [String(segment.id), segment]));
  for (const segment of incoming || []) byId.set(String(segment.id), segment);
  return Array.from(byId.values());
}

export function pageHasMore(page, loadedCount) {
  if (!page?.hasMore) return false;
  if (!page.paginated) return true;
  return Number(loadedCount) < Number(page.total);
}
