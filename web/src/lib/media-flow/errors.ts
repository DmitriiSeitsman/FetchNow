export class FlowError extends Error {
  readonly code: string;
  readonly userMessage: string;
  readonly retryable: boolean;
  readonly retryAfterMs: number | null;

  constructor(
    code: string,
    userMessage: string,
    retryable = false,
    retryAfterMs: number | null = null,
  ) {
    super(userMessage);
    this.name = "FlowError";
    this.code = code;
    this.userMessage = userMessage;
    this.retryable = retryable;
    this.retryAfterMs = retryAfterMs;
  }
}

export function isAbortError(err: unknown): boolean {
  return (
    (err instanceof DOMException && err.name === "AbortError") ||
    (err instanceof Error && err.name === "AbortError")
  );
}

const GENERIC = "Something went wrong. Please try again in a moment, or start over.";

const MESSAGES: Record<string, { text: string; retryable: boolean }> = {
  INVALID_URL: { text: "That link could not be accepted.", retryable: false },
  UNSUPPORTED_SCHEME: {
    text: "Only http and https links are allowed.",
    retryable: false,
  },
  UNSUPPORTED_PROVIDER: {
    text: "This site is not supported yet. Try a public VK, Rutube, or Yandex Preview link.",
    retryable: false,
  },
  BLOCKED_DESTINATION: {
    text: "That destination is not allowed.",
    retryable: false,
  },
  WRAPPER_UNSUPPORTED: {
    text: "This kind of wrapped link is not supported.",
    retryable: false,
  },
  WRAPPER_UNRESOLVED: {
    text: "The preview page could not be resolved to a supported video.",
    retryable: false,
  },
  RESOLVED_PROVIDER_UNSUPPORTED: {
    text: "The resolved video host is not supported.",
    retryable: false,
  },
  RESOLUTION_LOOP: {
    text: "The preview page could not be resolved.",
    retryable: false,
  },
  RESOLUTION_LIMIT_EXCEEDED: {
    text: "The preview page could not be resolved.",
    retryable: false,
  },
  UNSAFE_RESOLUTION_TARGET: {
    text: "That destination is not allowed.",
    retryable: false,
  },
  MEDIA_INSPECTION_FAILED: {
    text: "Media information could not be read from this link.",
    retryable: false,
  },
  CAPACITY_UNAVAILABLE: {
    text: "The service is temporarily at capacity. Please try again later.",
    retryable: true,
  },
  RATE_LIMITED: {
    text: "Too many requests. Please wait a moment and try again.",
    retryable: true,
  },
  JOB_NOT_FOUND: { text: GENERIC, retryable: false },
  JOB_EXPIRED: {
    text: "This check expired. Start over with the same link.",
    retryable: false,
  },
  INVALID_ACCESS_TOKEN: { text: GENERIC, retryable: false },
  IDEMPOTENCY_CONFLICT: { text: GENERIC, retryable: false },
  DOWNLOADS_DISABLED: {
    text: "Downloads are not available right now.",
    retryable: true,
  },
  DELIVERY_DISABLED: {
    text: "Saving files is not available right now.",
    retryable: true,
  },
  DOWNLOAD_JOB_NOT_FOUND: { text: GENERIC, retryable: false },
  PARENT_JOB_NOT_READY: {
    text: "Media information is not ready yet. Please wait.",
    retryable: true,
  },
  FORMAT_NOT_FOUND: {
    text: "That quality is no longer available. Start over to refresh options.",
    retryable: false,
  },
  FORMAT_NOT_ELIGIBLE: {
    text: "That quality is not available in the current free download mode.",
    retryable: false,
  },
  FORMAT_UNAVAILABLE: {
    text: "That quality is no longer available. Start over to refresh options.",
    retryable: false,
  },
  DOWNLOAD_TIMEOUT: {
    text: "Preparing the file took too long. Please try again later.",
    retryable: true,
  },
  DOWNLOAD_TOO_LARGE: {
    text: "This file is larger than the free download limit.",
    retryable: false,
  },
  DOWNLOAD_TOOL_FAILED: {
    text: "The file could not be prepared. Please try again later.",
    retryable: true,
  },
  DOWNLOAD_INVALID_OUTPUT: {
    text: "The prepared file could not be used. Please start over.",
    retryable: false,
  },
  DOWNLOAD_STORAGE_UNAVAILABLE: {
    text: "Storage is temporarily unavailable. Please try again later.",
    retryable: true,
  },
  DOWNLOAD_EXPIRED: {
    text: "The prepared file expired. Start over to prepare it again.",
    retryable: false,
  },
  DOWNLOAD_NOT_READY: {
    text: "The file is not ready yet. Please wait.",
    retryable: true,
  },
  FILE_TOO_LARGE: {
    text: "This file is larger than the free download limit.",
    retryable: false,
  },
  DURATION_TOO_LONG: {
    text: "This media is longer than the allowed limit.",
    retryable: false,
  },
  INTERNAL_ERROR: { text: GENERIC, retryable: true },
  SOURCE_UNAVAILABLE: {
    text: "The source could not be reached. Please try again later.",
    retryable: true,
  },
  SOURCE_TIMEOUT: {
    text: "The source timed out. Please try again later.",
    retryable: true,
  },
  NETWORK_ERROR: {
    text: "The network request failed. Check your connection and try again.",
    retryable: true,
  },
  BROWSER_UNSUPPORTED: {
    text: "Saving requires a browser with the File System Access API (current Chromium desktop). The prepared file stays available until it expires.",
    retryable: false,
  },
  SAVE_FAILED: {
    text: "The file could not be saved completely. It was not marked as finished.",
    retryable: false,
  },
  CONTRACT: { text: GENERIC, retryable: false },
};

export function userMessageForCode(code: string | null | undefined): {
  text: string;
  retryable: boolean;
} {
  if (!code || typeof code !== "string") {
    return { text: GENERIC, retryable: false };
  }
  return MESSAGES[code] ?? { text: GENERIC, retryable: false };
}

export function flowErrorFromCode(
  code: string | null | undefined,
  fallback = "CONTRACT",
  retryAfterMs: number | null = null,
): FlowError {
  const resolved = code && MESSAGES[code] ? code : fallback;
  const mapped = userMessageForCode(resolved);
  return new FlowError(resolved, mapped.text, mapped.retryable, retryAfterMs);
}
