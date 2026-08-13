import { describe, expect, it } from "vitest";
import { FlowError } from "./errors";
import { parseRetryAfterMs, pollUntilTerminal } from "./poller";

describe("poller", () => {
  it("honors bounded Retry-After and ignores unbounded values", () => {
    expect(parseRetryAfterMs("3")).toBe(3000);
    expect(parseRetryAfterMs("0")).toBeNull();
    expect(parseRetryAfterMs("120")).toBeNull();
  });

  it("does not overlap reads and stops at terminal", async () => {
    let inflight = 0;
    let max = 0;
    let n = 0;
    const ac = new AbortController();
    const value = await pollUntilTerminal({
      read: async () => {
        inflight += 1;
        max = Math.max(max, inflight);
        n += 1;
        inflight -= 1;
        return n;
      },
      isTerminal: (v) => v >= 3,
      expiresAt: () => new Date(Date.now() + 60_000).toISOString(),
      signal: ac.signal,
      sleep: async () => undefined,
      minIntervalMs: 1,
      maxIntervalMs: 2,
    });
    expect(value).toBe(3);
    expect(max).toBe(1);
  });

  it("cancels timers on abort", async () => {
    const ac = new AbortController();
    const run = pollUntilTerminal({
      read: async () => 0,
      isTerminal: () => false,
      expiresAt: () => new Date(Date.now() + 60_000).toISOString(),
      signal: ac.signal,
      sleep: async (_ms, signal) => {
        if (signal.aborted) {
          throw new DOMException("Aborted", "AbortError");
        }
        await new Promise<void>((_resolve, reject) => {
          const onAbort = () => reject(new DOMException("Aborted", "AbortError"));
          signal.addEventListener("abort", onAbort, { once: true });
        });
      },
    });
    queueMicrotask(() => ac.abort());
    await expect(run).rejects.toMatchObject({ name: "AbortError" });
  });

  it("stops at expiry without unbounded retry", async () => {
    const ac = new AbortController();
    await expect(
      pollUntilTerminal({
        read: async () => 1,
        isTerminal: () => false,
        expiresAt: () => new Date(Date.now() - 1000).toISOString(),
        signal: ac.signal,
      }),
    ).rejects.toMatchObject({ code: "JOB_EXPIRED" });
  });

  it("pauses while hidden then revalidates when visible", async () => {
    const ac = new AbortController();
    let hidden = true;
    let reads = 0;
    const value = await pollUntilTerminal({
      read: async () => {
        reads += 1;
        if (reads === 1) {
          hidden = false;
          return 1;
        }
        return 2;
      },
      isTerminal: (v) => v >= 2,
      expiresAt: () => new Date(Date.now() + 60_000).toISOString(),
      signal: ac.signal,
      hidden: () => hidden,
      sleep: async () => undefined,
      minIntervalMs: 1,
      maxIntervalMs: 2,
      hiddenIntervalMs: 5,
    });
    expect(value).toBe(2);
    expect(reads).toBe(2);
  });

  it("keeps backoff between the configured min and max", async () => {
    const sleeps: number[] = [];
    const ac = new AbortController();
    let n = 0;
    await pollUntilTerminal({
      read: async () => {
        n += 1;
        return n;
      },
      isTerminal: (v) => v >= 4,
      expiresAt: () => new Date(Date.now() + 60_000).toISOString(),
      signal: ac.signal,
      sleep: async (ms) => {
        sleeps.push(ms);
      },
      minIntervalMs: 10,
      maxIntervalMs: 40,
    });
    expect(sleeps.length).toBeGreaterThan(0);
    expect(sleeps.every((ms) => ms >= 10 && ms <= 40)).toBe(true);
  });

  it("retries a bounded number of transient errors then stops", async () => {
    const ac = new AbortController();
    let reads = 0;
    await expect(
      pollUntilTerminal({
        read: async () => {
          reads += 1;
          throw new FlowError("INTERNAL_ERROR", "x", true);
        },
        isTerminal: () => false,
        expiresAt: () => new Date(Date.now() + 60_000).toISOString(),
        signal: ac.signal,
        sleep: async () => undefined,
        maxTransientFailures: 2,
      }),
    ).rejects.toBeTruthy();
    expect(reads).toBeLessThanOrEqual(3);
  });
});
