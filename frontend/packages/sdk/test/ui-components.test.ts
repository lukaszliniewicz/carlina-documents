import assert from "node:assert/strict";
import test from "node:test";

import { createAddonApiClient } from "../src/api.ts";
import type {
  FrontendUiComponentDescriptor,
  UiComponentMountContext,
} from "../src/contracts.ts";
import {
  openUiComponent,
  validateJsonSchema,
} from "../src/ui-components.ts";

class FakeElement extends EventTarget {
  readonly children: FakeElement[] = [];
  readonly dataset: Record<string, string> = {};
  readonly attributes = new Map<string, string>();
  readonly ownerDocument: FakeDocument;
  readonly tagName: string;
  parent: FakeElement | null = null;
  open = false;
  focused = false;

  constructor(tagName: string, ownerDocument: FakeDocument) {
    super();
    this.tagName = tagName;
    this.ownerDocument = ownerDocument;
  }

  append(...children: FakeElement[]): void {
    for (const child of children) {
      child.parent = this;
      this.children.push(child);
    }
  }

  close(): void {
    this.open = false;
  }

  focus(): void {
    this.focused = true;
    this.ownerDocument.activeElement = this;
  }

  querySelector(): FakeElement | null {
    return null;
  }

  remove(): void {
    if (!this.parent) return;
    this.parent.children.splice(this.parent.children.indexOf(this), 1);
    this.parent = null;
  }

  removeAttribute(name: string): void {
    this.attributes.delete(name);
  }

  setAttribute(name: string, value: string): void {
    this.attributes.set(name, value);
  }

  showModal(): void {
    this.open = true;
  }
}

class FakeDocument {
  readonly location = { origin: "http://127.0.0.1:5003" };
  readonly head = new FakeElement("head", this);
  readonly body = new FakeElement("body", this);
  activeElement: FakeElement | null = null;

  createElement(tagName: string): FakeElement {
    return new FakeElement(tagName, this);
  }

  querySelector(): FakeElement | null {
    return null;
  }
}

function pickerDescriptor(): FrontendUiComponentDescriptor {
  return {
    id: "local_explorer.folder_picker",
    addon_id: "local_explorer",
    name: "Local folder picker",
    description: "Choose a folder",
    frontend_api_range: "^1.0",
    compatible: true,
    module_url: "/addons/local_explorer/static/dist/folder-picker.js",
    css_urls: [
      "/addons/local_explorer/static/dist/folder-picker.css",
    ],
    input_schema_id: "local_explorer.folder_picker.input.v1",
    output_schema_id: "local_explorer.folder_picker.output.v1",
    input_schema: {
      type: "object",
      properties: { allow_create: { type: "boolean" } },
      additionalProperties: false,
    },
    output_schema: {
      type: "object",
      properties: {
        path: { type: "string", minLength: 1 },
        resource_type: { type: "string", const: "local_path" },
      },
      required: ["path", "resource_type"],
      additionalProperties: false,
    },
    legacy: {
      css_urls: ["/addons/local_explorer/static/local_explorer.css"],
      js_urls: ["/addons/local_explorer/static/local_explorer.js"],
      global_names: ["local_explorer"],
    },
    status: "active",
  };
}

test("JSON schema validation covers component input and result constraints", () => {
  const descriptor = pickerDescriptor();
  assert.deepEqual(
    validateJsonSchema({ allow_create: true }, descriptor.input_schema),
    [],
  );
  assert.match(
    validateJsonSchema(
      { allow_create: "yes", unexpected: true },
      descriptor.input_schema,
    ).join(" "),
    /allow_create.*boolean.*unexpected.*not allowed/i,
  );
  assert.match(
    validateJsonSchema(
      { path: "", resource_type: "remote" },
      descriptor.output_schema,
    ).join(" "),
    /shorter.*constant/i,
  );
});

test("addon API clients scope /api requests beneath the provider addon", async () => {
  let requestedUrl = "";
  const api = createAddonApiClient(
    "http://127.0.0.1:5003",
    "local_explorer",
    async (input) => {
      requestedUrl = String(input);
      return new Response(JSON.stringify({ current_path: "C:\\" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    },
  );

  await api.request("/api/browse?path=C%3A%5C");
  assert.equal(
    requestedUrl,
    "http://127.0.0.1:5003/addons/local_explorer/api/browse?path=C%3A%5C",
  );
  await assert.rejects(
    () => api.request("/admin/users"),
    /addon API requests/i,
  );
});

test("UI component host mounts lazily, validates the result, unmounts, and restores focus", async () => {
  const document = new FakeDocument();
  const trigger = new FakeElement("button", document);
  document.activeElement = trigger;

  let context: UiComponentMountContext | undefined;
  let unmounted = false;
  let notifyMounted: (() => void) | undefined;
  const mounted = new Promise<void>((resolve) => {
    notifyMounted = resolve;
  });

  const resultPromise = openUiComponent<
    { allow_create: boolean },
    { path: string; resource_type: "local_path" }
  >(
    pickerDescriptor(),
    { allow_create: true },
    {
      applicationOrigin: document.location.origin,
      document: document as unknown as Document,
      loadStylesheets: async () => {},
      importModule: async () => ({
        apiVersion: "1.0",
        mount: (_target: Element, mountContext: UiComponentMountContext) => {
          context = mountContext;
          notifyMounted?.();
          return {
            unmount() {
              unmounted = true;
            },
          };
        },
      }),
    },
  );

  await mounted;
  context?.close({
    path: "C:\\Exports",
    resource_type: "local_path",
  });
  assert.deepEqual(await resultPromise, {
    path: "C:\\Exports",
    resource_type: "local_path",
  });
  assert.equal(unmounted, true);
  assert.equal(trigger.focused, true);
  assert.equal(document.body.children.length, 0);
});

test("UI component host preserves an explicit null input", async () => {
  const document = new FakeDocument();
  const descriptor = pickerDescriptor();
  descriptor.input_schema = { type: "null" };
  let context: UiComponentMountContext | undefined;
  let notifyMounted: (() => void) | undefined;
  const mounted = new Promise<void>((resolve) => {
    notifyMounted = resolve;
  });

  const resultPromise = openUiComponent(descriptor, null, {
    applicationOrigin: document.location.origin,
    document: document as unknown as Document,
    loadStylesheets: async () => {},
    importModule: async () => ({
      apiVersion: "1.0",
      mount: (_target: Element, mountContext: UiComponentMountContext) => {
        context = mountContext;
        notifyMounted?.();
        return { unmount() {} };
      },
    }),
  });

  await mounted;
  assert.equal(context?.input, null);
  context?.close();
  assert.equal(await resultPromise, undefined);
});

test("UI component host treats the native dialog cancel event as keyboard cancellation", async () => {
  const document = new FakeDocument();
  const trigger = new FakeElement("button", document);
  document.activeElement = trigger;
  let notifyMounted: (() => void) | undefined;
  const mounted = new Promise<void>((resolve) => {
    notifyMounted = resolve;
  });
  let unmounted = false;

  const resultPromise = openUiComponent(
    pickerDescriptor(),
    { allow_create: true },
    {
      applicationOrigin: document.location.origin,
      document: document as unknown as Document,
      loadStylesheets: async () => {},
      importModule: async () => ({
        apiVersion: "1.0",
        mount: () => {
          notifyMounted?.();
          return {
            unmount() {
              unmounted = true;
            },
          };
        },
      }),
    },
  );

  await mounted;
  const dialog = document.body.children[0];
  assert.ok(dialog);
  const cancel = new Event("cancel", { cancelable: true });
  assert.equal(dialog.dispatchEvent(cancel), false);
  assert.equal(cancel.defaultPrevented, true);
  assert.equal(await resultPromise, undefined);
  assert.equal(unmounted, true);
  assert.equal(trigger.focused, true);
  assert.equal(document.body.children.length, 0);
});

test("UI component host rejects an invalid provider result after cleanup", async () => {
  const document = new FakeDocument();
  let context: UiComponentMountContext | undefined;
  let notifyMounted: (() => void) | undefined;
  const mounted = new Promise<void>((resolve) => {
    notifyMounted = resolve;
  });

  const resultPromise = openUiComponent(
    pickerDescriptor(),
    { allow_create: false },
    {
      applicationOrigin: document.location.origin,
      document: document as unknown as Document,
      loadStylesheets: async () => {},
      importModule: async () => ({
        apiVersion: "1.0",
        mount: (_target: Element, mountContext: UiComponentMountContext) => {
          context = mountContext;
          notifyMounted?.();
          return { unmount() {} };
        },
      }),
    },
  );

  await mounted;
  context?.close({ path: "C:\\Exports", resource_type: "remote" });
  await assert.rejects(resultPromise, /result does not match its schema/i);
  assert.equal(document.body.children.length, 0);
});
