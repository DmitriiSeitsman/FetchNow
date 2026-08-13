/// <reference types="astro/client" />

interface ImportMetaEnv {
  readonly PUBLIC_SITE_URL: string;
  readonly PUBLIC_MEDIA_FLOW_ENABLED?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
