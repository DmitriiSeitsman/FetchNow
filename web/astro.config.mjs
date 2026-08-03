import { defineConfig } from "astro/config";

export default defineConfig({
  output: "static",
  compressHTML: true,
  build: {
    format: "directory",
  },
  server: {
    port: 4321,
    host: true,
  },
});
