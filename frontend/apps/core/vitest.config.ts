import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vitest/config";

import { coreRouteAliases } from "./src/lib/frontend-build-profile.ts";

export default defineConfig({
  plugins: [svelte({ configFile: false })],
  resolve: { conditions: ["browser"], alias: coreRouteAliases() },
  test: {
    environment: "jsdom",
    include: ["test/components/**/*.test.ts"],
    setupFiles: ["test/components/setup.ts"],
  },
});
