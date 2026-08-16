/** Build-time PUBLIC_SITE_URL validation for canonical / OG / sitemap. */

export class SiteUrlConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SiteUrlConfigError";
  }
}

const DEFAULT_DEV_SITE_URL = "http://localhost";

/**
 * Validate and normalize a public site origin.
 * Returns origin without a trailing slash (http://localhost:4321, https://fetchnow.online).
 */
export function parseSiteUrl(raw: string | undefined): string {
  if (raw === undefined || raw.trim() === "") {
    throw new SiteUrlConfigError(
      "PUBLIC_SITE_URL is required and must be an absolute http(s) origin",
    );
  }
  const trimmed = raw.trim();
  let url: URL;
  try {
    url = new URL(trimmed);
  } catch {
    throw new SiteUrlConfigError(
      `PUBLIC_SITE_URL must be an absolute URL; got ${JSON.stringify(trimmed)}`,
    );
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new SiteUrlConfigError(
      `PUBLIC_SITE_URL must use http or https; got protocol ${url.protocol}`,
    );
  }
  if (!url.hostname) {
    throw new SiteUrlConfigError("PUBLIC_SITE_URL must include a hostname");
  }
  if (url.search) {
    throw new SiteUrlConfigError("PUBLIC_SITE_URL must not include a query string");
  }
  if (url.hash) {
    throw new SiteUrlConfigError("PUBLIC_SITE_URL must not include a fragment");
  }
  const path = url.pathname === "" ? "/" : url.pathname;
  if (path !== "/") {
    throw new SiteUrlConfigError(
      "PUBLIC_SITE_URL must be an origin only (pathname must be /)",
    );
  }
  // Drop credentials if present — they must never appear in canonical URLs.
  if (url.username || url.password) {
    throw new SiteUrlConfigError("PUBLIC_SITE_URL must not include credentials");
  }
  return url.origin;
}

/**
 * Resolve the site origin for builds.
 * When indexing is disabled, missing PUBLIC_SITE_URL falls back to local default.
 * When indexing is enabled, an explicit valid PUBLIC_SITE_URL is required.
 */
export function resolveSiteUrl(options?: {
  raw?: string | undefined;
  indexingEnabled?: boolean;
}): string {
  const indexingEnabled = options?.indexingEnabled ?? false;
  const raw =
    options?.raw !== undefined
      ? options.raw
      : import.meta.env.PUBLIC_SITE_URL;

  if (raw === undefined || String(raw).trim() === "") {
    if (indexingEnabled) {
      throw new SiteUrlConfigError(
        "PUBLIC_SITE_URL is required when PUBLIC_SEARCH_INDEXING_ENABLED=true",
      );
    }
    return DEFAULT_DEV_SITE_URL;
  }
  return parseSiteUrl(String(raw));
}
