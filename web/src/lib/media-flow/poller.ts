import { FlowError, flowErrorFromCode } from "./errors";

export type PollerOptions<T> = {
  read: (signal: AbortSignal) => Promise<T>;
  isTerminal: (value: T) => boolean;
  expiresAt: () => string;
  signal: AbortSignal;
  hidden?: () => boolean;
  onTick?: (value: T) => void;
  now?: () => number;
  sleep?: (ms: number, signal: AbortSignal) => Promise<void>;
  minIntervalMs?: number;
  maxIntervalMs?: number;
  hiddenIntervalMs?: number;
  deadlineMs?: number;
  maxTransientFailures?: number;
  retryAfterHeader?: (value: T) => string | null;
};

const MIN_INTERVAL_MS = 700;
const MAX_INTERVAL_MS = 8_000;
const HIDDEN_INTERVAL_MS = 15_000;
const DEADLINE_MS = 15 * 60_000;
const MAX_TRANSIENT = 5;
const RETRY_AFTER_MAX_S = 60;

export function parseRetryAfterMs(
  header: string | null | undefined,
  now = Date.now(),
): number | null {
  if (!header) {
    return null;
  }
  const trimmed = header.trim();
  if (/^\d+$/.test(trimmed)) {
    const seconds = Number(trimmed);
    if (!Number.isFinite(seconds) || seconds < 1 || seconds > RETRY_AFTER_MAX_S) {
      return null;
    }
    return seconds * 1000;
  }
  const at = Date.parse(trimmed);
  if (!Number.isFinite(at)) {
    return null;
  }
  const delta = at - now;
  if (delta < 1_000 || delta > RETRY_AFTER_MAX_S * 1000) {
    return null;
  }
  return delta;
}

function defaultSleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(signal.reason ?? new DOMException("Aborted", "AbortError"));
      return;
    }
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      reject(signal.reason ?? new DOMException("Aborted", "AbortError"));
    };
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

function backoffMs(attempt: number, min: number, max: number): number {
  const exp = Math.min(max, min * 1.7 ** attempt);
  const jitter = exp * 0.2 * (Math.random() * 2 - 1);
  return Math.max(min, Math.min(max, Math.round(exp + jitter)));
}

export async function pollUntilTerminal<T>(options: PollerOptions<T>): Promise<T> {
  const min = options.minIntervalMs ?? MIN_INTERVAL_MS;
  const max = options.maxIntervalMs ?? MAX_INTERVAL_MS;
  const hiddenInterval = options.hiddenIntervalMs ?? HIDDEN_INTERVAL_MS;
  const deadlineMs = options.deadlineMs ?? DEADLINE_MS;
  const maxTransient = options.maxTransientFailures ?? MAX_TRANSIENT;
  const now = options.now ?? Date.now;
  const sleep = options.sleep ?? defaultSleep;
  const started = now();
  let attempt = 0;
  let transient = 0;
  let inFlight = false;
  let last: T | undefined;

  const waitVisible = async () => {
    if (!options.hidden?.()) {
      return;
    }
    const remaining = deadlineMs - (now() - started);
    if (remaining <= 0) {
      throw new FlowError(
        "SOURCE_TIMEOUT",
        "Preparing the file took too long. Please try again later.",
        true,
      );
    }
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => {
        cleanup();
        reject(
          new FlowError(
            "SOURCE_TIMEOUT",
            "Preparing the file took too long. Please try again later.",
            true,
          ),
        );
      }, remaining);
      const onAbort = () => {
        cleanup();
        reject(options.signal.reason ?? new DOMException("Aborted", "AbortError"));
      };
      const onVis = () => {
        if (!options.hidden?.()) {
          cleanup();
          resolve();
        }
      };
      const cleanup = () => {
        clearTimeout(timer);
        options.signal.removeEventListener("abort", onAbort);
        if (typeof document !== "undefined") {
          document.removeEventListener("visibilitychange", onVis);
        }
      };
      options.signal.addEventListener("abort", onAbort, { once: true });
      if (typeof document !== "undefined") {
        document.addEventListener("visibilitychange", onVis);
      }
    });
  };

  while (!options.signal.aborted) {
    if (now() - started > deadlineMs) {
      throw new FlowError(
        "SOURCE_TIMEOUT",
        "Preparing the file took too long. Please try again later.",
        true,
      );
    }
    const expires = Date.parse(options.expiresAt());
    if (Number.isFinite(expires) && now() >= expires) {
      throw flowErrorFromCode("JOB_EXPIRED");
    }
    if (inFlight) {
      throw new FlowError(
        "INTERNAL_ERROR",
        "Something went wrong. Please try again in a moment, or start over.",
      );
    }
    inFlight = true;
    try {
      last = await options.read(options.signal);
    } catch (err) {
      inFlight = false;
      if (options.signal.aborted) {
        throw err;
      }
      if (
        err instanceof FlowError &&
        (err.retryable || err.code === "CONTRACT") &&
        transient < maxTransient
      ) {
        transient += 1;
        const wait = err.retryAfterMs ?? backoffMs(attempt, min, max);
        await sleep(wait, options.signal);
        attempt += 1;
        continue;
      }
      throw err;
    }
    inFlight = false;
    transient = 0;
    options.onTick?.(last);
    if (options.isTerminal(last)) {
      return last;
    }
    let wait = backoffMs(attempt, min, max);
    attempt += 1;
    const retryAfter = parseRetryAfterMs(
      options.retryAfterHeader?.(last) ?? null,
      now(),
    );
    if (retryAfter !== null) {
      wait = retryAfter;
    }
    if (options.hidden?.()) {
      wait = Math.max(wait, hiddenInterval);
      try {
        await waitVisible();
      } catch (err) {
        if (options.signal.aborted) {
          throw options.signal.reason ?? new DOMException("Aborted", "AbortError");
        }
        throw err;
      }
      if (options.hidden?.()) {
        await sleep(wait, options.signal);
      }
      continue;
    }
    await sleep(wait, options.signal);
  }
  throw options.signal.reason ?? new DOMException("Aborted", "AbortError");
}
