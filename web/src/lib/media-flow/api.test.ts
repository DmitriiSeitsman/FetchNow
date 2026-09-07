import { describe, expect, it, vi } from "vitest";
import { MediaApi, sameOriginApiUrl } from "./api";
import { generateAccessToken } from "./credentials";
import { inspectedPayload, inspectionPayload, JOB_ID } from "./fixtures";

describe("api client", () => {
  it("uses same-origin no-store contracts for Premium and TEST checkout", async () => {
    const orderId = "11111111-2222-4333-8444-555555555555";
    const fetchImpl = vi.fn(async (input: string | URL | Request) => {
      const path = new URL(String(input)).pathname;
      if (path.endsWith("/premium/status")) {
        return new Response(JSON.stringify({ active: false }), { status: 200 });
      }
      if (path.endsWith("/payments/config")) {
        return new Response(JSON.stringify({ testCheckoutAvailable: true }), { status: 200 });
      }
      if (path.endsWith(`/payments/orders/${orderId}`)) {
        return new Response(JSON.stringify({
          status: "PENDING",
          productCode: "premium_24h",
          amountMinor: 100,
          currency: "RUB",
          createdAt: "2026-09-07T12:00:00Z",
          paidAt: null,
        }), { status: 200 });
      }
      return new Response(JSON.stringify({
        orderId,
        status: "PENDING",
        paymentForm: {
          action: "https://auth.robokassa.ru/Merchant/Index.aspx",
          method: "POST",
          fields: {
            MerchantLogin: "fetchnow",
            OutSum: "1.00",
            InvId: "42",
            Description: "Доступ FetchNow на 24 часа",
            SignatureValue: "a".repeat(64),
            IsTest: "1",
            Receipt: "%7B%7D",
            Culture: "ru",
          },
        },
      }), { status: 201 });
    });
    const api = new MediaApi({
      origin: "https://fetchnow.online",
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });
    await api.getPremiumStatus();
    await api.getPaymentConfig();
    await api.createPaymentOrder("A".repeat(32));
    await api.getPaymentOrder(orderId);
    const calls = fetchImpl.mock.calls as unknown as [string, RequestInit][];
    expect(calls.map(([url]) => new URL(url).origin))
      .toEqual(Array(4).fill("https://fetchnow.online"));
    expect(calls[2]?.[1].body).toBe(JSON.stringify({ productCode: "premium_24h" }));
    expect((calls[2]?.[1].headers as Record<string, string>)["Idempotency-Key"])
      .toBe("A".repeat(32));
    expect(calls[2]?.[1].body).not.toMatch(/amount|duration|signature|receipt/i);
    expect(calls.every(([, init]) => init.cache === "no-store")).toBe(true);
  });

  it("bootstraps the anonymous quota identity with same-origin credentials", async () => {
    const fetchImpl = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            tier: "free",
            downloadLimit: 3,
            downloadsUsed: 0,
            downloadsReserved: 0,
            downloadsRemaining: 3,
            resetAt: null,
          }),
          { status: 200 },
        ),
    );
    const api = new MediaApi({
      origin: "http://localhost",
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });

    await expect(api.getFreeQuota()).resolves.toMatchObject({
      tier: "free",
      downloadsRemaining: 3,
    });
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost/api/v1/media/quota");
    expect(init.credentials).toBe("same-origin");
    expect(init.method).toBe("GET");
  });

  it("treats the fail-closed quota flag response as not yet active", async () => {
    const api = new MediaApi({
      origin: "http://localhost",
      fetchImpl: (async () =>
        new Response(
          JSON.stringify({
            error: {
              code: "FREE_QUOTA_DISABLED",
              message: "Free download quota is not active.",
            },
          }),
          { status: 503 },
        )) as unknown as typeof fetch,
    });
    await expect(api.getFreeQuota()).resolves.toBeNull();
  });

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
