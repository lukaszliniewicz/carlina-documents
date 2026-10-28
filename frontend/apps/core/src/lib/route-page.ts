import type { AddonApplicationMatch } from "@carlina/sdk";

export function routePageKey(
  pathname: string,
  addonMatch: AddonApplicationMatch | undefined,
  routeKeys: readonly string[],
): string {
  if (routeKeys.includes(pathname)) return pathname;
  for (const family of ["/runs", "/settings"]) {
    if (routeKeys.includes(family) && pathname.startsWith(`${family}/`)) {
      return family;
    }
  }
  if (addonMatch) {
    return `addon:${addonMatch.addon.id}:${addonMatch.basePath}`;
  }
  return "";
}
