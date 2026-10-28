function withQueryToken(url, shareToken) {
  if (!shareToken) return url;
  if (url.includes("token=")) return url;
  return `${url}${url.includes("?") ? "&" : "?"}token=${encodeURIComponent(shareToken)}`;
}

async function decodeResponse(res, mode) {
  if (mode === "text") {
    return res.text();
  }

  const bodyText = await res.text();
  if (!bodyText) return {};
  try {
    return JSON.parse(bodyText);
  } catch {
    return { raw: bodyText };
  }
}

export class ApiError extends Error {
  constructor(message, { status = 0, payload = null, code = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
    this.code = code;
  }
}

export function responseError(payload, status) {
  let message;
  if (payload && typeof payload === "object") {
    const candidate = payload.error || payload.message || payload.raw;
    if (candidate) message = String(candidate);
  }
  return new ApiError(message || `Request failed (${status})`, {
    status,
    payload,
    code: payload?.code || payload?.status || null
  });
}

export function isAbortError(error) {
  return error?.name === "AbortError" || error?.code === "ABORT_ERR";
}

export function createIdempotencyKey(prefix = "translator") {
  const random = typeof globalThis.crypto?.randomUUID === "function"
    ? globalThis.crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}.${random}`;
}

export function createApiClient({
  shareToken = "",
  shareGrantToken = "",
  shareSessionCsrf = "",
  addonApi = null,
  signal = null
} = {}) {
  const authFetch = async (url, options = {}) => {
    const headers = { ...(options.headers || {}) };
    if (shareToken) {
      headers["X-Shared-Token"] = shareToken;
    }
    if (shareGrantToken) {
      headers["X-Translator-Share-Token"] = shareGrantToken;
    }
    const method = String(options.method || "GET").toUpperCase();
    if (shareSessionCsrf && !["GET", "HEAD", "OPTIONS"].includes(method)) {
      headers["X-Translator-CSRF"] = shareSessionCsrf;
    }
    return fetch(url, {
      ...options,
      ...(options.signal || !signal ? {} : { signal }),
      credentials: options.credentials || "same-origin",
      headers
    });
  };

  const request = async (url, options = {}, mode = "json") => {
    // Keep share tokens in the header for API calls. Query-string tokens are
    // reserved for top-level navigation/download URLs where a browser cannot
    // attach X-Shared-Token itself.
    const parsed = new URL(url, window.location.origin);
    const addonBase = "/addons/translator";
    if (
      addonApi &&
      !shareToken &&
      !shareGrantToken &&
      !shareSessionCsrf &&
      mode === "json" &&
      parsed.origin === window.location.origin &&
      parsed.pathname.startsWith(`${addonBase}/api/`)
    ) {
      return addonApi.request(
        `${parsed.pathname.slice(addonBase.length)}${parsed.search}`,
        options.signal || !signal ? options : { ...options, signal }
      );
    }
    const res = await authFetch(url, options);
    const payload = await decodeResponse(res, mode);
    if (!res.ok) {
      throw responseError(payload, res.status);
    }
    return payload;
  };

  return {
    shareToken,
    shareGrantToken,
    shareSessionCsrf,
    authFetch,
    tokenizedUrl: (url) => withQueryToken(url, shareToken),
    fetchJson: (url, options = {}) => request(url, options, "json"),
    fetchText: (url, options = {}) => request(url, options, "text")
  };
}
