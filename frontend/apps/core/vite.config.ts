import { fileURLToPath, URL } from "node:url";

import { svelte } from "@sveltejs/vite-plugin-svelte";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

import { coreRouteAliases } from "./src/lib/frontend-build-profile.ts";

const outputDirectory = fileURLToPath(
  new URL("../../../iarf_assistant/static/frontend", import.meta.url),
);
const entry = fileURLToPath(new URL("./src/main.ts", import.meta.url));

export default defineConfig({
  base: "./",
  plugins: [tailwindcss(), svelte({ configFile: false })],
  resolve: { alias: coreRouteAliases() },
  build: {
    cssCodeSplit: true,
    emptyOutDir: true,
    manifest: "manifest.json",
    outDir: outputDirectory,
    sourcemap: false,
    rollupOptions: {
      input: entry,
      preserveEntrySignatures: "strict",
      output: {
        assetFileNames: (assetInfo) =>
          assetInfo.names.some((name) => name.endsWith(".css"))
            ? "assets/core-[hash][extname]"
            : "assets/[name]-[hash][extname]",
        chunkFileNames: "assets/[name]-[hash].js",
        entryFileNames: "assets/core-[hash].js",
        format: "es",
      },
    },
  },
});
