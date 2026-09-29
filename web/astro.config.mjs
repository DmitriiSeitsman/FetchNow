import { defineConfig } from "astro/config";

/**
 * Dev/preview bind policy (SEC-02):
 * - default: loopback only (127.0.0.1)
 * - LAN opt-in: FETCHNOW_ASTRO_HOST=lan (or =true / =0.0.0.0)
 * Production Nginx/gateway ports are unrelated and unchanged.
 */
function resolveDevHost() {
  const raw = (process.env.FETCHNOW_ASTRO_HOST ?? "").trim().toLowerCase();
  if (raw === "lan" || raw === "true" || raw === "0.0.0.0" || raw === "*") {
    return true;
  }
  return "127.0.0.1";
}

export default defineConfig({
  output: "static",
  // Keep HTML-aware compression explicitly. Astro 7 default is 'jsx', which
  // can drop spaces between adjacent inline elements.
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
    host: resolveDevHost(),
  },
});
