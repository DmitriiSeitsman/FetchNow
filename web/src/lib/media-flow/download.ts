import { readBoundedUtf8, sameOriginApiUrl } from "./api";
import { isUuid } from "./contracts";
import { FlowError, flowErrorFromCode, isAbortError } from "./errors";

export const ALLOWED_CONTENT_TYPES = new Set([
  "video/mp4",
  "video/webm",
  "video/x-matroska",
  "audio/mp4",
  "audio/mpeg",
  "audio/ogg",
]);

const MAX_BYTES = 536_870_912;
const MAX_ERROR_BODY_BYTES = 8192;
const DISPOSITION_RE =
  /^attachment; filename="(fetchnow-[0-9a-fA-F-]{36}\.[a-z0-9]{2,8})"$/;

export class PickerCancelledError extends FlowError {
  constructor() {
    super("PICKER_CANCELLED", "Save was cancelled.", false);
    this.name = "PickerCancelledError";
  }
}

export type WritableFile = {
  write: (data: Uint8Array) => Promise<void>;
  close: () => Promise<void>;
  abort?: (reason?: unknown) => Promise<void>;
};

export type SaveFilePicker = (options: {
  suggestedName: string;
}) => Promise<{ createWritable: () => Promise<WritableFile> }>;

export type DownloadDeps = {
  fetchImpl?: typeof fetch;
  origin?: string;
  showSaveFilePicker?: SaveFilePicker | undefined;
  hasSavePicker?: () => boolean;
};

function contentTypeOf(header: string | null): string | null {
  if (!header) {
    return null;
  }
  return header.split(";", 1)[0]?.trim().toLowerCase() ?? null;
}

export function parseContentDispositionFilename(header: string | null): string | null {
  if (!header) {
    return null;
  }
  const match = DISPOSITION_RE.exec(header.trim());
  return match?.[1] ?? null;
}

export function suggestedFilename(downloadJobId: string, container: string): string {
  if (!isUuid(downloadJobId) || !/^[a-z0-9]{2,8}$/.test(container)) {
    throw new FlowError(
      "CONTRACT",
      "Something went wrong. Please try again in a moment, or start over.",
    );
  }
  return `fetchnow-${downloadJobId}.${container}`;
}

export function fileSystemAccessSupported(
  io: Pick<DownloadDeps, "hasSavePicker" | "showSaveFilePicker"> = {},
): boolean {
  if (io.hasSavePicker) {
    return io.hasSavePicker();
  }
  if (io.showSaveFilePicker) {
    return true;
  }
  return (
    typeof globalThis !== "undefined" &&
    "showSaveFilePicker" in globalThis &&
    typeof (globalThis as { showSaveFilePicker?: unknown }).showSaveFilePicker ===
      "function"
  );
}

async function releaseReader(
  reader: ReadableStreamDefaultReader<Uint8Array> | null,
  cancel: boolean,
): Promise<void> {
  if (!reader) {
    return;
  }
  try {
    if (cancel) {
      await reader.cancel();
    }
  } finally {
    try {
      reader.releaseLock();
    } catch {
      /* already released */
    }
  }
}

export async function saveArtifactStream(input: {
  downloadJobId: string;
  token: string;
  container: string;
  signal: AbortSignal;
  deps?: DownloadDeps;
}): Promise<void> {
  if (!fileSystemAccessSupported(input.deps ?? {})) {
    throw flowErrorFromCode("BROWSER_UNSUPPORTED");
  }
  const origin = input.deps?.origin;
  const url = sameOriginApiUrl(
    `/api/v1/media/download-jobs/${input.downloadJobId}/content`,
    origin,
  );
  const name = suggestedFilename(input.downloadJobId, input.container);
  const picker =
    input.deps?.showSaveFilePicker ??
    ((
      globalThis as unknown as { showSaveFilePicker: SaveFilePicker }
    ).showSaveFilePicker.bind(globalThis) as SaveFilePicker);

  let writable: WritableFile | null = null;
  let reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
  let ownedBody: ReadableStream<Uint8Array> | null = null;
  let fetchStarted = false;
  let writableClosed = false;
  let writableAborted = false;
  let readerHandled = false;

  const abortWritableOnce = async (): Promise<void> => {
    if (!writable || writableClosed || writableAborted) {
      return;
    }
    writableAborted = true;
    if (writable.abort) {
      await writable.abort();
    }
  };

  const disposeFailure = async (primary: unknown): Promise<never> => {
    if (!readerHandled) {
      readerHandled = true;
      try {
        if (reader) {
          await releaseReader(reader, true);
        } else if (ownedBody) {
          await ownedBody.cancel();
        }
      } catch {
        /* preserve primary */
      }
    }
    try {
      await abortWritableOnce();
    } catch {
      /* preserve primary */
    }
    throw primary;
  };

  try {
    let handle: { createWritable: () => Promise<WritableFile> };
    try {
      handle = await picker({ suggestedName: name });
      writable = await handle.createWritable();
    } catch (err) {
      if (isAbortError(err) && !input.signal.aborted) {
        throw new PickerCancelledError();
      }
      throw err;
    }

    const fetchImpl = input.deps?.fetchImpl ?? globalThis.fetch.bind(globalThis);
    fetchStarted = true;
    const response = await fetchImpl(url, {
      method: "GET",
      headers: { Authorization: `Bearer ${input.token}` },
      credentials: "same-origin",
      redirect: "error",
      cache: "no-store",
      signal: input.signal,
    });
    if (response.status !== 200) {
      let code: string | undefined;
      try {
        const text = await readBoundedUtf8(response, MAX_ERROR_BODY_BYTES);
        const parsed = JSON.parse(text) as { error?: { code?: string } };
        if (typeof parsed.error?.code === "string") {
          code = parsed.error.code;
        }
      } catch {
        code = undefined;
      }
      throw flowErrorFromCode(code ?? "SAVE_FAILED");
    }
    ownedBody = response.body;
    if (!ownedBody) {
      throw new FlowError(
        "CONTRACT",
        "Something went wrong. Please try again in a moment, or start over.",
      );
    }
    reader = ownedBody.getReader();
    const type = contentTypeOf(response.headers.get("Content-Type"));
    if (!type || !ALLOWED_CONTENT_TYPES.has(type)) {
      throw new FlowError(
        "CONTRACT",
        "Something went wrong. Please try again in a moment, or start over.",
      );
    }
    const lengthHeader = response.headers.get("Content-Length");
    if (!lengthHeader || !/^\d+$/.test(lengthHeader)) {
      throw new FlowError(
        "CONTRACT",
        "Something went wrong. Please try again in a moment, or start over.",
      );
    }
    const expected = Number(lengthHeader);
    if (!Number.isFinite(expected) || expected <= 0 || expected > MAX_BYTES) {
      throw flowErrorFromCode("DOWNLOAD_TOO_LARGE");
    }
    const filename = parseContentDispositionFilename(
      response.headers.get("Content-Disposition"),
    );
    if (filename !== name) {
      throw new FlowError(
        "CONTRACT",
        "Something went wrong. Please try again in a moment, or start over.",
      );
    }
    let written = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      written += value.byteLength;
      if (written > expected) {
        throw new FlowError(
          "SAVE_FAILED",
          "The file could not be saved completely. It was not marked as finished.",
        );
      }
      await writable.write(value);
    }
    if (written !== expected) {
      throw new FlowError(
        "SAVE_FAILED",
        "The file could not be saved completely. It was not marked as finished.",
      );
    }
    await writable.close();
    writableClosed = true;
    readerHandled = true;
    await releaseReader(reader, false);
    writable = null;
  } catch (err) {
    let primary = err;
    if (
      fetchStarted &&
      isAbortError(err) &&
      !input.signal.aborted &&
      !(err instanceof PickerCancelledError)
    ) {
      primary = flowErrorFromCode("SAVE_FAILED");
    }
    await disposeFailure(primary);
  }
}
