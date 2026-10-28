export function classNames(
  ...values: Array<string | false | null | undefined>
): string {
  return values.filter((value): value is string => Boolean(value)).join(" ");
}

/** Fetch a prepared artifact without navigating away from the workspace. */
export async function prepareDownload(
  url: string,
  filename: string,
  options: { signal?: AbortSignal; fetcher?: typeof fetch } = {},
): Promise<{ url: string; filename: string; dispose: () => void }> {
  const target = new URL(url, window.location.origin);
  if (target.origin !== window.location.origin || target.username || target.password) {
    throw new Error("The download must belong to this Carlina instance.");
  }
  const response = await (options.fetcher || fetch)(target.href, {
    credentials: "same-origin", redirect: "error", signal: options.signal,
  });
  if (!response.ok) throw new Error(`Unable to retrieve the prepared document (${response.status}).`);
  const blob = await response.blob();
  if (!blob.size) throw new Error("The prepared document is empty.");
  options.signal?.throwIfAborted();
  const objectUrl = URL.createObjectURL(blob);
  return {
    url: objectUrl,
    filename: Array.from(filename, (character) => character.charCodeAt(0) < 32 || /[\\/:*?"<>|]/.test(character) ? "_" : character).join("").slice(0, 200),
    dispose: () => URL.revokeObjectURL(objectUrl),
  };
}
