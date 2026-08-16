import { describe, expect, it } from "vitest";
import {
  parseSiteUrl,
  resolveSiteUrl,
  SiteUrlConfigError,
} from "./site-url";

describe("PUBLIC_SITE_URL validation", () => {
  it("accepts valid origins", () => {
    expect(parseSiteUrl("https://fetchnow.online")).toBe("https://fetchnow.online");
    expect(parseSiteUrl("https://staging.fetchnow.online")).toBe(
      "https://staging.fetchnow.online",
    );
    expect(parseSiteUrl("http://localhost")).toBe("http://localhost");
    expect(parseSiteUrl("http://localhost:4321")).toBe("http://localhost:4321");
    expect(parseSiteUrl("https://fetchnow.online/")).toBe("https://fetchnow.online");
  });

  it("rejects invalid values", () => {
    for (const raw of [
      "fetchnow.online",
      "//fetchnow.online",
      "ftp://fetchnow.online",
      "https://",
      "https://fetchnow.online/foo",
      "https://fetchnow.online?x=1",
      "https://fetchnow.online/#foo",
      "https://user:pass@fetchnow.online",
    ]) {
      expect(() => parseSiteUrl(raw)).toThrow(SiteUrlConfigError);
    }
  });

  it("allows localhost fallback only when indexing is off", () => {
    expect(resolveSiteUrl({ raw: undefined, indexingEnabled: false })).toBe(
      "http://localhost",
    );
    expect(() =>
      resolveSiteUrl({ raw: undefined, indexingEnabled: true }),
    ).toThrow(SiteUrlConfigError);
    expect(() =>
      resolveSiteUrl({ raw: "", indexingEnabled: true }),
    ).toThrow(SiteUrlConfigError);
  });
});
