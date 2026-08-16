import type { APIRoute } from "astro";
import { isSearchIndexingEnabled } from "../lib/seo/indexing";
import { siteUrl } from "../lib/seo/pages";

export const GET: APIRoute = () => {
  const indexingEnabled = isSearchIndexingEnabled();
  const origin = siteUrl();
  // Pre-launch: allow crawlers to fetch HTML so they can honor meta/X-Robots-Tag
  // noindex. Do not advertise a sitemap until indexing is intentionally enabled.
  const body = indexingEnabled
    ? [
        "User-agent: *",
        "Allow: /",
        `Sitemap: ${origin}/sitemap.xml`,
        "",
      ].join("\n")
    : ["User-agent: *", "Allow: /", ""].join("\n");

  return new Response(body, {
    headers: {
      "Content-Type": "text/plain; charset=utf-8",
      "Cache-Control": "public, max-age=300",
    },
  });
};
