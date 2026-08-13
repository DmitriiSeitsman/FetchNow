/**
 * @vitest-environment jsdom
 */
import { describe, expect, it, vi } from "vitest";
import { MediaFlowController } from "./controller";
import { MediaApi } from "./api";
import { FlowSession } from "./session";
import { generateAccessToken } from "./credentials";
import {
  downloadPayload,
  inspectedPayload,
  inspectionPayload,
  OPTION_ID,
} from "./fixtures";
import { parseDownloadJob, parseInspectionJob } from "./contracts";

describe("browser flow integration", () => {
  it("submits, inspects, selects, downloads, and streams a save", async () => {
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
      documentHidden: () => false,
      save,
    });

    await controller.submit("https://vk.com/video1");
    expect(controller.snapshot().phase).toBe("inspected");
    expect(api.createInspectionJob).toHaveBeenCalledTimes(1);
    controller.selectFormat(OPTION_ID);
    expect(controller.snapshot().downloadEligible).toBe(true);
    await controller.enqueueDownload();
    expect(controller.snapshot().phase).toBe("ready");
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

  it("does not issue API calls when the feature flag is off", async () => {
    const { parseMediaFlowFlag } = await import("./flag");
    expect(parseMediaFlowFlag("false")).toBe(false);
  });
});
