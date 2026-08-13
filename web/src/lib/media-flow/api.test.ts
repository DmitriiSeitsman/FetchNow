import { describe, expect, it, vi } from "vitest";
import { MediaApi, sameOriginApiUrl } from "./api";
import { generateAccessToken } from "./credentials";
import { inspectedPayload, inspectionPayload, JOB_ID } from "./fixtures";

describe("api client", () => {
  it("sends Bearer to same-origin paths only", async () => {
    const token = generateAccessToken();
    const fetchImpl = vi.fn(async () => {
      return new Response(JSON.stringify(inspectionPayload()), { status: 202 });
    });
    const api = new MediaApi({
      origin: "http://localhost",
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });
    await api.createInspectionJob("https://vk.com/video1", token);
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost/api/v1/media/jobs");
    expect(url.startsWith("http://localhost/api/")).toBe(true);
    expect(String(url)).not.toContain(token);
    expect(init.credentials).toBe("same-origin");
    expect(init.redirect).toBe("error");
    expect(init.headers).toBeInstanceOf(Headers);
    expect((init.headers as Headers).get("Authorization")).toBe(`Bearer ${token}`);
  });

  it("rejects non-api or query-bearing paths", () => {
    expect(() => sameOriginApiUrl("/other", "http://localhost")).toThrow();
    expect(() =>
      sameOriginApiUrl("/api/v1/media/jobs?x=1", "http://localhost"),
    ).toThrow();
  });

  it("parses inspected status", async () => {
    const token = generateAccessToken();
    const api = new MediaApi({
      origin: "http://localhost",
      fetchImpl: (async () =>
        new Response(JSON.stringify(inspectedPayload()), {
          status: 200,
        })) as unknown as typeof fetch,
    });
    const job = await api.getInspectionJob(JOB_ID, token);
    expect(job.state).toBe("inspected");
  });
});

describe("Retry-After integration", () => {
  it("uses a bounded Retry-After from HTTP 429 in the poller", async () => {
    const { pollUntilTerminal } = await import("./poller");
    const { parseInspectionJob } = await import("./contracts");
    const token = generateAccessToken();
    const sleeps: number[] = [];
    let calls = 0;
    const fetchImpl = vi.fn(async () => {
      calls += 1;
      if (calls === 1) {
        return new Response(
          JSON.stringify({ error: { code: "RATE_LIMITED", message: "slow" } }),
          { status: 429, headers: { "Retry-After": "3" } },
        );
      }
      return new Response(JSON.stringify(inspectedPayload()), { status: 200 });
    });
    const api = new MediaApi({
      origin: "http://localhost",
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });
    const ac = new AbortController();
    const job = await pollUntilTerminal({
      read: (signal) => api.getInspectionJob(JOB_ID, token, signal),
      isTerminal: (value) => value.state === "inspected",
      expiresAt: () => new Date(Date.now() + 60_000).toISOString(),
      signal: ac.signal,
      sleep: async (ms) => {
        sleeps.push(ms);
      },
      minIntervalMs: 10,
      maxIntervalMs: 20,
      maxTransientFailures: 5,
    });
    expect(parseInspectionJob(inspectedPayload()).state).toBe("inspected");
    expect(job.state).toBe("inspected");
    expect(sleeps[0]).toBe(3000);
    expect(calls).toBe(2);
  });

  it("ignores Retry-After values above 60 seconds", async () => {
    const { pollUntilTerminal } = await import("./poller");
    const token = generateAccessToken();
    const sleeps: number[] = [];
    let calls = 0;
    const fetchImpl = vi.fn(async () => {
      calls += 1;
      if (calls === 1) {
        return new Response(
          JSON.stringify({ error: { code: "RATE_LIMITED", message: "slow" } }),
          { status: 429, headers: { "Retry-After": "120" } },
        );
      }
      return new Response(JSON.stringify(inspectedPayload()), { status: 200 });
    });
    const api = new MediaApi({
      origin: "http://localhost",
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });
    const ac = new AbortController();
    await pollUntilTerminal({
      read: (signal) => api.getInspectionJob(JOB_ID, token, signal),
      isTerminal: (value) => value.state === "inspected",
      expiresAt: () => new Date(Date.now() + 60_000).toISOString(),
      signal: ac.signal,
      sleep: async (ms) => {
        sleeps.push(ms);
      },
      minIntervalMs: 10,
      maxIntervalMs: 20,
      maxTransientFailures: 5,
    });
    expect(sleeps[0]).toBeGreaterThanOrEqual(10);
    expect(sleeps[0]).toBeLessThanOrEqual(20);
    expect(sleeps[0]).not.toBe(120_000);
  });
});
