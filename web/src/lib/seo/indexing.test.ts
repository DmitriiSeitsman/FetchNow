import { describe, expect, it } from "vitest";
import {
  parseSearchIndexingFlag,
  robotsMetaContent,
  SearchIndexingConfigError,
} from "./indexing";

describe("search indexing flag", () => {
  it("treats missing as false", () => {
    expect(parseSearchIndexingFlag(undefined)).toBe(false);
  });

  it("accepts exact true/false strings and booleans", () => {
    expect(parseSearchIndexingFlag("true")).toBe(true);
    expect(parseSearchIndexingFlag(true)).toBe(true);
    expect(parseSearchIndexingFlag("false")).toBe(false);
    expect(parseSearchIndexingFlag(false)).toBe(false);
  });

  it("rejects invalid explicit values", () => {
    for (const raw of ["yes", "1", "TRUE", "False", "enabled", "foo", ""]) {
      expect(() => parseSearchIndexingFlag(raw)).toThrow(SearchIndexingConfigError);
    }
  });

  it("emits fail-closed robots meta by default", () => {
    expect(robotsMetaContent(false)).toBe("noindex,nofollow");
    expect(robotsMetaContent(true)).toBe("index,follow");
  });
});
