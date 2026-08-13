import { describe, expect, it } from "vitest";
import { generateAccessToken } from "./credentials";
import {
  FlowSession,
  LEGACY_SESSION_KEY,
  parseRecoveryRecord,
  SESSION_KEY,
  SESSION_VERSION,
} from "./session";
import { JOB_ID, OPTION_ID, EXPIRES, DOWNLOAD_ID } from "./fixtures";

function memory() {
  const map = new Map<string, string>();
  return {
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => {
      map.set(k, v);
    },
    removeItem: (k: string) => {
      map.delete(k);
    },
    map,
  };
}

describe("session recovery", () => {
  it("rejects malformed records, unknown fields, and schema v1", () => {
    expect(parseRecoveryRecord({ v: 1, extra: true })).toBeNull();
    expect(parseRecoveryRecord({ v: 2 })).toBeNull();
    const token = generateAccessToken();
    expect(
      parseRecoveryRecord({
        v: 1,
        token,
        mediaJobId: JOB_ID,
        downloadJobId: null,
        formatOptionId: null,
        phase: "inspecting",
        expiresAt: EXPIRES,
      }),
    ).toBeNull();
  });

  it("stores a bounded schema without submitted URLs", () => {
    const store = memory();
    const session = new FlowSession(store);
    const token = generateAccessToken();
    session.write({
      v: SESSION_VERSION,
      token,
      mediaJobId: JOB_ID,
      downloadJobId: null,
      formatOptionId: OPTION_ID,
      phase: "inspecting",
      expiresAt: EXPIRES,
    });
    const raw = store.getItem(SESSION_KEY) ?? "";
    expect(raw).not.toContain("vk.com");
    expect(raw).not.toContain("http");
    expect(raw).not.toContain("Bearer");
    const read = session.read(Date.parse("2026-08-13T04:30:00Z"));
    expect(read?.token).toBe(token);
    expect(read?.mediaJobId).toBe(JOB_ID);
    expect(read?.v).toBe(2);
  });

  it("clears expired records", () => {
    const store = memory();
    const session = new FlowSession(store);
    session.write({
      v: SESSION_VERSION,
      token: generateAccessToken(),
      mediaJobId: JOB_ID,
      downloadJobId: null,
      formatOptionId: null,
      phase: "inspecting",
      expiresAt: "2020-01-01T00:00:00Z",
    });
    expect(session.read()).toBeNull();
    expect(store.getItem(SESSION_KEY)).toBeNull();
  });

  it("discards incompatible legacy v1 session keys", () => {
    const store = memory();
    const token = generateAccessToken();
    store.setItem(
      LEGACY_SESSION_KEY,
      JSON.stringify({
        v: 1,
        token,
        mediaJobId: JOB_ID,
        downloadJobId: DOWNLOAD_ID,
        formatOptionId: OPTION_ID,
        phase: "downloading",
        expiresAt: EXPIRES,
      }),
    );
    const session = new FlowSession(store);
    expect(session.read(Date.parse("2026-08-13T04:30:00Z"))).toBeNull();
    expect(store.getItem(LEGACY_SESSION_KEY)).toBeNull();
    expect(store.getItem(SESSION_KEY)).toBeNull();
  });

  it("rejects incoherent and terminal recovery records", () => {
    const token = generateAccessToken();
    expect(
      parseRecoveryRecord({
        v: SESSION_VERSION,
        token,
        mediaJobId: null,
        downloadJobId: DOWNLOAD_ID,
        formatOptionId: OPTION_ID,
        phase: "ready",
        expiresAt: EXPIRES,
      }),
    ).toBeNull();
    expect(
      parseRecoveryRecord({
        v: SESSION_VERSION,
        token,
        mediaJobId: JOB_ID,
        downloadJobId: DOWNLOAD_ID,
        formatOptionId: null,
        phase: "ready",
        expiresAt: EXPIRES,
      }),
    ).toBeNull();
    expect(
      parseRecoveryRecord({
        v: SESSION_VERSION,
        token,
        mediaJobId: JOB_ID,
        downloadJobId: null,
        formatOptionId: OPTION_ID,
        phase: "completed",
        expiresAt: EXPIRES,
      }),
    ).toBeNull();
    expect(
      parseRecoveryRecord({
        v: SESSION_VERSION,
        token,
        mediaJobId: JOB_ID,
        downloadJobId: null,
        formatOptionId: null,
        phase: "inspection_failed",
        expiresAt: EXPIRES,
      }),
    ).toBeNull();
  });

  it("does not throw when sessionStorage methods fail", () => {
    const token = generateAccessToken();
    const session = new FlowSession({
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("quota");
      },
      removeItem: () => {
        throw new Error("blocked");
      },
    });
    expect(session.read()).toBeNull();
    expect(() =>
      session.write({
        v: SESSION_VERSION,
        token,
        mediaJobId: JOB_ID,
        downloadJobId: null,
        formatOptionId: null,
        phase: "inspecting",
        expiresAt: EXPIRES,
      }),
    ).not.toThrow();
    expect(() => session.clear()).not.toThrow();
  });
});
