(function installCarlinaCsrfFetch(windowObject) {
    "use strict";

    if (!windowObject || windowObject.__carlinaCsrfFetchInstalled) {
        return;
    }

    const originalFetch = windowObject.fetch.bind(windowObject);
    const unsafeMethods = new Set(["POST", "PUT", "PATCH", "DELETE"]);

    function csrfToken() {
        return document.querySelector('meta[name="csrf-token"]')?.content || "";
    }

    windowObject.fetch = function carlinaFetch(input, init) {
        const requestUrl = new URL(
            input instanceof Request ? input.url : String(input),
            windowObject.location.href,
        );
        const method = String(
            init?.method || (input instanceof Request ? input.method : "GET"),
        ).toUpperCase();
        if (requestUrl.origin !== windowObject.location.origin || !unsafeMethods.has(method)) {
            return originalFetch(input, init);
        }

        const headers = new Headers(
            init?.headers || (input instanceof Request ? input.headers : undefined),
        );
        if (!headers.has("X-CSRF-Token")) {
            headers.set("X-CSRF-Token", csrfToken());
        }
        return originalFetch(input, { ...init, method, headers });
    };
    windowObject.__carlinaCsrfFetchInstalled = true;
})(window);
