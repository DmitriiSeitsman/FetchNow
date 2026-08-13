import { defineConfig } from "astro/config";

export default defineConfig({
  output: "static",
  compressHTML: true,
  build: {
    format: "directory",
    inlineStylesheets: "never",
  },
  vite: {
    build: {
      assetsInlineLimit: 0,
    },
  },
  server: {
    port: 4321,
    host: true,
  },
});
