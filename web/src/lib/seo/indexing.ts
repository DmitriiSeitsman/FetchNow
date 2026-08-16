/** Build-time search indexing flag. Fail-closed with strict boolean strings. */

export class SearchIndexingConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SearchIndexingConfigError";
  }
}

/**
 * Parse PUBLIC_SEARCH_INDEXING_ENABLED.
 *
 * - missing / undefined → false (fail-closed default)
 * - exact "true" / true → true
 * - exact "false" / false → false
 * - anything else → configuration error (never silently coerce)
 */
export function parseSearchIndexingFlag(
  raw: string | boolean | undefined,
): boolean {
  if (raw === undefined) {
    return false;
  }
  if (raw === true || raw === "true") {
    return true;
  }
  if (raw === false || raw === "false") {
    return false;
  }
  throw new SearchIndexingConfigError(
    `PUBLIC_SEARCH_INDEXING_ENABLED must be exactly "true" or "false" (or unset); got ${JSON.stringify(String(raw))}`,
  );
}

export function isSearchIndexingEnabled(
  raw: string | boolean | undefined = import.meta.env
    .PUBLIC_SEARCH_INDEXING_ENABLED,
): boolean {
  return parseSearchIndexingFlag(raw);
}

export function robotsMetaContent(indexingEnabled: boolean): string {
  return indexingEnabled ? "index,follow" : "noindex,nofollow";
}
