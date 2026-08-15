import { describe, expect, it, vi } from "vitest";
import { FlowError } from "./errors";
import {
  isActiveDownloadProgress,
  jobProgressSemanticKey,
  parseRetryAfterMs,
  pollUntilTerminal,
} from "./poller";

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

  it("retries CONTRACT parse failures without treating them as terminal", async () => {
    const ac = new AbortController();
    let reads = 0;
    const ticks: number[] = [];
    const value = await pollUntilTerminal({
      read: async () => {
        reads += 1;
        if (reads === 1) {
          throw new FlowError("CONTRACT", "x", false);
        }
        return reads;
      },
      isTerminal: (v) => v >= 2,
      expiresAt: () => new Date(Date.now() + 60_000).toISOString(),
      signal: ac.signal,
      sleep: async () => undefined,
      maxTransientFailures: 2,
      onTick: (v) => {
        ticks.push(v);
      },
    });
    expect(value).toBe(2);
    expect(ticks).toEqual([2]);
  });

  it("backs off on unchanged queued responses", async () => {
    const sleeps: number[] = [];
    const ac = new AbortController();
    let n = 0;
    await pollUntilTerminal({
      read: async () => {
        n += 1;
        return {
          state: "queued",
          progressStage: "queued",
          progressPercent: null,
          attempt: 1,
          updatedAt: "2026-01-01T00:00:00Z",
        };
      },
      isTerminal: () => n >= 5,
      expiresAt: () => new Date(Date.now() + 60_000).toISOString(),
      signal: ac.signal,
      sleep: async (ms) => {
        sleeps.push(ms);
      },
      minIntervalMs: 100,
      maxIntervalMs: 8_000,
      semanticKey: jobProgressSemanticKey,
    });
    expect(sleeps.length).toBeGreaterThanOrEqual(3);
    // Unchanged key: attempt grows → later waits trend upward (bounded).
    expect(sleeps[sleeps.length - 1]!).toBeGreaterThanOrEqual(sleeps[0]!);
    expect(Math.max(...sleeps)).toBeLessThanOrEqual(8_000);
  });

  it("resets delay when progressPercent or state changes", async () => {
    const sleeps: number[] = [];
    const ac = new AbortController();
    const sequence = [
      {
        state: "downloading",
        progressStage: "downloading_video",
        progressPercent: 10,
        attempt: 1,
        updatedAt: "t1",
      },
      {
        state: "downloading",
        progressStage: "downloading_video",
        progressPercent: 10,
        attempt: 1,
        updatedAt: "t1",
      },
      {
        state: "downloading",
        progressStage: "downloading_video",
        progressPercent: 40,
        attempt: 1,
        updatedAt: "t2",
      },
      {
        state: "downloading",
        progressStage: "downloading_audio",
        progressPercent: 5,
        attempt: 1,
        updatedAt: "t3",
      },
      {
        state: "ready",
        progressStage: "ready",
        progressPercent: null,
        attempt: 1,
        updatedAt: "t4",
      },
    ];
    let i = 0;
    await pollUntilTerminal({
      read: async () => sequence[Math.min(i++, sequence.length - 1)]!,
      isTerminal: (v) => v.state === "ready",
      expiresAt: () => new Date(Date.now() + 60_000).toISOString(),
      signal: ac.signal,
      sleep: async (ms) => {
        sleeps.push(ms);
      },
      minIntervalMs: 700,
      maxIntervalMs: 8_000,
      semanticKey: jobProgressSemanticKey,
      isActiveProgress: isActiveDownloadProgress,
      activeMaxIntervalMs: 1_000,
    });
    expect(sleeps.every((ms) => ms <= 1_000)).toBe(true);
    expect(sleeps.every((ms) => ms >= 700)).toBe(true);
  });

  it("keeps active download polls at most 1000ms even after many ticks", async () => {
    const sleeps: number[] = [];
    const ac = new AbortController();
    let percent = 0;
    await pollUntilTerminal({
      read: async () => {
        percent += 5;
        return {
          state: "downloading",
          progressStage: "downloading_video",
          progressPercent: Math.min(90, percent),
          attempt: 1,
          updatedAt: `t${percent}`,
        };
      },
      isTerminal: () => percent >= 50,
      expiresAt: () => new Date(Date.now() + 60_000).toISOString(),
      signal: ac.signal,
      sleep: async (ms) => {
        sleeps.push(ms);
      },
      minIntervalMs: 700,
      maxIntervalMs: 8_000,
      semanticKey: jobProgressSemanticKey,
      isActiveProgress: isActiveDownloadProgress,
      activeMaxIntervalMs: 1_000,
    });
    expect(sleeps.length).toBeGreaterThan(5);
    expect(sleeps.every((ms) => ms <= 1_000)).toBe(true);
  });

  it("delivers several onTick samples across 5.5s video and 4.2s audio stages", async () => {
    vi.useFakeTimers();
    const ticks: Array<number | null> = [];
    const ac = new AbortController();
    let clock = 0;
    const videoMs = 5_500;
    const audioMs = 4_200;
    try {
      const run = pollUntilTerminal({
        read: async () => {
          if (clock < videoMs) {
            const pct = Math.min(90, Math.floor((clock / videoMs) * 90));
            return {
              state: "downloading",
              progressStage: "downloading_video",
              progressPercent: pct,
              attempt: 1,
              updatedAt: `v${clock}`,
            };
          }
          if (clock < videoMs + audioMs) {
            const elapsed = clock - videoMs;
            const pct = Math.min(90, Math.floor((elapsed / audioMs) * 90));
            return {
              state: "downloading",
              progressStage: "downloading_audio",
              progressPercent: pct,
              attempt: 1,
              updatedAt: `a${clock}`,
            };
          }
          return {
            state: "ready",
            progressStage: "ready",
            progressPercent: null,
            attempt: 1,
            updatedAt: "done",
          };
        },
        isTerminal: (v) => v.state === "ready",
        expiresAt: () => new Date(Date.now() + 120_000).toISOString(),
        signal: ac.signal,
        now: () => Date.now() + clock,
        sleep: async (ms) => {
          clock += ms;
          await vi.advanceTimersByTimeAsync(ms);
        },
        minIntervalMs: 700,
        maxIntervalMs: 8_000,
        semanticKey: jobProgressSemanticKey,
        isActiveProgress: isActiveDownloadProgress,
        activeMaxIntervalMs: 1_000,
        onTick: (v) => {
          ticks.push(v.progressPercent);
        },
      });
      const value = await run;
      expect(value.state).toBe("ready");
      const videoTicks = ticks.filter((p, idx) => {
        // Rough: early ticks during video stage have non-null percents before audio.
        return p !== null && idx < ticks.length - 1;
      });
      expect(videoTicks.length).toBeGreaterThanOrEqual(4);
      expect(ticks.some((p) => typeof p === "number" && p > 0)).toBe(true);
      // Ready is immediate — last tick is terminal and polling ends (no delay after).
      expect(ticks[ticks.length - 1]).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});
