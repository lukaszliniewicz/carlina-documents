import { resolveSameOriginModuleUrl } from "./loader.ts";

export type FrontendStylesheetLoader = (
  urls: readonly string[],
  applicationOrigin: string,
  document: Document,
) => Promise<void>;

const stylesheetLoads = new WeakMap<Document, Map<string, Promise<void>>>();

export const loadFrontendStylesheets: FrontendStylesheetLoader = async (
  urls,
  applicationOrigin,
  document,
) => {
  let documentLoads = stylesheetLoads.get(document);
  if (!documentLoads) {
    documentLoads = new Map();
    stylesheetLoads.set(document, documentLoads);
  }

  await Promise.all(
    urls.map((rawUrl) => {
      const url = resolveSameOriginModuleUrl(rawUrl, applicationOrigin);
      const existingLoad = documentLoads?.get(url);
      if (existingLoad) {
        return existingLoad;
      }

      const existingLink = Array.from(
        document.querySelectorAll<HTMLLinkElement>('link[rel="stylesheet"]'),
      ).find((link) => link.href === url);
      if (existingLink) {
        const loaded = Promise.resolve();
        documentLoads?.set(url, loaded);
        return loaded;
      }

      const load = new Promise<void>((resolve, reject) => {
        const link = document.createElement("link");
        link.rel = "stylesheet";
        link.href = url;
        link.dataset.iarfFrontendStyle = "true";
        link.addEventListener("load", () => resolve(), { once: true });
        link.addEventListener(
          "error",
          () =>
            reject(
              new Error(`Unable to load frontend stylesheet '${rawUrl}'.`),
            ),
          { once: true },
        );
        document.head.append(link);
      });
      documentLoads?.set(url, load);
      return load;
    }),
  );
};
