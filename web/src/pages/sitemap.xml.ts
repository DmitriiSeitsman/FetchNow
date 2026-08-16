import type { APIRoute } from "astro";
import { isSearchIndexingEnabled } from "../lib/seo/indexing";
import { absoluteUrl, indexablePaths } from "../lib/seo/pages";

export const GET: APIRoute = () => {
  if (!isSearchIndexingEnabled()) {
    return new Response(
      `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>\n`,
      {
        headers: {
          "Content-Type": "application/xml; charset=utf-8",
          "Cache-Control": "public, max-age=300",
          "X-Robots-Tag": "noindex, nofollow",
        },
      },
    );
  }

  const urls = indexablePaths()
    .map((path) => `  <url>\n    <loc>${absoluteUrl(path)}</loc>\n  </url>`)
    .join("\n");

  const body = `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${urls}\n</urlset>\n`;
  return new Response(body, {
    headers: {
      "Content-Type": "application/xml; charset=utf-8",
      "Cache-Control": "public, max-age=300",
    },
  });
};
