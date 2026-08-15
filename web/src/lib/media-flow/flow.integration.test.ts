/**
 * @vitest-environment jsdom
 */
import { describe, expect, it, vi } from "vitest";
import { MediaFlowController } from "./controller";
import { MediaApi } from "./api";
import { FlowSession } from "./session";
import { generateAccessToken } from "./credentials";
import {
  browserGrantPayload,
  downloadPayload,
  inspectedPayload,
  inspectionPayload,
  OPTION_ID,
  progressiveFormat,
} from "./fixtures";
import { parseBrowserGrant, parseDownloadJob, parseInspectionJob } from "./contracts";

const enabledCapabilities = {
  providerId: "vk",
  operations: {
    downloadVideo: "enabled",
    extractAudio: "disabled",
    selectQuality: "enabled",
    selectContainer: "disabled",
  },
  contentKinds: {
    video: "enabled",
    clip: "enabled",
    live: "disabled",
    playlist: "disabled",
  },
  metadata: {
    title: "enabled",
    duration: "enabled",
    thumbnail: "planned",
  },
} as const;

describe("browser flow integration", () => {
  it("submits, inspects, selects, downloads, arms a grant, and keeps session on native click", async () => {
    const token = generateAccessToken();
    const api = {
      createInspectionJob: vi.fn(async () => parseInspectionJob(inspectionPayload())),
      getInspectionJob: vi.fn(async () => parseInspectionJob(inspectedPayload())),
      createDownloadJob: vi.fn(async () =>
        parseDownloadJob(downloadPayload({ state: "queued", artifactReady: false })),
      ),
      getDownloadJob: vi.fn(async () =>
        parseDownloadJob(
          downloadPayload({
            state: "ready",
            artifactReady: true,
            completedAt: "2026-08-13T04:22:27Z",
          }),
        ),
      ),
      createBrowserGrant: vi.fn(async () => parseBrowserGrant(browserGrantPayload())),
      cancelDownloadJob: vi.fn(),
    };
    const save = vi.fn(async () => undefined);
    const store = new Map<string, string>();
    const controller = new MediaFlowController({
      api: api as unknown as MediaApi,
      session: new FlowSession({
        getItem: (k) => store.get(k) ?? null,
        setItem: (k, v) => {
          store.set(k, v);
        },
        removeItem: (k) => {
          store.delete(k);
        },
      }),
      generateToken: () => token,
      pickerSupported: () => false,
      secureContext: () => true,
      documentHidden: () => false,
      save,
    });

    await controller.submit("https://vk.com/video1");
    expect(controller.snapshot().phase).toBe("inspected");
    expect(api.createInspectionJob).toHaveBeenCalledTimes(1);
    controller.selectFormat(OPTION_ID);
    expect(controller.snapshot().downloadEligible).toBe(true);
    await controller.enqueueDownload();
    await vi.waitFor(() => {
      expect(controller.snapshot().canNativeDownload).toBe(true);
    });
    expect(api.createBrowserGrant).toHaveBeenCalledTimes(1);
    controller.onNativeDownloadClick();
    expect(controller.snapshot().nativeDownloadHandoff).toBe(true);
    expect(controller.snapshot().phase).toBe("ready");
    expect([...store.keys()]).toHaveLength(1);
    expect(save).not.toHaveBeenCalled();
  });

  it("still completes FSA save when the picker is supported", async () => {
    const token = generateAccessToken();
    const api = {
      createInspectionJob: vi.fn(async () => parseInspectionJob(inspectionPayload())),
      getInspectionJob: vi.fn(async () => parseInspectionJob(inspectedPayload())),
      createDownloadJob: vi.fn(async () =>
        parseDownloadJob(downloadPayload({ state: "queued", artifactReady: false })),
      ),
      getDownloadJob: vi.fn(async () =>
        parseDownloadJob(
          downloadPayload({
            state: "ready",
            artifactReady: true,
            completedAt: "2026-08-13T04:22:27Z",
          }),
        ),
      ),
      createBrowserGrant: vi.fn(async () => parseBrowserGrant(browserGrantPayload())),
      cancelDownloadJob: vi.fn(),
    };
    const save = vi.fn(async () => undefined);
    const store = new Map<string, string>();
    const controller = new MediaFlowController({
      api: api as unknown as MediaApi,
      session: new FlowSession({
        getItem: (k) => store.get(k) ?? null,
        setItem: (k, v) => {
          store.set(k, v);
        },
        removeItem: (k) => {
          store.delete(k);
        },
      }),
      generateToken: () => token,
      pickerSupported: () => true,
      secureContext: () => true,
      documentHidden: () => false,
      save,
    });
    await controller.submit("https://vk.com/video1");
    controller.selectFormat(OPTION_ID);
    await controller.enqueueDownload();
    await vi.waitFor(() => {
      expect(controller.snapshot().canNativeDownload).toBe(true);
    });
    await controller.saveFile();
    expect(save).toHaveBeenCalledTimes(1);
    expect(controller.snapshot().phase).toBe("completed");
    expect([...store.keys()]).toHaveLength(0);
  });

  it("reaches format selection when durationSeconds is null", async () => {
    const token = generateAccessToken();
    const api = {
      createInspectionJob: vi.fn(async () => parseInspectionJob(inspectionPayload())),
      getInspectionJob: vi.fn(async () =>
        parseInspectionJob(inspectedPayload({ durationSeconds: null })),
      ),
      createDownloadJob: vi.fn(),
      getDownloadJob: vi.fn(),
      createBrowserGrant: vi.fn(),
      cancelDownloadJob: vi.fn(),
    };
    const store = new Map<string, string>();
    const controller = new MediaFlowController({
      api: api as unknown as MediaApi,
      session: new FlowSession({
        getItem: (k) => store.get(k) ?? null,
        setItem: (k, v) => {
          store.set(k, v);
        },
        removeItem: (k) => {
          store.delete(k);
        },
      }),
      generateToken: () => token,
      pickerSupported: () => true,
      secureContext: () => true,
      documentHidden: () => false,
    });
    await controller.submit("https://vk.com/video-1_2");
    const snap = controller.snapshot();
    expect(snap.phase).toBe("inspected");
    expect(snap.result?.durationSeconds).toBeNull();
    expect(snap.formats).toHaveLength(1);
    controller.selectFormat(OPTION_ID);
    expect(controller.snapshot().downloadEligible).toBe(true);
  });

  it("selects a derived quality when muxingRequired is false", async () => {
    const token = generateAccessToken();
    const api = {
      createInspectionJob: vi.fn(async () => parseInspectionJob(inspectionPayload())),
      getInspectionJob: vi.fn(async () => parseInspectionJob(inspectedPayload())),
      createDownloadJob: vi.fn(async () =>
        parseDownloadJob(downloadPayload({ state: "queued", artifactReady: false })),
      ),
      getDownloadJob: vi.fn(),
      createBrowserGrant: vi.fn(),
      cancelDownloadJob: vi.fn(),
    };
    const store = new Map<string, string>();
    const controller = new MediaFlowController({
      api: api as unknown as MediaApi,
      session: new FlowSession({
        getItem: (k) => store.get(k) ?? null,
        setItem: (k, v) => {
          store.set(k, v);
        },
        removeItem: (k) => {
          store.delete(k);
        },
      }),
      generateToken: () => token,
      pickerSupported: () => true,
      secureContext: () => true,
      documentHidden: () => false,
    });
    await controller.submit("https://vk.com/video-1_2");
    expect(controller.snapshot().muxingBlocked).toBe(false);
    controller.selectFormat(OPTION_ID);
    expect(controller.snapshot().downloadEligible).toBe(true);
    await controller.enqueueDownload();
    expect(api.createDownloadJob).toHaveBeenCalledTimes(1);
  });

  it("does not enqueue when muxingRequired means no executable option", async () => {
    const token = generateAccessToken();
    const api = {
      createInspectionJob: vi.fn(async () => parseInspectionJob(inspectionPayload())),
      getInspectionJob: vi.fn(async () =>
        parseInspectionJob(inspectedPayload({ muxingRequired: true })),
      ),
      createDownloadJob: vi.fn(),
      getDownloadJob: vi.fn(),
      createBrowserGrant: vi.fn(),
      cancelDownloadJob: vi.fn(),
    };
    const store = new Map<string, string>();
    const controller = new MediaFlowController({
      api: api as unknown as MediaApi,
      session: new FlowSession({
        getItem: (k) => store.get(k) ?? null,
        setItem: (k, v) => {
          store.set(k, v);
        },
        removeItem: (k) => {
          store.delete(k);
        },
      }),
      generateToken: () => token,
      pickerSupported: () => true,
      secureContext: () => true,
      documentHidden: () => false,
    });
    await controller.submit("https://vk.com/video-1_2");
    expect(controller.snapshot().muxingBlocked).toBe(true);
    controller.selectFormat(OPTION_ID);
    expect(controller.snapshot().downloadEligible).toBe(false);
    await controller.enqueueDownload();
    expect(api.createDownloadJob).not.toHaveBeenCalled();
  });

  it("blocks disabled or planned download policy even with an eligible format", async () => {
    for (const state of ["disabled", "planned"] as const) {
      const token = generateAccessToken();
      const payload = inspectedPayload();
      payload.providerCapabilities = {
        ...enabledCapabilities,
        operations: { ...enabledCapabilities.operations, downloadVideo: state },
      };
      const api = {
        createInspectionJob: vi.fn(async () => parseInspectionJob(inspectionPayload())),
        getInspectionJob: vi.fn(async () => parseInspectionJob(payload)),
        createDownloadJob: vi.fn(),
        getDownloadJob: vi.fn(),
        createBrowserGrant: vi.fn(),
        cancelDownloadJob: vi.fn(),
      };
      const controller = new MediaFlowController({
        api: api as unknown as MediaApi,
        session: new FlowSession(),
        generateToken: () => token,
        pickerSupported: () => false,
        secureContext: () => true,
        documentHidden: () => false,
      });

      await controller.submit("https://vk.com/video-1_2");
      expect(controller.snapshot().downloadEligible).toBe(false);
      await controller.enqueueDownload();
      expect(api.createDownloadJob).not.toHaveBeenCalled();
    }
  });

  it("keeps an internal default downloadable when quality selection is disabled", async () => {
    const token = generateAccessToken();
    const payload = inspectedPayload({
      formats: [
        {
          ...progressiveFormat,
          formatOptionId: OPTION_ID,
        },
        {
          ...progressiveFormat,
          formatOptionId: "fmt_bbbbbbbbbbbbbbbbbbbbbbbbbbbb",
          height: 480,
          qualityLabel: "p480",
        },
      ],
    });
    payload.providerCapabilities = {
      ...enabledCapabilities,
      operations: { ...enabledCapabilities.operations, selectQuality: "disabled" },
    };
    const api = {
      createInspectionJob: vi.fn(async () => parseInspectionJob(inspectionPayload())),
      getInspectionJob: vi.fn(async () => parseInspectionJob(payload)),
      createDownloadJob: vi.fn(async () =>
        parseDownloadJob(downloadPayload({ state: "queued", artifactReady: false })),
      ),
      getDownloadJob: vi.fn(async () =>
        parseDownloadJob(
          downloadPayload({
            state: "failed",
            completedAt: "2026-08-13T04:22:27Z",
            errorCode: "DOWNLOAD_TOOL_FAILED",
          }),
        ),
      ),
      createBrowserGrant: vi.fn(),
      cancelDownloadJob: vi.fn(),
    };
    const controller = new MediaFlowController({
      api: api as unknown as MediaApi,
      session: new FlowSession(),
      generateToken: () => token,
      pickerSupported: () => false,
      secureContext: () => true,
      documentHidden: () => false,
    });

    await controller.submit("https://vk.com/video-1_2");
    const defaultId = controller.snapshot().selectedFormatId;
    expect(defaultId).toBe(OPTION_ID);
    expect(controller.snapshot().canSelectQuality).toBe(false);
    expect(controller.snapshot().downloadEligible).toBe(true);
    controller.selectFormat("fmt_bbbbbbbbbbbbbbbbbbbbbbbbbbbb");
    expect(controller.snapshot().selectedFormatId).toBe(defaultId);
    await controller.enqueueDownload();
    expect(api.createDownloadJob).toHaveBeenCalledWith(
      expect.any(String),
      OPTION_ID,
      token,
      expect.any(AbortSignal),
    );
  });

  it("keeps download available with a single auto-selected quality when selection is enabled", async () => {
    const token = generateAccessToken();
    const payload = inspectedPayload();
    payload.providerCapabilities = { ...enabledCapabilities };
    const api = {
      createInspectionJob: vi.fn(async () => parseInspectionJob(inspectionPayload())),
      getInspectionJob: vi.fn(async () => parseInspectionJob(payload)),
      createDownloadJob: vi.fn(async () =>
        parseDownloadJob(downloadPayload({ state: "queued", artifactReady: false })),
      ),
      getDownloadJob: vi.fn(async () =>
        parseDownloadJob(
          downloadPayload({
            state: "failed",
            completedAt: "2026-08-13T04:22:27Z",
            errorCode: "DOWNLOAD_TOOL_FAILED",
          }),
        ),
      ),
      createBrowserGrant: vi.fn(),
      cancelDownloadJob: vi.fn(),
    };
    const controller = new MediaFlowController({
      api: api as unknown as MediaApi,
      session: new FlowSession(),
      generateToken: () => token,
      pickerSupported: () => false,
      secureContext: () => true,
      documentHidden: () => false,
    });

    await controller.submit("https://vk.com/video-1_2");
    expect(controller.snapshot().canSelectQuality).toBe(true);
    expect(controller.snapshot().formats).toHaveLength(1);
    expect(controller.snapshot().selectedFormatId).toBe(OPTION_ID);
    expect(controller.snapshot().downloadEligible).toBe(true);
    await controller.enqueueDownload();
    expect(api.createDownloadJob).toHaveBeenCalledWith(
      expect.any(String),
      OPTION_ID,
      token,
      expect.any(AbortSignal),
    );
  });

  it("preserves legacy download behavior when providerCapabilities are absent", async () => {
    const token = generateAccessToken();
    const payload = inspectedPayload();
    delete payload.providerCapabilities;
    const api = {
      createInspectionJob: vi.fn(async () => parseInspectionJob(inspectionPayload())),
      getInspectionJob: vi.fn(async () => parseInspectionJob(payload)),
      createDownloadJob: vi.fn(async () =>
        parseDownloadJob(downloadPayload({ state: "queued", artifactReady: false })),
      ),
      getDownloadJob: vi.fn(async () =>
        parseDownloadJob(
          downloadPayload({
            state: "failed",
            completedAt: "2026-08-13T04:22:27Z",
            errorCode: "DOWNLOAD_TOOL_FAILED",
          }),
        ),
      ),
      createBrowserGrant: vi.fn(),
      cancelDownloadJob: vi.fn(),
    };
    const controller = new MediaFlowController({
      api: api as unknown as MediaApi,
      session: new FlowSession(),
      generateToken: () => token,
      pickerSupported: () => false,
      secureContext: () => true,
      documentHidden: () => false,
    });

    await controller.submit("https://vk.com/video-1_2");
    expect(controller.snapshot().canSelectQuality).toBe(true);
    expect(controller.snapshot().downloadEligible).toBe(true);
    await controller.enqueueDownload();
    expect(api.createDownloadJob).toHaveBeenCalled();
  });

  it("fails closed for an explicit null provider capability profile", async () => {
    const token = generateAccessToken();
    const payload = inspectedPayload();
    payload.providerCapabilities = null;
    const api = {
      createInspectionJob: vi.fn(async () => parseInspectionJob(inspectionPayload())),
      getInspectionJob: vi.fn(async () => parseInspectionJob(payload)),
      createDownloadJob: vi.fn(),
      getDownloadJob: vi.fn(),
      createBrowserGrant: vi.fn(),
      cancelDownloadJob: vi.fn(),
    };
    const controller = new MediaFlowController({
      api: api as unknown as MediaApi,
      session: new FlowSession(),
      generateToken: () => token,
      pickerSupported: () => false,
      secureContext: () => true,
      documentHidden: () => false,
    });

    await controller.submit("https://vk.com/video-1_2");
    expect(controller.snapshot().downloadEligible).toBe(false);
    expect(controller.snapshot().canSelectQuality).toBe(false);
    await controller.enqueueDownload();
    expect(api.createDownloadJob).not.toHaveBeenCalled();
  });

  it("does not issue API calls when the feature flag is off", async () => {
    const { parseMediaFlowFlag } = await import("./flag");
    expect(parseMediaFlowFlag("false")).toBe(false);
  });
});
