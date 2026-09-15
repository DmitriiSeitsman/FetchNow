import { describe, expect, it } from "vitest";
import { FlowError } from "./errors";
import {
  suggestedFilename,
  isSecureDeliveryContext,
  contentDispositionHeader,
  parseContentDispositionFilename,
  encodeRfc8187,
  decodeRfc8187,
  computeGrantRefreshDelayMs,
  GRANT_REISSUE_BUFFER_MS,
  MIN_GRANT_REFRESH_DELAY_MS,
} from "./download";
import { DOWNLOAD_ID } from "./fixtures";

describe("delivery filename helpers", () => {
  it("keeps the token out of the suggested filename", () => {
    const name = suggestedFilename(DOWNLOAD_ID, "mp4");
    expect(name).toBe(`fetchnow-${DOWNLOAD_ID}.mp4`);
    expect(name).not.toContain("Bearer");
  });

  it("rejects a non-UUID job id or an implausible container", () => {
    expect(() => suggestedFilename("not-a-uuid", "mp4")).toThrow(FlowError);
    expect(() => suggestedFilename(DOWNLOAD_ID, "MP4!")).toThrow(FlowError);
  });

  it("round-trips a filename through Content-Disposition", () => {
    const name = suggestedFilename(DOWNLOAD_ID, "mp4");
    const header = contentDispositionHeader(name, name);
    expect(parseContentDispositionFilename(header, name)).toBe(name);
  });

  it("refuses a header whose filename does not match the expected name", () => {
    const name = suggestedFilename(DOWNLOAD_ID, "mp4");
    const header = contentDispositionHeader(name, name);
    expect(parseContentDispositionFilename(header, "other.mp4")).toBeNull();
    expect(parseContentDispositionFilename(null, name)).toBeNull();
    expect(parseContentDispositionFilename("inline", name)).toBeNull();
  });

  it("encodes and decodes RFC 8187 values", () => {
    const encoded = encodeRfc8187("видео.mp4");
    expect(encoded.startsWith("UTF-8''")).toBe(true);
    expect(decodeRfc8187(encoded)).toBe("видео.mp4");
    expect(decodeRfc8187("bogus")).toBeNull();
  });
});

describe("secure delivery context", () => {
  it("detects secure delivery contexts for native download", () => {
    expect(isSecureDeliveryContext("https://example.com")).toBe(true);
    expect(isSecureDeliveryContext("http://localhost")).toBe(true);
    expect(isSecureDeliveryContext("http://127.0.0.1")).toBe(true);
    expect(isSecureDeliveryContext("http://example.com")).toBe(false);
  });
});

describe("computeGrantRefreshDelayMs", () => {
  it("never returns zero for a still-valid 30s grant equal to the buffer", () => {
    const now = 1_000_000;
    const delay = computeGrantRefreshDelayMs(now + 30_000, now);
    expect(delay).not.toBeNull();
    expect(delay!).toBeGreaterThanOrEqual(MIN_GRANT_REFRESH_DELAY_MS);
    expect(delay!).toBeLessThan(30_000);
    expect(delay!).toBe(15_000);
  });

  it("keeps a positive bounded delay for a 31s grant", () => {
    const now = 1_000_000;
    const delay = computeGrantRefreshDelayMs(now + 31_000, now);
    expect(delay).not.toBeNull();
    expect(delay!).toBeGreaterThanOrEqual(1);
    expect(delay!).toBeLessThan(31_000);
  });

  it("refreshes before expiry for a normal 300s grant", () => {
    const now = 1_000_000;
    const delay = computeGrantRefreshDelayMs(now + 300_000, now);
    expect(delay).toBe(300_000 - GRANT_REISSUE_BUFFER_MS);
  });

  it("returns null after expiry and never schedules past remaining life", () => {
    const now = 1_000_000;
    expect(computeGrantRefreshDelayMs(now - 1, now)).toBeNull();
    const short = computeGrantRefreshDelayMs(now + 3_000, now);
    expect(short).not.toBeNull();
    expect(short!).toBeLessThan(3_000);
    expect(short!).toBeGreaterThanOrEqual(1);
  });
});
