import { describe, expect, it, vi } from "vitest";
import { MediaFlowController } from "./controller";
import { MediaApi } from "./api";
import { FlowSession, SESSION_KEY } from "./session";
import { generateAccessToken } from "./credentials";
import { PickerCancelledError, saveArtifactStream } from "./download";
import {
  browserGrantPayload,
  downloadPayload,
  inspectedPayload,
  inspectionPayload,
  DOWNLOAD_ID,
  OPTION_ID,
} from "./fixtures";
import { parseBrowserGrant, parseDownloadJob, parseInspectionJob } from "./contracts";
import { flowErrorFromCode } from "./errors";

function deferred<T = void>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function memoryStore() {
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

function mockApi() {
  return {
    createInspectionJob: vi.fn(async () => parseInspectionJob(inspectionPayload())),
    getInspectionJob: vi.fn(async () => parseInspectionJob(inspectedPayload())),
    createDownloadJob: vi.fn(async () =>
      parseDownloadJob(downloadPayload({ state: "queued", artifactReady: false })),
    ),
    getDownloadJob: vi.fn(async (...args: [string, string, AbortSignal?]) => {
      void args;
      return parseDownloadJob(
        downloadPayload({
          state: "ready",
          artifactReady: true,
          completedAt: "2026-08-13T04:22:27Z",
        }),
      );
    }),
    cancelDownloadJob: vi.fn(),
    createBrowserGrant: vi.fn(async () => parseBrowserGrant(browserGrantPayload())),
  };
}

function makeController(
  api: ReturnType<typeof mockApi>,
  save: ReturnType<typeof vi.fn>,
  store = memoryStore(),
) {
  const token = generateAccessToken();
  const controller = new MediaFlowController({
    api: api as unknown as MediaApi,
    session: new FlowSession(store),
    generateToken: () => token,
    pickerSupported: () => true,
    secureContext: () => true,
    documentHidden: () => false,
    save: save as unknown as typeof saveArtifactStream,
  });
  return { controller, token, store };
}

async function reachReady(controller: MediaFlowController): Promise<void> {
  await controller.submit("https://vk.com/video-1_2");
  controller.selectFormat(OPTION_ID);
  await controller.enqueueDownload();
  await vi.waitFor(() => {
    expect(controller.snapshot().phase).toBe("ready");
  });
}

describe("controller cancellation and lifecycle", () => {
  it("silently refreshes quota after a successful File System Access save", async () => {
    const getFreeQuota = vi.fn(async () => null);
    const api = { ...mockApi(), getFreeQuota };
    const { controller } = makeController(api, vi.fn(async () => undefined));
    await reachReady(controller);
    getFreeQuota.mockClear();

    await controller.saveFile();
    await vi.waitFor(() => expect(getFreeQuota).toHaveBeenCalledOnce());
    expect(controller.snapshot().phase).toBe("completed");
  });

  it("coalesces quota refreshes when foreground events follow native handoff", async () => {
    const quota = deferred<null>();
    const getFreeQuota = vi.fn(async () => null);
    const api = { ...mockApi(), getFreeQuota };
    const { controller } = makeController(api, vi.fn());
    await reachReady(controller);
    await vi.waitFor(() => expect(controller.snapshot().canNativeDownload).toBe(true));
    getFreeQuota.mockClear();
    getFreeQuota.mockImplementation(() => quota.promise);
    expect(controller.onNativeDownloadClick()).toBe(true);

    controller.onForegroundResume();
    controller.onForegroundResume();
    expect(getFreeQuota).toHaveBeenCalledOnce();
    quota.resolve(null);
    await vi.waitFor(() => expect(controller.snapshot().quotaLoading).toBe(false));
  });

  it("returns to ready and stays not busy after picker cancellation", async () => {
    const api = mockApi();
    const save = vi.fn(async () => {
      throw new PickerCancelledError();
    });
    const { controller, store } = makeController(api, save);
    await reachReady(controller);
    await controller.saveFile();
    const snap = controller.snapshot();
    expect(snap.phase).toBe("ready");
    expect(snap.busy).toBe(false);
    expect(snap.canSaveAs).toBe(true);
    expect(snap.canNativeDownload).toBe(true);
    expect(store.map.size).toBe(1);
  });

  it("treats picker cancellation after save begins as a retryable ready state", async () => {
    const api = mockApi();
    const gate = deferred();
    const save = vi.fn(async () => {
      await gate.promise;
      throw new PickerCancelledError();
    });
    const { controller } = makeController(api, save);
    await reachReady(controller);
    const pending = controller.saveFile();
    expect(controller.snapshot().phase).toBe("saving");
    gate.resolve();
    await pending;
    expect(controller.snapshot().phase).toBe("ready");
    expect(controller.snapshot().busy).toBe(false);
  });

  it("maps createWritable cancellation to ready and keeps the recovery record", async () => {
    const api = mockApi();
    const save = vi.fn(async (input: Parameters<typeof saveArtifactStream>[0]) =>
      saveArtifactStream({
        ...input,
        deps: {
          origin: "http://localhost",
          fetchImpl: vi.fn() as unknown as typeof fetch,
          showSaveFilePicker: async () => ({
            createWritable: async () => {
              throw new DOMException("The user aborted a request.", "AbortError");
            },
          }),
        },
      }),
    );
    const { controller, store } = makeController(api, save);
    await reachReady(controller);
    await controller.saveFile();
    expect(controller.snapshot().phase).toBe("ready");
    expect(controller.snapshot().busy).toBe(false);
    expect(controller.snapshot().canSaveAs).toBe(true);
    expect(controller.snapshot().canNativeDownload).toBe(true);
    expect(store.map.size).toBe(1);
  });

  it("returns to READY with fallback actions after a stream AbortError", async () => {
    const api = mockApi();
    const save = vi.fn(async () => {
      throw new DOMException("The user aborted a request.", "AbortError");
    });
    const { controller, store } = makeController(api, save);
    await reachReady(controller);
    await controller.saveFile();
    expect(controller.snapshot().phase).toBe("ready");
    expect(controller.snapshot().busy).toBe(false);
    expect(controller.snapshot().errorText).toContain("скачать его обычным способом");
    expect(controller.snapshot().canSaveAs).toBe(true);
    expect(controller.snapshot().canNativeDownload).toBe(true);
    expect(store.map.size).toBe(1);
  });

  it.each([
    ["network", new TypeError("fetch failed")],
    ["write", new Error("write failed")],
    ["response validation", flowErrorFromCode("CONTRACT")],
  ])(
    "keeps the prepared job and grant after a local %s failure",
    async (_label, localError) => {
      const api = mockApi();
      const save = vi.fn(async () => {
        throw localError;
      });
      const { controller, store } = makeController(api, save);
      await reachReady(controller);
      await controller.saveFile();
      const snap = controller.snapshot();
      expect(snap.phase).toBe("ready");
      expect(snap.canSaveAs).toBe(true);
      expect(snap.canNativeDownload).toBe(true);
      expect(snap.errorText).toContain("повторить попытку");
      expect(api.createDownloadJob).toHaveBeenCalledTimes(1);
      expect(store.map.size).toBe(1);
    },
  );

  it("retries local save against the existing prepared job without new admission", async () => {
    const api = mockApi();
    const save = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("write failed"))
      .mockResolvedValueOnce(undefined);
    const { controller } = makeController(api, save);
    await reachReady(controller);
    const preparedJobId = controller.snapshot().selectedFormat?.formatOptionId;
    await controller.saveFile();
    expect(controller.snapshot().phase).toBe("ready");
    await controller.saveFile();
    expect(controller.snapshot().phase).toBe("completed");
    expect(save).toHaveBeenCalledTimes(2);
    expect(save.mock.calls[0]?.[0].downloadJobId).toBe(save.mock.calls[1]?.[0].downloadJobId);
    expect(save.mock.calls[0]?.[0].expectedArtifactBytes).toBeGreaterThan(0);
    expect(api.createDownloadJob).toHaveBeenCalledTimes(1);
    expect(preparedJobId).toBe(OPTION_ID);
  });

  it("does not present an expired server artifact as locally recoverable", async () => {
    const api = mockApi();
    const save = vi.fn(async () => {
      throw flowErrorFromCode("DOWNLOAD_EXPIRED");
    });
    const { controller, store } = makeController(api, save);
    await reachReady(controller);
    await controller.saveFile();
    expect(controller.snapshot().phase).toBe("expired");
    expect(controller.snapshot().canSaveAs).toBe(false);
    expect(store.map.size).toBe(0);
  });

  it("does not stay saving/busy after abort while streaming", async () => {
    const api = mockApi();
    const gate = deferred();
    const save = vi.fn(async ({ signal }: { signal: AbortSignal }) => {
      await gate.promise;
      if (signal.aborted) {
        throw new DOMException("Aborted", "AbortError");
      }
    });
    const { controller } = makeController(api, save);
    await reachReady(controller);
    const pending = controller.saveFile();
    expect(controller.snapshot().phase).toBe("saving");
    controller.onPageHide();
    gate.resolve();
    await pending;
    await controller.onPageShow(true);
    expect(controller.snapshot().phase).toBe("ready");
    expect(controller.snapshot().busy).toBe(false);
  });

  it("startOver during an in-flight save does not complete later", async () => {
    const api = mockApi();
    const gate = deferred();
    const save = vi.fn(async () => gate.promise);
    const { controller } = makeController(api, save);
    await reachReady(controller);
    const pending = controller.saveFile();
    expect(controller.snapshot().phase).toBe("saving");
    controller.startOver();
    expect(controller.snapshot().phase).toBe("idle");
    expect(controller.snapshot().busy).toBe(false);
    gate.resolve();
    await pending;
    expect(controller.snapshot().phase).toBe("idle");
  });

  it("controller abort during save stays stale and silent and never leaves the next flow busy", async () => {
    const api = mockApi();
    const gate = deferred();
    const save = vi.fn(async ({ signal }: { signal: AbortSignal }) => {
      await gate.promise;
      if (signal.aborted) {
        throw new DOMException("Aborted", "AbortError");
      }
    });
    const { controller } = makeController(api, save);
    await reachReady(controller);
    const pending = controller.saveFile();
    expect(controller.snapshot().phase).toBe("saving");
    controller.startOver();
    expect(controller.snapshot().phase).toBe("idle");
    expect(controller.snapshot().busy).toBe(false);
    await reachReady(controller);
    expect(controller.snapshot().busy).toBe(false);
    gate.resolve();
    await pending;
    expect(controller.snapshot().phase).toBe("ready");
    expect(controller.snapshot().busy).toBe(false);
  });

  it("ignores stale completion after a newer generation", async () => {
    const api = mockApi();
    const first = deferred();
    const save = vi
      .fn()
      .mockImplementationOnce(async () => first.promise)
      .mockImplementationOnce(async () => undefined);
    const { controller } = makeController(api, save);
    await reachReady(controller);
    const stale = controller.saveFile();
    controller.startOver();
    await reachReady(controller);
    await controller.saveFile();
    expect(controller.snapshot().phase).toBe("completed");
    first.resolve();
    await stale;
    expect(controller.snapshot().phase).toBe("completed");
  });

  it("does not reuse a restored download id on a new submit", async () => {
    const api = mockApi();
    const inspectGate = deferred<ReturnType<typeof parseInspectionJob>>();
    api.getInspectionJob.mockImplementationOnce(async () => inspectGate.promise);
    const store = memoryStore();
    const token = generateAccessToken();
    store.setItem(
      SESSION_KEY,
      JSON.stringify({
        v: 2,
        token,
        mediaJobId: parseInspectionJob(inspectionPayload()).id,
        downloadJobId: DOWNLOAD_ID,
        formatOptionId: OPTION_ID,
        phase: "ready",
        expiresAt: "2099-01-01T00:00:00Z",
      }),
    );
    const { controller } = makeController(
      api,
      vi.fn(async () => undefined),
      store,
    );
    const restoring = controller.restore();
    controller.startOver();
    api.getInspectionJob.mockImplementation(async () =>
      parseInspectionJob(inspectedPayload()),
    );
    await controller.submit("https://vk.com/video-1_2");
    inspectGate.resolve(parseInspectionJob(inspectedPayload()));
    await restoring;
    expect(api.getDownloadJob).not.toHaveBeenCalled();
    expect(controller.snapshot().phase).toBe("inspected");
  });

  it("session write failures do not fail a successful inspection", async () => {
    const api = mockApi();
    const controller = new MediaFlowController({
      api: api as unknown as MediaApi,
      session: new FlowSession({
        getItem: () => null,
        setItem: () => {
          throw new Error("quota");
        },
        removeItem: () => {
          throw new Error("blocked");
        },
      }),
      generateToken: generateAccessToken,
      pickerSupported: () => true,
      documentHidden: () => false,
    });
    await controller.submit("https://vk.com/video-1_2");
    expect(controller.snapshot().phase).toBe("inspected");
    expect(controller.snapshot().formats.length).toBeGreaterThan(0);
  });

  it("BFCache restore does not remain in an aborted saving busy state", async () => {
    const api = mockApi();
    const gate = deferred();
    const save = vi.fn(async () => gate.promise);
    const { controller } = makeController(api, save);
    await reachReady(controller);
    const pending = controller.saveFile();
    expect(controller.snapshot().phase).toBe("saving");
    controller.onPageHide();
    const restored = controller.onPageShow(true);
    gate.resolve();
    await pending;
    await restored;
    expect(controller.snapshot().phase).toBe("ready");
    expect(controller.snapshot().busy).toBe(false);
  });

  it("ignores incoherent poll payloads and keeps the current generation", async () => {
    const api = mockApi();
    let reads = 0;
    api.getDownloadJob.mockImplementation(async () => {
      reads += 1;
      if (reads === 1) {
        throw flowErrorFromCode("CONTRACT");
      }
      return parseDownloadJob(
        downloadPayload({
          state: "ready",
          artifactReady: true,
          completedAt: "2026-08-13T04:22:27Z",
        }),
      );
    });
    const save = vi.fn();
    const { controller } = makeController(api, save);
    await controller.submit("https://vk.com/video-1_2");
    controller.selectFormat(OPTION_ID);
    await controller.enqueueDownload();
    expect(controller.snapshot().phase).toBe("ready");
    expect(controller.snapshot().errorText).toBeNull();
    expect(reads).toBeGreaterThanOrEqual(2);
  });

  it("Cancel task calls the server API while Start over does not", async () => {
    const api = mockApi();
    api.getDownloadJob.mockImplementation(async (...args: [string, string, AbortSignal?]) => {
      const signal = args[2];
      await new Promise<void>((_resolve, reject) => {
        if (signal?.aborted) {
          reject(new DOMException("Aborted", "AbortError"));
          return;
        }
        signal?.addEventListener("abort", () => {
          reject(new DOMException("Aborted", "AbortError"));
        });
      });
      throw new DOMException("Aborted", "AbortError");
    });
    api.cancelDownloadJob.mockImplementation(async () =>
      parseDownloadJob(
        downloadPayload({
          state: "cancelled",
          completedAt: "2026-08-13T04:22:27Z",
        }),
      ),
    );
    const { controller } = makeController(api, vi.fn(async () => undefined));
    await controller.submit("https://vk.com/video-1_2");
    const pending = controller.enqueueDownload();
    await Promise.resolve();
    await Promise.resolve();
    expect(controller.snapshot().canCancelTask).toBe(true);
    await controller.cancelTask();
    await pending;
    expect(api.cancelDownloadJob).toHaveBeenCalledOnce();
    expect(controller.snapshot().phase).toBe("cancelled");

    const again = mockApi();
    const { controller: local } = makeController(again, vi.fn(async () => undefined));
    await local.submit("https://vk.com/video-1_2");
    local.startOver();
    expect(again.cancelDownloadJob).not.toHaveBeenCalled();
    expect(local.snapshot().phase).toBe("idle");
  });

  it("quota bootstrap still applies when the controller remains open", async () => {
    const onChange = vi.fn();
    const api = {
      ...mockApi(),
      getFreeQuota: vi.fn(async () => ({
        tier: "free" as const,
        downloadLimit: 3,
        downloadsUsed: 1,
        downloadsReserved: 0,
        downloadsRemaining: 2,
        resetAt: null,
      })),
    };
    const controller = new MediaFlowController({
      api: api as unknown as MediaApi,
      session: new FlowSession(memoryStore()),
      generateToken: generateAccessToken,
      pickerSupported: () => true,
      secureContext: () => true,
      documentHidden: () => false,
      onChange,
    });
    await controller.initializeQuota();
    expect(controller.snapshot().freeQuota?.downloadsRemaining).toBe(2);
    expect(onChange).toHaveBeenCalled();
  });

  it("stale quota completion does not emit after disconnect", async () => {
    const onChange = vi.fn();
    const gate = deferred<{
      tier: "free";
      downloadLimit: number;
      downloadsUsed: number;
      downloadsReserved: number;
      downloadsRemaining: number;
      resetAt: null;
    }>();
    const api = {
      ...mockApi(),
      getFreeQuota: vi.fn(async () => gate.promise),
    };
    const controller = new MediaFlowController({
      api: api as unknown as MediaApi,
      session: new FlowSession(memoryStore()),
      generateToken: generateAccessToken,
      pickerSupported: () => true,
      secureContext: () => true,
      documentHidden: () => false,
      onChange,
    });
    const pending = controller.initializeQuota();
    onChange.mockClear();
    controller.disconnect();
    gate.resolve({
      tier: "free",
      downloadLimit: 3,
      downloadsUsed: 1,
      downloadsReserved: 0,
      downloadsRemaining: 2,
      resetAt: null,
    });
    await pending;
    expect(onChange).not.toHaveBeenCalled();
    expect(controller.snapshot().freeQuota).toBeNull();
  });
});
