import {
  parseApiError,
  parseDownloadJob,
  parseInspectionJob,
  type DownloadJob,
  type InspectionJob,
} from "./contracts";
import { FlowError, flowErrorFromCode } from "./errors";
import { parseRetryAfterMs } from "./poller";

const JSON_HEADERS = {
  Accept: "application/json",
  "Content-Type": "application/json",
};

const MAX_JSON_BYTES = 1_000_000;

export type ApiClientOptions = {
  origin?: string;
  fetchImpl?: typeof fetch;
  now?: () => number;
};

function resolveOrigin(explicit?: string): string {
  if (explicit) {
    return new URL(explicit).origin;
  }
  if (typeof globalThis.location?.origin === "string") {
    return globalThis.location.origin;
  }
  throw new FlowError(
    "INTERNAL_ERROR",
    "Something went wrong. Please try again in a moment, or start over.",
  );
}

export function sameOriginApiUrl(path: string, origin?: string): string {
  if (!path.startsWith("/api/")) {
    throw new FlowError(
      "CONTRACT",
      "Something went wrong. Please try again in a moment, or start over.",
    );
  }
  if (path.includes("?") || path.includes("#")) {
    throw new FlowError(
      "CONTRACT",
      "Something went wrong. Please try again in a moment, or start over.",
    );
  }
  const base = resolveOrigin(origin);
  const url = new URL(path, `${base}/`);
  if (url.origin !== base) {
    throw new FlowError(
      "CONTRACT",
      "Something went wrong. Please try again in a moment, or start over.",
    );
  }
  if (url.search || url.hash) {
    throw new FlowError(
      "CONTRACT",
      "Something went wrong. Please try again in a moment, or start over.",
    );
  }
  return url.href;
}

export async function readBoundedUtf8(
  response: Response,
  maxBytes: number,
): Promise<string> {
  if (!response.body) {
    return "";
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      total += value.byteLength;
      if (total > maxBytes) {
        try {
          await reader.cancel();
        } catch {
          /* preserve contract error */
        }
        throw new FlowError(
          "CONTRACT",
          "Something went wrong. Please try again in a moment, or start over.",
        );
      }
      chunks.push(value);
    }
  } finally {
    try {
      reader.releaseLock();
    } catch {
      /* already released */
    }
  }
  let size = 0;
  for (const chunk of chunks) {
    size += chunk.byteLength;
  }
  const merged = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    merged.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder("utf-8", { fatal: false }).decode(merged);
}

function bearerHeaders(token: string, extra?: Record<string, string>): Headers {
  const headers = new Headers(extra);
  headers.set("Authorization", `Bearer ${token}`);
  return headers;
}

async function readJson(response: Response): Promise<unknown> {
  const text = await readBoundedUtf8(response, MAX_JSON_BYTES);
  if (!text) {
    return null;
  }
  try {
    return JSON.parse(text) as unknown;
  } catch {
    throw new FlowError(
      "CONTRACT",
      "Something went wrong. Please try again in a moment, or start over.",
    );
  }
}

function throwHttpError(status: number, body: unknown, headers: Headers): never {
  const retryAfter =
    status === 429 || status >= 500
      ? parseRetryAfterMs(headers.get("Retry-After"))
      : null;
  let err: FlowError;
  try {
    const parsed = parseApiError(body);
    err = flowErrorFromCode(parsed.error.code);
  } catch (caught) {
    if (caught instanceof FlowError && caught.code !== "CONTRACT") {
      err = caught;
    } else if (status === 429) {
      err = flowErrorFromCode("RATE_LIMITED");
    } else if (status >= 500) {
      err = flowErrorFromCode("INTERNAL_ERROR");
    } else {
      err = flowErrorFromCode("CONTRACT");
    }
  }
  if (err.retryable && retryAfter !== null) {
    throw new FlowError(err.code, err.userMessage, true, retryAfter);
  }
  throw err;
}

export class MediaApi {
  private readonly origin: string;
  private readonly fetchImpl: typeof fetch;

  constructor(options: ApiClientOptions = {}) {
    this.origin = resolveOrigin(options.origin);
    this.fetchImpl = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
  }

  private async requestJson(
    path: string,
    init: RequestInit,
  ): Promise<{ status: number; body: unknown; headers: Headers }> {
    const url = sameOriginApiUrl(path, this.origin);
    let response: Response;
    try {
      response = await this.fetchImpl(url, {
        ...init,
        credentials: "same-origin",
        redirect: "error",
        cache: "no-store",
      });
    } catch (err) {
      if (err instanceof FlowError) {
        throw err;
      }
      if (init.signal?.aborted) {
        throw err;
      }
      throw new FlowError(
        "NETWORK_ERROR",
        "The network request failed. Check your connection and try again.",
        true,
      );
    }
    const body = await readJson(response);
    return { status: response.status, body, headers: response.headers };
  }

  async createInspectionJob(
    url: string,
    token: string,
    signal?: AbortSignal,
  ): Promise<InspectionJob> {
    const { status, body, headers } = await this.requestJson("/api/v1/media/jobs", {
      method: "POST",
      headers: bearerHeaders(token, JSON_HEADERS),
      body: JSON.stringify({ url }),
      signal,
    });
    if (status !== 202) {
      throwHttpError(status, body, headers);
    }
    return parseInspectionJob(body);
  }

  async getInspectionJob(
    jobId: string,
    token: string,
    signal?: AbortSignal,
  ): Promise<InspectionJob> {
    const { status, body, headers } = await this.requestJson(
      `/api/v1/media/jobs/${jobId}`,
      {
        method: "GET",
        headers: bearerHeaders(token, { Accept: "application/json" }),
        signal,
      },
    );
    if (status !== 200) {
      throwHttpError(status, body, headers);
    }
    return parseInspectionJob(body);
  }

  async createDownloadJob(
    mediaJobId: string,
    formatOptionId: string,
    token: string,
    signal?: AbortSignal,
  ): Promise<DownloadJob> {
    const { status, body, headers } = await this.requestJson(
      `/api/v1/media/jobs/${mediaJobId}/downloads`,
      {
        method: "POST",
        headers: bearerHeaders(token, JSON_HEADERS),
        body: JSON.stringify({ formatOptionId }),
        signal,
      },
    );
    if (status !== 202) {
      throwHttpError(status, body, headers);
    }
    return parseDownloadJob(body);
  }

  async getDownloadJob(
    downloadJobId: string,
    token: string,
    signal?: AbortSignal,
  ): Promise<DownloadJob> {
    const { status, body, headers } = await this.requestJson(
      `/api/v1/media/download-jobs/${downloadJobId}`,
      {
        method: "GET",
        headers: bearerHeaders(token, { Accept: "application/json" }),
        signal,
      },
    );
    if (status !== 200) {
      throwHttpError(status, body, headers);
    }
    return parseDownloadJob(body);
  }

  contentUrl(downloadJobId: string): string {
    return sameOriginApiUrl(
      `/api/v1/media/download-jobs/${downloadJobId}/content`,
      this.origin,
    );
  }
}
