import assert from "node:assert/strict";
import test from "node:test";

import type {
  FrontendAddonOperation,
  FrontendApiClient,
  FrontendCapabilityCandidate,
} from "@carlina/sdk";

import {
  addonApiErrorPayload,
  addonApiErrorStatus,
  createAddonManagerApi,
  dependencyReviewNoticesFromError,
} from "../src/lib/addon-manager-api.ts";

interface RecordedRequest {
  path: string;
  init: RequestInit;
}

function recordingClient(response: unknown = {}): {
  client: FrontendApiClient;
  requests: RecordedRequest[];
} {
  const requests: RecordedRequest[] = [];
  return {
    requests,
    client: {
      async request<Value>(path: string, init: RequestInit = {}) {
        requests.push({ path, init });
        return response as Value;
      },
    },
  };
}

function jsonBody(request: RecordedRequest): unknown {
  assert.equal(request.init.headers instanceof Headers, false);
  return JSON.parse(String(request.init.body));
}

function operation(overrides: Partial<FrontendAddonOperation> = {}): FrontendAddonOperation {
  return {
    id: "op-1",
    job_id: "job-1",
    addon_id: "email",
    operation: "activate",
    status: "running",
    stage: "runtime_load",
    progress: 50,
    message: "Loading",
    error: null,
    bytes: { current: null, total: null },
    last_activity_at: null,
    queued_at: "2026-08-04T12:00:00Z",
    started_at: "2026-08-04T12:00:01Z",
    completed_at: null,
    updated_at: "2026-08-04T12:00:02Z",
    attempt: 1,
    recovered_count: 0,
    cancel_requested: false,
    cancellable: true,
    retryable: false,
    retry_expires_at: null,
    input_retained: null,
    admission: null,
    readiness: {},
    result: null,
    log_source_ids: [],
    log_links: [],
    status_url: "/api/addons/operations/op-1",
    cancel_url: "/api/addons/operations/op-1/cancel",
    retry_url: "/api/addons/operations/op-1/retry",
    ...overrides,
  };
}

test("owns encoded read endpoints and request cancellation signals", async () => {
  const { client, requests } = recordingClient();
  const api = createAddonManagerApi(client);
  const controller = new AbortController();

  await api.detail("mail/unsafe", controller.signal);
  await api.capabilityResolution("mail/unsafe", controller.signal);
  await api.operations(40, controller.signal);

  assert.deepEqual(
    requests.map((request) => request.path),
    [
      "/api/addons/mail%2Funsafe/detail",
      "/api/addons/mail%2Funsafe/capability-resolution",
      "/api/addons/operations?limit=40",
    ],
  );
  assert.equal(requests.every((request) => request.init.signal === controller.signal), true);
});

test("maps lifecycle commands to typed endpoints and bodies", async () => {
  const { client, requests } = recordingClient({ message: "queued" });
  const api = createAddonManagerApi(client);

  await api.execute({
    kind: "activate",
    addonId: "email",
    dependencyReview: ["imap-license"],
  });
  await api.execute({
    kind: "purge",
    addonId: "email",
    confirmation: "PURGE email",
  });
  await api.execute({ kind: "restart-workers", addonId: "email" });
  await api.execute({ kind: "run-job", addonId: "email", jobId: "sync/daily" });
  await api.execute({ kind: "restart-server" });

  assert.deepEqual(
    requests.map((request) => request.path),
    [
      "/api/addons/activate",
      "/api/addons/purge-data",
      "/api/addons/email/workers/restart",
      "/api/addons/email/jobs/sync%2Fdaily/trigger",
      "/api/system/restart",
    ],
  );
  assert.deepEqual(jsonBody(requests[0]), {
    addon_id: "email",
    dependency_review: ["imap-license"],
  });
  assert.deepEqual(jsonBody(requests[1]), {
    addon_id: "email",
    confirmation: "PURGE email",
  });
  assert.equal(requests.every((request) => request.init.method === "POST"), true);
});

test("serializes capability bindings and local install approvals", async () => {
  const { client, requests } = recordingClient();
  const api = createAddonManagerApi(client);
  const candidate = {
    addon_id: "gmail",
    provider_id: "gmail.messages",
    capability: "email.messages",
  } as FrontendCapabilityCandidate;

  await api.bindCapability("email", {
    requirementId: "mail-provider",
    candidate,
    connectionId: "connection-7",
    selected: true,
  });
  const file = new File(["package"], "email.zip", { type: "application/zip" });
  await api.installLocal({
    file,
    approveNativeCode: true,
    dependencyReview: ["imap-license", "html-license"],
  });

  assert.equal(requests[0].path, "/api/addons/email/capability-bindings");
  assert.deepEqual(jsonBody(requests[0]), {
    requirement_id: "mail-provider",
    provider_addon_id: "gmail",
    provider_capability_id: "gmail.messages",
    connection_id: "connection-7",
    selected: true,
  });
  assert.equal(requests[1].path, "/api/addons/install");
  assert.equal(requests[1].init.body instanceof FormData, true);
  const form = requests[1].init.body as FormData;
  assert.equal((form.get("addon_zip") as File).name, "email.zip");
  assert.equal(form.get("approve_native_code"), "true");
  assert.deepEqual(form.getAll("dependency_review"), [
    "imap-license",
    "html-license",
  ]);
});

test("uses server-provided operation control links only inside the adapter", async () => {
  const response = operation({ status: "cancelled" });
  const { client, requests } = recordingClient(response);
  const api = createAddonManagerApi(client);

  assert.equal(await api.controlOperation(operation(), "cancel"), response);
  assert.equal(requests[0].path, "/api/addons/operations/op-1/cancel");
  assert.equal(requests[0].init.method, "POST");
});

test("validates dependency-review conflict payloads at the transport boundary", () => {
  const error = Object.assign(new Error("Review required"), {
    status: 409,
    payload: {
      status: "dependency_review_required",
      dependency_notices: [
        {
          id: "pdf-license",
          packages: ["PyMuPDF"],
          license: "AGPL-3.0 or commercial",
          purpose: "PDF rendering",
          review_required: true,
        },
        { id: "invalid", packages: [42] },
      ],
    },
  });

  assert.equal(addonApiErrorStatus(error), 409);
  assert.equal(addonApiErrorPayload(error)?.status, "dependency_review_required");
  assert.deepEqual(dependencyReviewNoticesFromError(error), [
    {
      id: "pdf-license",
      packages: ["PyMuPDF"],
      license: "AGPL-3.0 or commercial",
      purpose: "PDF rendering",
      review_required: true,
    },
  ]);
  assert.deepEqual(dependencyReviewNoticesFromError(new Error("plain")), []);
});
