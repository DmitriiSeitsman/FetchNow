import { describe, expect, it, vi } from "vitest";
import { generateAccessToken, isCanonicalAccessToken } from "./credentials";
import { sameOriginApiUrl } from "./api";

describe("credentials", () => {
  it("produces 43-char canonical base64url from 32 CSPRNG bytes", () => {
    const token = generateAccessToken();
    expect(token).toHaveLength(43);
    expect(isCanonicalAccessToken(token)).toBe(true);
  });

  it("does not use Math.random", () => {
    const spy = vi.spyOn(Math, "random");
    generateAccessToken();
    expect(spy).not.toHaveBeenCalled();
    spy.mockRestore();
  });

  it("never appears in constructed API URLs", () => {
    const token = generateAccessToken();
    const url = sameOriginApiUrl("/api/v1/media/jobs", "http://localhost");
    expect(url).not.toContain(token);
    expect(url).not.toContain("?");
    expect(url).not.toContain("#");
  });

  it("rejects padded or non-canonical tokens", () => {
    const token = generateAccessToken();
    expect(isCanonicalAccessToken(token + "=")).toBe(false);
    expect(isCanonicalAccessToken("x".repeat(43))).toBe(false);
  });
});
