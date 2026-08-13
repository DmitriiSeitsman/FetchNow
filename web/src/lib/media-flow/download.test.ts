import { describe, expect, it, vi } from "vitest";
import { FlowError } from "./errors";
import {
  PickerCancelledError,
  saveArtifactStream,
  suggestedFilename,
  fileSystemAccessSupported,
} from "./download";
import { DOWNLOAD_ID } from "./fixtures";

function chunkStream(chunks: Uint8Array[]): ReadableStream<Uint8Array> {
  let i = 0;
  return new ReadableStream({
    pull(controller) {
      if (i >= chunks.length) {
        controller.close();
        return;
      }
      controller.enqueue(chunks[i]);
      i += 1;
    },
  });
}

describe("download streaming", () => {
  it("adds Authorization only to a same-origin content URL and streams bytes", async () => {
    const blobSpy = vi.spyOn(Response.prototype, "blob");
    const bufferSpy = vi.spyOn(Response.prototype, "arrayBuffer");
    const writes: number[] = [];
    const writable = {
      write: async (data: Uint8Array) => {
        writes.push(data.byteLength);
      },
      close: vi.fn(async () => undefined),
      abort: vi.fn(async () => undefined),
    };
    const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      expect(url).toBe(
        `http://localhost/api/v1/media/download-jobs/${DOWNLOAD_ID}/content`,
      );
      expect(url).not.toContain("?");
      expect(init?.headers).toEqual({
        Authorization: "Bearer tokentokentokentokentokentokentoken12",
      });
      const body = chunkStream([new Uint8Array([1, 2]), new Uint8Array([3])]);
      return new Response(body, {
        status: 200,
        headers: {
          "Content-Type": "video/mp4",
          "Content-Length": "3",
          "Content-Disposition": `attachment; filename="${suggestedFilename(DOWNLOAD_ID, "mp4")}"`,
        },
      });
    });
    await saveArtifactStream({
      downloadJobId: DOWNLOAD_ID,
      token: "tokentokentokentokentokentokentoken12",
      container: "mp4",
      signal: new AbortController().signal,
      deps: {
        origin: "http://localhost",
        fetchImpl: fetchImpl as unknown as typeof fetch,
        showSaveFilePicker: async () => ({
          createWritable: async () => writable,
        }),
      },
    });
    expect(writes).toEqual([2, 1]);
    expect(writable.close).toHaveBeenCalledOnce();
    expect(writable.abort).not.toHaveBeenCalled();
    expect(blobSpy).not.toHaveBeenCalled();
    expect(bufferSpy).not.toHaveBeenCalled();
    blobSpy.mockRestore();
    bufferSpy.mockRestore();
  });

  it("fails on early EOF and excess bytes and aborts the writable", async () => {
    const abort = vi.fn(async () => undefined);
    const make = async (chunks: Uint8Array[], length: string) => {
      await saveArtifactStream({
        downloadJobId: DOWNLOAD_ID,
        token: "tokentokentokentokentokentokentoken12",
        container: "mp4",
        signal: new AbortController().signal,
        deps: {
          origin: "http://localhost",
          showSaveFilePicker: async () => ({
            createWritable: async () => ({
              write: async () => undefined,
              close: async () => undefined,
              abort,
            }),
          }),
          fetchImpl: (async () =>
            new Response(chunkStream(chunks), {
              status: 200,
              headers: {
                "Content-Type": "video/mp4",
                "Content-Length": length,
                "Content-Disposition": `attachment; filename="${suggestedFilename(DOWNLOAD_ID, "mp4")}"`,
              },
            })) as unknown as typeof fetch,
        },
      });
    };
    await expect(make([new Uint8Array([1])], "4")).rejects.toBeInstanceOf(FlowError);
    await expect(make([new Uint8Array([1, 2, 3, 4, 5])], "2")).rejects.toBeInstanceOf(
      FlowError,
    );
    expect(abort).toHaveBeenCalled();
  });

  it("does not fetch when the File System Access API is missing", async () => {
    const fetchImpl = vi.fn();
    await expect(
      saveArtifactStream({
        downloadJobId: DOWNLOAD_ID,
        token: "tokentokentokentokentokentokentoken12",
        container: "mp4",
        signal: new AbortController().signal,
        deps: {
          origin: "http://localhost",
          hasSavePicker: () => false,
          fetchImpl: fetchImpl as unknown as typeof fetch,
        },
      }),
    ).rejects.toMatchObject({ code: "BROWSER_UNSUPPORTED" });
    expect(fetchImpl).not.toHaveBeenCalled();
    expect(fileSystemAccessSupported({ hasSavePicker: () => false })).toBe(false);
  });

  it("does not automatically retry a partial write", async () => {
    const fetchImpl = vi.fn(async () => {
      return new Response(chunkStream([new Uint8Array([1])]), {
        status: 200,
        headers: {
          "Content-Type": "video/mp4",
          "Content-Length": "4",
          "Content-Disposition": `attachment; filename="${suggestedFilename(DOWNLOAD_ID, "mp4")}"`,
        },
      });
    });
    await expect(
      saveArtifactStream({
        downloadJobId: DOWNLOAD_ID,
        token: "tokentokentokentokentokentokentoken12",
        container: "mp4",
        signal: new AbortController().signal,
        deps: {
          origin: "http://localhost",
          showSaveFilePicker: async () => ({
            createWritable: async () => ({
              write: async () => undefined,
              close: async () => undefined,
              abort: async () => undefined,
            }),
          }),
          fetchImpl: fetchImpl as unknown as typeof fetch,
        },
      }),
    ).rejects.toBeInstanceOf(FlowError);
    expect(fetchImpl).toHaveBeenCalledOnce();
  });

  it("keeps the token out of the suggested filename", () => {
    const name = suggestedFilename(DOWNLOAD_ID, "mp4");
    expect(name).toBe(`fetchnow-${DOWNLOAD_ID}.mp4`);
    expect(name).not.toContain("Bearer");
  });
});

function trackedBody(chunks: Uint8Array[], length?: string, headers?: Headers) {
  let i = 0;
  const cancel = vi.fn(async () => undefined);
  const releaseLock = vi.fn(() => undefined);
  const reader = {
    read: async () => {
      if (i >= chunks.length) {
        return { done: true as const, value: undefined };
      }
      const value = chunks[i];
      i += 1;
      return { done: false as const, value };
    },
    cancel,
    releaseLock,
  };
  const bytes = chunks.reduce((n, c) => n + c.byteLength, 0);
  return {
    cancel,
    releaseLock,
    response: {
      status: 200,
      headers: headers ?? contentHeaders(length ?? String(bytes)),
      body: { getReader: () => reader },
    },
  };
}

function contentHeaders(length: string): Headers {
  return new Headers({
    "Content-Type": "video/mp4",
    "Content-Length": length,
    "Content-Disposition": `attachment; filename="${suggestedFilename(DOWNLOAD_ID, "mp4")}"`,
  });
}

describe("download resource lifecycle", () => {
  const token = "tokentokentokentokentokentokentoken12";

  it("releases the reader without cancelling after a clean save", async () => {
    const tracked = trackedBody([new Uint8Array([1, 2, 3])]);
    const abort = vi.fn(async () => undefined);
    const close = vi.fn(async () => undefined);
    await saveArtifactStream({
      downloadJobId: DOWNLOAD_ID,
      token,
      container: "mp4",
      signal: new AbortController().signal,
      deps: {
        origin: "http://localhost",
        fetchImpl: (async () => tracked.response) as unknown as typeof fetch,
        showSaveFilePicker: async () => ({
          createWritable: async () => ({
            write: async () => undefined,
            close,
            abort,
          }),
        }),
      },
    });
    expect(tracked.cancel).not.toHaveBeenCalled();
    expect(tracked.releaseLock).toHaveBeenCalledOnce();
    expect(close).toHaveBeenCalledOnce();
    expect(abort).not.toHaveBeenCalled();
  });

  it("cancels the reader on write failure, short stream, and close failure", async () => {
    const writeFail = trackedBody([new Uint8Array([1, 2])]);
    const writeAbort = vi.fn(async () => undefined);
    await expect(
      saveArtifactStream({
        downloadJobId: DOWNLOAD_ID,
        token,
        container: "mp4",
        signal: new AbortController().signal,
        deps: {
          origin: "http://localhost",
          fetchImpl: (async () => writeFail.response) as unknown as typeof fetch,
          showSaveFilePicker: async () => ({
            createWritable: async () => ({
              write: async () => {
                throw new Error("write failed");
              },
              close: async () => undefined,
              abort: writeAbort,
            }),
          }),
        },
      }),
    ).rejects.toBeTruthy();
    expect(writeFail.cancel).toHaveBeenCalledOnce();
    expect(writeAbort).toHaveBeenCalledOnce();

    const short = trackedBody([new Uint8Array([1])], "4");
    const shortAbort = vi.fn(async () => undefined);
    await expect(
      saveArtifactStream({
        downloadJobId: DOWNLOAD_ID,
        token,
        container: "mp4",
        signal: new AbortController().signal,
        deps: {
          origin: "http://localhost",
          fetchImpl: (async () => short.response) as unknown as typeof fetch,
          showSaveFilePicker: async () => ({
            createWritable: async () => ({
              write: async () => undefined,
              close: async () => undefined,
              abort: shortAbort,
            }),
          }),
        },
      }),
    ).rejects.toMatchObject({ code: "SAVE_FAILED" });
    expect(short.cancel).toHaveBeenCalledOnce();

    const closeFail = trackedBody([new Uint8Array([1, 2])]);
    const closeAbort = vi.fn(async () => undefined);
    await expect(
      saveArtifactStream({
        downloadJobId: DOWNLOAD_ID,
        token,
        container: "mp4",
        signal: new AbortController().signal,
        deps: {
          origin: "http://localhost",
          fetchImpl: (async () => closeFail.response) as unknown as typeof fetch,
          showSaveFilePicker: async () => ({
            createWritable: async () => ({
              write: async () => undefined,
              close: async () => {
                throw new Error("close failed");
              },
              abort: closeAbort,
            }),
          }),
        },
      }),
    ).rejects.toBeTruthy();
    expect(closeFail.cancel).toHaveBeenCalledOnce();
    expect(closeAbort).toHaveBeenCalledOnce();
  });

  it("preserves the primary error if reader.cancel or writable.abort fails", async () => {
    const tracked = trackedBody([new Uint8Array([1, 2])]);
    tracked.cancel.mockImplementation(async () => {
      throw new Error("cancel failed");
    });
    await expect(
      saveArtifactStream({
        downloadJobId: DOWNLOAD_ID,
        token,
        container: "mp4",
        signal: new AbortController().signal,
        deps: {
          origin: "http://localhost",
          fetchImpl: (async () => tracked.response) as unknown as typeof fetch,
          showSaveFilePicker: async () => ({
            createWritable: async () => ({
              write: async () => {
                throw new Error("write failed");
              },
              close: async () => undefined,
              abort: async () => {
                throw new Error("abort failed");
              },
            }),
          }),
        },
      }),
    ).rejects.toMatchObject({ message: "write failed" });
  });

  it("cancels the reader when the stream is aborted and preserves the primary error", async () => {
    const ac = new AbortController();
    let readStarted!: () => void;
    const started = new Promise<void>((resolve) => {
      readStarted = resolve;
    });
    const cancel = vi.fn(async () => {
      throw new Error("cancel failed");
    });
    const releaseLock = vi.fn(() => undefined);
    const abort = vi.fn(async () => {
      throw new Error("abort failed");
    });
    const reader = {
      read: async () => {
        readStarted();
        await new Promise<never>((_, reject) => {
          ac.signal.addEventListener("abort", () => {
            reject(new DOMException("Aborted", "AbortError"));
          });
        });
        return { done: true as const, value: undefined };
      },
      cancel,
      releaseLock,
    };
    const pending = saveArtifactStream({
      downloadJobId: DOWNLOAD_ID,
      token,
      container: "mp4",
      signal: ac.signal,
      deps: {
        origin: "http://localhost",
        fetchImpl: (async () =>
          ({
            status: 200,
            headers: contentHeaders("4"),
            body: { getReader: () => reader },
          }) as unknown as Response) as unknown as typeof fetch,
        showSaveFilePicker: async () => ({
          createWritable: async () => ({
            write: async () => undefined,
            close: async () => undefined,
            abort,
          }),
        }),
      },
    });
    await started;
    ac.abort();
    await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    expect(cancel).toHaveBeenCalledOnce();
    expect(abort).toHaveBeenCalledOnce();
  });

  it("maps picker AbortError to PickerCancelledError without fetching", async () => {
    const fetchImpl = vi.fn();
    await expect(
      saveArtifactStream({
        downloadJobId: DOWNLOAD_ID,
        token,
        container: "mp4",
        signal: new AbortController().signal,
        deps: {
          origin: "http://localhost",
          fetchImpl: fetchImpl as unknown as typeof fetch,
          showSaveFilePicker: async () => {
            throw new DOMException("The user aborted a request.", "AbortError");
          },
        },
      }),
    ).rejects.toBeInstanceOf(PickerCancelledError);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("maps createWritable AbortError to PickerCancelledError without fetching", async () => {
    const fetchImpl = vi.fn();
    await expect(
      saveArtifactStream({
        downloadJobId: DOWNLOAD_ID,
        token,
        container: "mp4",
        signal: new AbortController().signal,
        deps: {
          origin: "http://localhost",
          fetchImpl: fetchImpl as unknown as typeof fetch,
          showSaveFilePicker: async () => ({
            createWritable: async () => {
              throw new DOMException("The user aborted a request.", "AbortError");
            },
          }),
        },
      }),
    ).rejects.toBeInstanceOf(PickerCancelledError);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("does not map reader AbortError to picker cancellation", async () => {
    const abort = vi.fn(async () => undefined);
    const cancel = vi.fn(async () => undefined);
    const releaseLock = vi.fn(() => undefined);
    const close = vi.fn(async () => undefined);
    const reader = {
      read: async () => {
        throw new DOMException("The user aborted a request.", "AbortError");
      },
      cancel,
      releaseLock,
    };
    await expect(
      saveArtifactStream({
        downloadJobId: DOWNLOAD_ID,
        token,
        container: "mp4",
        signal: new AbortController().signal,
        deps: {
          origin: "http://localhost",
          fetchImpl: (async () =>
            ({
              status: 200,
              headers: contentHeaders("4"),
              body: { getReader: () => reader },
            }) as unknown as Response) as unknown as typeof fetch,
          showSaveFilePicker: async () => ({
            createWritable: async () => ({
              write: async () => undefined,
              close,
              abort,
            }),
          }),
        },
      }),
    ).rejects.toMatchObject({ code: "SAVE_FAILED" });
    expect(cancel).toHaveBeenCalledOnce();
    expect(releaseLock).toHaveBeenCalledOnce();
    expect(abort).toHaveBeenCalledOnce();
    expect(close).not.toHaveBeenCalled();
  });

  it("does not map write or close AbortError to picker cancellation", async () => {
    const writeTracked = trackedBody([new Uint8Array([1, 2])]);
    await expect(
      saveArtifactStream({
        downloadJobId: DOWNLOAD_ID,
        token,
        container: "mp4",
        signal: new AbortController().signal,
        deps: {
          origin: "http://localhost",
          fetchImpl: (async () => writeTracked.response) as unknown as typeof fetch,
          showSaveFilePicker: async () => ({
            createWritable: async () => ({
              write: async () => {
                throw new DOMException("The user aborted a request.", "AbortError");
              },
              close: async () => undefined,
              abort: async () => undefined,
            }),
          }),
        },
      }),
    ).rejects.toMatchObject({ code: "SAVE_FAILED" });
    expect(writeTracked.cancel).toHaveBeenCalledOnce();

    const closeTracked = trackedBody([new Uint8Array([1, 2])]);
    await expect(
      saveArtifactStream({
        downloadJobId: DOWNLOAD_ID,
        token,
        container: "mp4",
        signal: new AbortController().signal,
        deps: {
          origin: "http://localhost",
          fetchImpl: (async () => closeTracked.response) as unknown as typeof fetch,
          showSaveFilePicker: async () => ({
            createWritable: async () => ({
              write: async () => undefined,
              close: async () => {
                throw new DOMException("The user aborted a request.", "AbortError");
              },
              abort: async () => undefined,
            }),
          }),
        },
      }),
    ).rejects.toMatchObject({ code: "SAVE_FAILED" });
    expect(closeTracked.cancel).toHaveBeenCalledOnce();
  });
});

describe("pre-stream header rejection cancels the body", () => {
  const token = "tokentokentokentokentokentokentoken12";

  async function rejectPrestream(headers: Headers, cancelFails = false) {
    const tracked = trackedBody([new Uint8Array([1, 2, 3])], "3", headers);
    if (cancelFails) {
      tracked.cancel.mockImplementation(async () => {
        throw new Error("cancel failed");
      });
    }
    const abort = vi.fn(async () => undefined);
    const close = vi.fn(async () => undefined);
    let thrown: unknown;
    try {
      await saveArtifactStream({
        downloadJobId: DOWNLOAD_ID,
        token,
        container: "mp4",
        signal: new AbortController().signal,
        deps: {
          origin: "http://localhost",
          fetchImpl: (async () => tracked.response) as unknown as typeof fetch,
          showSaveFilePicker: async () => ({
            createWritable: async () => ({
              write: async () => undefined,
              close,
              abort,
            }),
          }),
        },
      });
    } catch (err) {
      thrown = err;
    }
    return { tracked, abort, close, thrown };
  }

  it("cancels and releases on invalid Content-Type without closing the writable", async () => {
    const { tracked, abort, close, thrown } = await rejectPrestream(
      new Headers({
        "Content-Type": "text/html",
        "Content-Length": "3",
        "Content-Disposition": `attachment; filename="${suggestedFilename(DOWNLOAD_ID, "mp4")}"`,
      }),
    );
    expect(thrown).toMatchObject({ code: "CONTRACT" });
    expect(tracked.cancel).toHaveBeenCalledOnce();
    expect(tracked.releaseLock).toHaveBeenCalledOnce();
    expect(abort).toHaveBeenCalledOnce();
    expect(close).not.toHaveBeenCalled();
  });

  it("cancels and releases on missing Content-Type", async () => {
    const { tracked, close, thrown } = await rejectPrestream(
      new Headers({
        "Content-Length": "3",
        "Content-Disposition": `attachment; filename="${suggestedFilename(DOWNLOAD_ID, "mp4")}"`,
      }),
    );
    expect(thrown).toMatchObject({ code: "CONTRACT" });
    expect(tracked.cancel).toHaveBeenCalledOnce();
    expect(tracked.releaseLock).toHaveBeenCalledOnce();
    expect(close).not.toHaveBeenCalled();
  });

  it("cancels and releases on invalid Content-Length", async () => {
    const { tracked, close, thrown } = await rejectPrestream(
      new Headers({
        "Content-Type": "video/mp4",
        "Content-Length": "not-a-number",
        "Content-Disposition": `attachment; filename="${suggestedFilename(DOWNLOAD_ID, "mp4")}"`,
      }),
    );
    expect(thrown).toMatchObject({ code: "CONTRACT" });
    expect(tracked.cancel).toHaveBeenCalledOnce();
    expect(tracked.releaseLock).toHaveBeenCalledOnce();
    expect(close).not.toHaveBeenCalled();
  });

  it("cancels and releases on oversized Content-Length", async () => {
    const { tracked, close, thrown } = await rejectPrestream(
      new Headers({
        "Content-Type": "video/mp4",
        "Content-Length": "536870913",
        "Content-Disposition": `attachment; filename="${suggestedFilename(DOWNLOAD_ID, "mp4")}"`,
      }),
    );
    expect(thrown).toMatchObject({ code: "DOWNLOAD_TOO_LARGE" });
    expect(tracked.cancel).toHaveBeenCalledOnce();
    expect(tracked.releaseLock).toHaveBeenCalledOnce();
    expect(close).not.toHaveBeenCalled();
  });

  it("cancels and releases on malformed or mismatched Content-Disposition", async () => {
    const malformed = await rejectPrestream(
      new Headers({
        "Content-Type": "video/mp4",
        "Content-Length": "3",
        "Content-Disposition": "inline; filename=nope.mp4",
      }),
    );
    expect(malformed.thrown).toMatchObject({ code: "CONTRACT" });
    expect(malformed.tracked.cancel).toHaveBeenCalledOnce();
    expect(malformed.tracked.releaseLock).toHaveBeenCalledOnce();
    expect(malformed.close).not.toHaveBeenCalled();

    const mismatched = await rejectPrestream(
      new Headers({
        "Content-Type": "video/mp4",
        "Content-Length": "3",
        "Content-Disposition": `attachment; filename="${suggestedFilename("bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee", "mp4")}"`,
      }),
    );
    expect(mismatched.thrown).toMatchObject({ code: "CONTRACT" });
    expect(mismatched.tracked.cancel).toHaveBeenCalledOnce();
    expect(mismatched.close).not.toHaveBeenCalled();
  });

  it("cancels on unexpected header validation errors and preserves the primary error if cancel fails", async () => {
    const headers = {
      get(name: string) {
        if (name === "Content-Type") {
          throw new Error("header boom");
        }
        return contentHeaders("3").get(name);
      },
    } as unknown as Headers;
    const tracked = trackedBody([new Uint8Array([1, 2, 3])], "3", headers);
    tracked.cancel.mockImplementation(async () => {
      throw new Error("cancel failed");
    });
    const abort = vi.fn(async () => {
      throw new Error("abort failed");
    });
    const close = vi.fn(async () => undefined);
    await expect(
      saveArtifactStream({
        downloadJobId: DOWNLOAD_ID,
        token,
        container: "mp4",
        signal: new AbortController().signal,
        deps: {
          origin: "http://localhost",
          fetchImpl: (async () => tracked.response) as unknown as typeof fetch,
          showSaveFilePicker: async () => ({
            createWritable: async () => ({
              write: async () => undefined,
              close,
              abort,
            }),
          }),
        },
      }),
    ).rejects.toMatchObject({ message: "header boom" });
    expect(tracked.cancel).toHaveBeenCalledOnce();
    expect(tracked.releaseLock).toHaveBeenCalledOnce();
    expect(close).not.toHaveBeenCalled();
  });

  it("preserves the primary contract error when pre-stream cancel fails", async () => {
    const { tracked, thrown } = await rejectPrestream(
      new Headers({
        "Content-Type": "text/plain",
        "Content-Length": "3",
        "Content-Disposition": `attachment; filename="${suggestedFilename(DOWNLOAD_ID, "mp4")}"`,
      }),
      true,
    );
    expect(thrown).toMatchObject({ code: "CONTRACT" });
    expect(tracked.cancel).toHaveBeenCalledOnce();
    expect(tracked.releaseLock).toHaveBeenCalledOnce();
  });
});
