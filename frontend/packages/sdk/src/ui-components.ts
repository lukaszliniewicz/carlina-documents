import { createAddonApiClient } from "./api.ts";
import {
  FRONTEND_API_VERSION,
  type FrontendApiClient,
  type FrontendBootstrap,
  type FrontendFeatureMap,
  type FrontendJsonSchema,
  type FrontendUiComponentDescriptor,
  type FrontendUiComponentService,
  type MountedFrontend,
  type UiComponentMountContext,
  type UiComponentModule,
} from "./contracts.ts";
import {
  loadVersionedFrontendModule,
  type ModuleImporter,
} from "./loader.ts";
import {
  loadFrontendStylesheets,
  type FrontendStylesheetLoader,
} from "./stylesheets.ts";

export type UiComponentStylesheetLoader = FrontendStylesheetLoader;

export interface UiComponentOpenOptions {
  api?: FrontendApiClient;
  applicationOrigin?: string;
  document?: Document;
  featureFlags?: FrontendFeatureMap;
  fetchImplementation?: typeof fetch;
  importModule?: ModuleImporter;
  loadStylesheets?: UiComponentStylesheetLoader;
  overlayHost?: Element;
  signal?: AbortSignal;
}

export type UiComponentService = FrontendUiComponentService;

function schemaTypes(schema: FrontendJsonSchema): string[] {
  const declared = schema.type;
  if (typeof declared === "string") {
    return [declared];
  }
  if (Array.isArray(declared)) {
    return declared.filter((value): value is string => typeof value === "string");
  }
  return [];
}

function valueMatchesType(value: unknown, type: string): boolean {
  switch (type) {
    case "array":
      return Array.isArray(value);
    case "boolean":
      return typeof value === "boolean";
    case "integer":
      return typeof value === "number" && Number.isInteger(value);
    case "null":
      return value === null;
    case "number":
      return typeof value === "number" && Number.isFinite(value);
    case "object":
      return typeof value === "object" && value !== null && !Array.isArray(value);
    case "string":
      return typeof value === "string";
    default:
      return true;
  }
}

function equalJsonValue(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) {
    return true;
  }
  try {
    return JSON.stringify(left) === JSON.stringify(right);
  } catch {
    return false;
  }
}

function validateSchemaNode(
  value: unknown,
  schema: FrontendJsonSchema,
  path: string,
  errors: string[],
): void {
  const types = schemaTypes(schema);
  if (types.length && !types.some((type) => valueMatchesType(value, type))) {
    errors.push(`${path} must be ${types.join(" or ")}.`);
    return;
  }

  if ("const" in schema && !equalJsonValue(value, schema.const)) {
    errors.push(`${path} must equal the declared constant.`);
  }
  if (
    Array.isArray(schema.enum) &&
    !schema.enum.some((candidate) => equalJsonValue(value, candidate))
  ) {
    errors.push(`${path} must be one of the declared values.`);
  }

  if (typeof value === "string") {
    if (
      typeof schema.minLength === "number" &&
      value.length < schema.minLength
    ) {
      errors.push(`${path} is shorter than ${schema.minLength}.`);
    }
    if (
      typeof schema.maxLength === "number" &&
      value.length > schema.maxLength
    ) {
      errors.push(`${path} is longer than ${schema.maxLength}.`);
    }
  }

  if (Array.isArray(value)) {
    if (typeof schema.minItems === "number" && value.length < schema.minItems) {
      errors.push(`${path} has fewer than ${schema.minItems} items.`);
    }
    if (typeof schema.maxItems === "number" && value.length > schema.maxItems) {
      errors.push(`${path} has more than ${schema.maxItems} items.`);
    }
    if (
      schema.items &&
      typeof schema.items === "object" &&
      !Array.isArray(schema.items)
    ) {
      value.forEach((item, index) => {
        validateSchemaNode(
          item,
          schema.items as FrontendJsonSchema,
          `${path}[${index}]`,
          errors,
        );
      });
    }
  }

  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return;
  }

  const record = value as Record<string, unknown>;
  const properties =
    schema.properties &&
    typeof schema.properties === "object" &&
    !Array.isArray(schema.properties)
      ? (schema.properties as Record<string, unknown>)
      : {};
  const required = Array.isArray(schema.required)
    ? schema.required.filter(
        (property): property is string => typeof property === "string",
      )
    : [];

  for (const property of required) {
    if (!(property in record)) {
      errors.push(`${path}.${property} is required.`);
    }
  }
  for (const [property, propertySchema] of Object.entries(properties)) {
    if (
      property in record &&
      propertySchema &&
      typeof propertySchema === "object" &&
      !Array.isArray(propertySchema)
    ) {
      validateSchemaNode(
        record[property],
        propertySchema as FrontendJsonSchema,
        `${path}.${property}`,
        errors,
      );
    }
  }
  if (schema.additionalProperties === false) {
    for (const property of Object.keys(record)) {
      if (!(property in properties)) {
        errors.push(`${path}.${property} is not allowed.`);
      }
    }
  }
}

export function validateJsonSchema(
  value: unknown,
  schema: FrontendJsonSchema | null | undefined,
): string[] {
  if (!schema) {
    return [];
  }
  const errors: string[] = [];
  validateSchemaNode(value, schema, "$", errors);
  return errors;
}

export function assertJsonSchema(
  value: unknown,
  schema: FrontendJsonSchema | null | undefined,
  label = "Value",
): void {
  const errors = validateJsonSchema(value, schema);
  if (errors.length) {
    throw new TypeError(`${label} does not match its schema: ${errors.join(" ")}`);
  }
}

export const loadUiComponentStylesheets: UiComponentStylesheetLoader =
  loadFrontendStylesheets;

function componentOrigin(
  document: Document,
  configuredOrigin: string | undefined,
): string {
  if (configuredOrigin) {
    return configuredOrigin;
  }
  if (document.location?.origin) {
    return document.location.origin;
  }
  throw new TypeError("A UI component requires an application origin.");
}

function restoreFocus(target: Element | null): void {
  const focus = (target as { focus?: (options?: FocusOptions) => void } | null)
    ?.focus;
  if (typeof focus !== "function") {
    return;
  }
  try {
    focus.call(target, { preventScroll: true });
  } catch {
    focus.call(target);
  }
}

function showDialog(dialog: HTMLDialogElement): void {
  if (typeof dialog.showModal === "function") {
    dialog.showModal();
  } else {
    dialog.setAttribute("open", "");
  }
}

function closeDialog(dialog: HTMLDialogElement): void {
  if (dialog.open && typeof dialog.close === "function") {
    dialog.close();
  } else {
    dialog.removeAttribute("open");
  }
}

function descriptorModule(
  descriptor: FrontendUiComponentDescriptor,
): {
  id: string;
  module_url: string;
  frontend_api_range: string;
} {
  if (!descriptor.compatible) {
    throw new TypeError(
      `UI component '${descriptor.id}' targets an incompatible frontend API.`,
    );
  }
  if (!descriptor.module_url || !descriptor.frontend_api_range) {
    throw new TypeError(
      `UI component '${descriptor.id}' has no versioned module entrypoint.`,
    );
  }
  if (!/^[A-Za-z0-9_.-]+$/.test(descriptor.addon_id)) {
    throw new TypeError(
      `UI component '${descriptor.id}' has an invalid addon identifier.`,
    );
  }
  return {
    id: descriptor.id,
    module_url: descriptor.module_url,
    frontend_api_range: descriptor.frontend_api_range,
  };
}

export async function openUiComponent<Input = unknown, Result = unknown>(
  descriptor: FrontendUiComponentDescriptor,
  input?: Input,
  options: UiComponentOpenOptions = {},
): Promise<Result | undefined> {
  const document = options.document ?? globalThis.document;
  if (!document?.body) {
    throw new TypeError("A UI component requires a browser document.");
  }
  const componentInput = input === undefined ? {} : input;
  assertJsonSchema(
    componentInput,
    descriptor.input_schema,
    `UI component '${descriptor.id}' input`,
  );

  const applicationOrigin = componentOrigin(
    document,
    options.applicationOrigin,
  );
  const moduleDescriptor = descriptorModule(descriptor);
  const stylesheetLoader =
    options.loadStylesheets ?? loadUiComponentStylesheets;
  const [componentModule] = await Promise.all([
    loadVersionedFrontendModule<UiComponentMountContext>(
      moduleDescriptor,
      applicationOrigin,
      options.importModule,
    ) as Promise<UiComponentModule>,
    stylesheetLoader(
      descriptor.css_urls,
      applicationOrigin,
      document,
    ),
  ]);

  const previousFocus = document.activeElement;
  const dialog = document.createElement("dialog");
  dialog.dataset.iarfUiComponentHost = "true";
  dialog.dataset.addonId = descriptor.addon_id;
  dialog.dataset.componentId = descriptor.id;
  dialog.setAttribute("aria-label", descriptor.name || descriptor.id);

  const target = document.createElement("div");
  target.dataset.iarfUiComponentTarget = "true";
  target.dataset.addonId = descriptor.addon_id;
  dialog.append(target);

  const host =
    options.overlayHost ??
    document.querySelector("#overlay-host") ??
    document.querySelector("#ui-component-modals-container") ??
    document.body;
  host.append(dialog);

  const controller = new AbortController();
  const contextApi =
    options.api ??
    createAddonApiClient(
      applicationOrigin,
      descriptor.addon_id,
      options.fetchImplementation,
    );

  return new Promise<Result | undefined>((resolve, reject) => {
    let settling = false;
    let mountPromise: Promise<MountedFrontend>;

    const settle = async (
      result: Result | undefined,
      failure?: unknown,
    ): Promise<void> => {
      if (settling) {
        return;
      }
      settling = true;

      let error = failure;
      if (error === undefined && result !== undefined) {
        try {
          assertJsonSchema(
            result,
            descriptor.output_schema,
            `UI component '${descriptor.id}' result`,
          );
        } catch (schemaError) {
          error = schemaError;
        }
      }

      controller.abort();
      options.signal?.removeEventListener("abort", abort);

      let mounted: MountedFrontend | undefined;
      try {
        mounted = await mountPromise;
      } catch (mountError) {
        error ??= mountError;
      }
      if (mounted) {
        try {
          await mounted.unmount();
        } catch (unmountError) {
          error ??= unmountError;
        }
      }

      closeDialog(dialog);
      dialog.remove();
      restoreFocus(previousFocus);

      if (error !== undefined) {
        reject(error);
      } else {
        resolve(result);
      }
    };

    const close = (result?: unknown): void => {
      void settle(result as Result | undefined);
    };
    const abort = (): void => close();
    const cancel = (event: Event): void => {
      event.preventDefault();
      close();
    };
    const dismissBackdrop = (event: Event): void => {
      if (event.target === dialog) {
        close();
      }
    };

    dialog.addEventListener("cancel", cancel);
    dialog.addEventListener("click", dismissBackdrop);
    options.signal?.addEventListener("abort", abort, { once: true });

    const context: UiComponentMountContext = {
      addonId: descriptor.addon_id,
      api: contextApi,
      apiVersion: FRONTEND_API_VERSION,
      basePath: `/addons/${descriptor.addon_id}`,
      close,
      componentId: descriptor.id,
      featureFlags: options.featureFlags ?? {},
      input: componentInput,
      signal: controller.signal,
    };

    mountPromise = Promise.resolve()
      .then(() => componentModule.mount(target, context))
      .then((mounted) => {
        if (!mounted || typeof mounted.unmount !== "function") {
          throw new TypeError(
            `Frontend module '${descriptor.id}' did not return an unmount handle.`,
          );
        }
        return mounted;
      });

    mountPromise.then(
      () => {
        if (settling) {
          return;
        }
        showDialog(dialog);
        queueMicrotask(() => {
          const focusTarget = target.querySelector<HTMLElement>(
            "[autofocus], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])",
          );
          focusTarget?.focus({ preventScroll: true });
        });
      },
      (error) => {
        void settle(undefined, error);
      },
    );

    if (options.signal?.aborted) {
      abort();
    }
  });
}

export function findUiComponentDescriptor(
  bootstrap: FrontendBootstrap,
  componentId: string,
): FrontendUiComponentDescriptor | undefined {
  return bootstrap.ui_components.find(
    (component) => component.id === componentId,
  );
}

export function createUiComponentService(
  bootstrap: FrontendBootstrap,
  applicationOrigin: string,
  defaults: Omit<
    UiComponentOpenOptions,
    "applicationOrigin" | "featureFlags"
  > = {},
): UiComponentService {
  return {
    async open<Input = unknown, Result = unknown>(
      componentId: string,
      input?: Input,
      options = {},
    ): Promise<Result | undefined> {
      const descriptor = findUiComponentDescriptor(bootstrap, componentId);
      if (!descriptor) {
        throw new Error(`UI component '${componentId}' is not available.`);
      }
      return openUiComponent<Input, Result>(descriptor, input, {
        ...defaults,
        ...options,
        applicationOrigin,
        featureFlags: bootstrap.feature_flags,
      });
    },
  };
}
