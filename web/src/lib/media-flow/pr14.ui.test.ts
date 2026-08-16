/**
 * @vitest-environment jsdom
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";
import { renderFlow } from "./render";
import { MediaFlowController } from "./controller";
import { MediaApi } from "./api";
import { FlowSession } from "./session";
import { generateAccessToken } from "./credentials";
import { fileSystemAccessSupported, isSecureDeliveryContext } from "./download";
import type { FlowSnapshot } from "./controller";
import {
  BROWSER_GRANT_PATH,
  browserGrantPayload,
  downloadPayload,
  inspectedPayload,
  inspectionPayload,
  OPTION_ID,
} from "./fixtures";
import { parseBrowserGrant, parseDownloadJob, parseInspectionJob } from "./contracts";
import { saveArtifactStream } from "./download";

const here = dirname(fileURLToPath(import.meta.url));
const astroSource = readFileSync(
  join(here, "../../components/MediaFlow.astro"),
  "utf8",
);

function snapshot(partial: Partial<FlowSnapshot> = {}): FlowSnapshot {
  return {
    phase: "idle",
    statusText: "",
    errorText: null,
    result: null,
    formats: [],
    selectedFormatId: null,
    downloadEligible: false,
    muxingBlocked: false,
    httpsRequired: false,
    grantArming: false,
    canNativeDownload: false,
    canRetryGrant: false,
    canSaveAs: false,
    downloadHref: null,
    nativeDownloadHandoff: false,
    browserUnsupported: false,
    busy: false,
    canSubmit: true,
    canStartOver: false,
    canCancelTask: false,
    restored: false,
    progressStage: null,
    progressPercent: null,
    artifactBytes: null,
    ...partial,
  };
}

function mountFlow(): void {
  document.body.innerHTML = `
    <section class="flow" data-media-flow>
      <p class="status" data-flow-status aria-live="polite"></p>
      <section class="progress-card" data-flow-progress hidden>
        <div class="progress-head">
          <span class="loader" data-flow-loader hidden><span class="spinner"></span></span>
          <p class="progress-stage" data-flow-progress-label></p>
        </div>
        <div class="progress-track" data-flow-progress-bar></div>
      </section>
      <p data-flow-https hidden></p>
      <p data-flow-grant-pending hidden></p>
      <p data-flow-handoff hidden></p>
      <button class="btn btn-primary" data-flow-save-as hidden>Save as…</button>
      <a class="btn btn-primary" data-flow-native-download hidden download>Download file</a>
    </section>
  `;
}

function el(selector: string): HTMLElement {
  const node = document.querySelector<HTMLElement>(selector);
  if (!node) {
    throw new Error(`missing ${selector}`);
  }
  return node;
}

describe("PR14 native browser download", () => {
  it("pins a real anchor for native download, not a button or blob URL", () => {
    expect(astroSource).toContain('data-flow-native-download');
    expect(astroSource).toContain("Download file");
    expect(astroSource).toContain("download");
    expect(astroSource).toContain("data-flow-save-as");
    expect(astroSource).not.toContain("data-flow-save hidden");
    expect(astroSource).toContain("Save as…");
    // Save as… leads the action row where it exists, so it is mounted first.
    expect(astroSource.indexOf("data-flow-save-as")).toBeLessThan(
      astroSource.indexOf("data-flow-native-download"),
    );
    // The "requires a Chromium desktop browser" note explained a control that is
    // no longer rendered on those browsers.
    expect(astroSource).not.toContain("data-flow-browser");
  });

  it("strictly parses browser grant responses", () => {
    const grant = parseBrowserGrant(browserGrantPayload());
    expect(grant.downloadPath).toBe(BROWSER_GRANT_PATH);
    expect(() =>
      parseBrowserGrant({
        downloadPath: BROWSER_GRANT_PATH,
        expiresAt: "2099-01-01T00:00:00Z",
        extra: true,
      }),
    ).toThrow();
  });

  it("renders a same-origin href on the native download anchor", () => {
    mountFlow();
    renderFlow(
      document,
      snapshot({
        phase: "ready",
        canNativeDownload: true,
        downloadHref: BROWSER_GRANT_PATH,
      }),
    );
    const link = el("[data-flow-native-download]") as HTMLAnchorElement;
    expect(link.tagName).toBe("A");
    expect(link.href).toContain(BROWSER_GRANT_PATH);
    expect(link.href).not.toMatch(/[?#]/);
    expect(link.hidden).toBe(false);
    expect(link.textContent).toBe("Download file");
  });

  it("shows grant arming and handoff copy without faking progress", () => {
    mountFlow();
    renderFlow(
      document,
      snapshot({
        phase: "ready",
        grantArming: true,
        statusText: "Preparing secure download…",
        progressStage: "ready",
        artifactBytes: 12_000_000,
      }),
    );
    expect(el("[data-flow-grant-pending]").hidden).toBe(false);
    expect(el("[data-flow-native-download]").hidden).toBe(true);

    renderFlow(
      document,
      snapshot({
        phase: "ready",
        nativeDownloadHandoff: true,
        canNativeDownload: true,
        downloadHref: BROWSER_GRANT_PATH,
        statusText: "Sent to your browser",
        progressStage: "ready",
        artifactBytes: 12_000_000,
      }),
    );
    expect(el("[data-flow-handoff]").hidden).toBe(false);
    expect(el("[data-flow-progress]").hidden).toBe(true);
    const link = el("[data-flow-native-download]") as HTMLAnchorElement;
    expect(link.textContent).toBe("Download again");
  });

  it("shows HTTPS required on insecure remote origins and never arms download", () => {
    mountFlow();
    renderFlow(
      document,
      snapshot({
        phase: "ready",
        httpsRequired: true,
        statusText: "Open this page over HTTPS to download the prepared file.",
      }),
    );
    expect(el("[data-flow-https]").hidden).toBe(false);
    expect(el("[data-flow-native-download]").hidden).toBe(true);
    expect(isSecureDeliveryContext("http://example.com")).toBe(false);
    expect(isSecureDeliveryContext("http://localhost")).toBe(true);
    expect(isSecureDeliveryContext("https://example.com")).toBe(true);
  });

  it("offers only the native download when the browser has no save picker", () => {
    mountFlow();
    renderFlow(
      document,
      snapshot({
        phase: "ready",
        canNativeDownload: true,
        downloadHref: BROWSER_GRANT_PATH,
        browserUnsupported: true,
        canSaveAs: false,
      }),
    );
    const link = el("[data-flow-native-download]");
    expect(link.hidden).toBe(false);
    expect(link.classList.contains("btn-primary")).toBe(true);
    expect(el("[data-flow-save-as]").hidden).toBe(true);
    expect(fileSystemAccessSupported({ hasSavePicker: () => false })).toBe(false);
  });

  it("leads with Save as… and demotes the anchor when a picker exists", () => {
    mountFlow();
    renderFlow(
      document,
      snapshot({
        phase: "ready",
        canNativeDownload: true,
        downloadHref: BROWSER_GRANT_PATH,
        browserUnsupported: false,
        canSaveAs: true,
      }),
    );
    const saveAs = document.querySelector<HTMLButtonElement>("[data-flow-save-as]");
    expect(saveAs?.hidden).toBe(false);
    expect(saveAs?.disabled).toBe(false);
    expect(saveAs?.classList.contains("btn-primary")).toBe(true);
    const link = el("[data-flow-native-download]");
    expect(link.hidden).toBe(false);
    expect(link.classList.contains("btn-ghost")).toBe(true);
    expect(link.classList.contains("btn-primary")).toBe(false);
  });

  it("keeps Save as… mounted but disabled while the save runs", () => {
    mountFlow();
    renderFlow(
      document,
      snapshot({
        phase: "saving",
        busy: true,
        canSaveAs: false,
        browserUnsupported: false,
      }),
    );
    const saveAs = document.querySelector<HTMLButtonElement>("[data-flow-save-as]");
    expect(saveAs?.hidden).toBe(false);
    expect(saveAs?.disabled).toBe(true);
  });

  it("arms a grant when the job becomes ready and ignores stale responses", async () => {
    const token = generateAccessToken();
    let grantCalls = 0;
    const first = vi.fn(async () => {
      grantCalls += 1;
      await new Promise((resolve) => setTimeout(resolve, 20));
      return parseBrowserGrant(browserGrantPayload());
    });
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
      createBrowserGrant: first,
      cancelDownloadJob: vi.fn(),
    };
    const controller = new MediaFlowController({
      api: api as unknown as MediaApi,
      session: new FlowSession(),
      generateToken: () => token,
      pickerSupported: () => false,
      secureContext: () => true,
      documentHidden: () => false,
      save: vi.fn() as unknown as typeof saveArtifactStream,
    });
    await controller.submit("https://vk.com/video-1_2");
    controller.selectFormat(OPTION_ID);
    await controller.enqueueDownload();
    expect(controller.snapshot().grantArming).toBe(true);
    await vi.waitFor(() => {
      expect(controller.snapshot().canNativeDownload).toBe(true);
    });
    expect(controller.snapshot().downloadHref).toBe(BROWSER_GRANT_PATH);
    expect(grantCalls).toBe(1);

    controller.startOver();
    const staleGrant = vi.fn(async () => parseBrowserGrant(browserGrantPayload()));
    api.createBrowserGrant = staleGrant;
    await controller.submit("https://vk.com/video-1_2");
    controller.selectFormat(OPTION_ID);
    const pending = controller.enqueueDownload();
    await vi.waitFor(() => {
      expect(staleGrant).toHaveBeenCalled();
    });
    controller.startOver();
    await pending;
    expect(controller.snapshot().phase).toBe("idle");
  });

  it("keeps the session after native download click", async () => {
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
      save: vi.fn() as unknown as typeof saveArtifactStream,
    });
    await controller.submit("https://vk.com/video-1_2");
    controller.selectFormat(OPTION_ID);
    await controller.enqueueDownload();
    await vi.waitFor(() => {
      expect(controller.snapshot().canNativeDownload).toBe(true);
    });
    controller.onNativeDownloadClick();
    expect(controller.snapshot().nativeDownloadHandoff).toBe(true);
    expect(controller.snapshot().phase).toBe("ready");
    expect(store.size).toBe(1);
    expect(controller.snapshot().downloadHref).toBe(BROWSER_GRANT_PATH);
  });
});

describe("PR14 grant refresh / resume / retry hardening", () => {
  function readyApi(createBrowserGrant: ReturnType<typeof vi.fn>) {
    return {
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
      createBrowserGrant,
      cancelDownloadJob: vi.fn(),
    };
  }

  async function reachReady(controller: MediaFlowController): Promise<void> {
    await controller.submit("https://vk.com/video-1_2");
    controller.selectFormat(OPTION_ID);
    await controller.enqueueDownload();
    await vi.waitFor(() => {
      expect(controller.snapshot().phase).toBe("ready");
    });
    await vi.waitFor(() => {
      expect(
        controller.snapshot().canNativeDownload || controller.snapshot().canRetryGrant,
      ).toBe(true);
    });
  }

  it("does not immediately loop grant refresh for a 30-second TTL", async () => {
    const token = generateAccessToken();
    let clock = Date.parse("2026-06-01T00:00:00.000Z");
    let grantCalls = 0;
    const createBrowserGrant = vi.fn(async () => {
      grantCalls += 1;
      return parseBrowserGrant(
        browserGrantPayload({
          expiresAt: new Date(clock + 30_000).toISOString(),
        }),
      );
    });
    const controller = new MediaFlowController({
      api: readyApi(createBrowserGrant) as unknown as MediaApi,
      session: new FlowSession(),
      generateToken: () => token,
      pickerSupported: () => false,
      secureContext: () => true,
      documentHidden: () => false,
      save: vi.fn() as unknown as typeof saveArtifactStream,
      now: () => clock,
    });
    await reachReady(controller);
    expect(grantCalls).toBe(1);
    controller.disconnect();

    vi.useFakeTimers();
    try {
      const advance = async (ms: number) => {
        clock += ms;
        await vi.advanceTimersByTimeAsync(ms);
      };
      // Re-arm under fake timers so the refresh timer is controllable.
      await controller.armNativeDownload();
      expect(grantCalls).toBe(2);

      await advance(14_000);
      expect(grantCalls).toBe(2);
      await advance(1_500);
      expect(grantCalls).toBe(3);
      await advance(14_000);
      expect(grantCalls).toBe(3);
    } finally {
      vi.useRealTimers();
      controller.disconnect();
    }
  });

  it("refreshes a 300-second grant before expiry with a positive delay", async () => {
    const token = generateAccessToken();
    let clock = Date.parse("2026-06-01T00:00:00.000Z");
    let grantCalls = 0;
    const createBrowserGrant = vi.fn(async () => {
      grantCalls += 1;
      return parseBrowserGrant(
        browserGrantPayload({
          expiresAt: new Date(clock + 300_000).toISOString(),
        }),
      );
    });
    const controller = new MediaFlowController({
      api: readyApi(createBrowserGrant) as unknown as MediaApi,
      session: new FlowSession(),
      generateToken: () => token,
      pickerSupported: () => false,
      secureContext: () => true,
      documentHidden: () => false,
      save: vi.fn() as unknown as typeof saveArtifactStream,
      now: () => clock,
    });
    await reachReady(controller);
    expect(grantCalls).toBe(1);
    controller.disconnect();

    vi.useFakeTimers();
    try {
      // Still within TTL — arm only reschedules (no extra POST).
      await controller.armNativeDownload();
      expect(grantCalls).toBe(1);
      // Expire, then re-arm under fake timers.
      clock += 301_000;
      await controller.armNativeDownload();
      expect(grantCalls).toBe(2);
      // Fresh 300s grant scheduled at 270s.
      clock += 269_000;
      await vi.advanceTimersByTimeAsync(269_000);
      expect(grantCalls).toBe(2);
      clock += 1_500;
      await vi.advanceTimersByTimeAsync(1_500);
      expect(grantCalls).toBe(3);
    } finally {
      vi.useRealTimers();
      controller.disconnect();
    }
  });

  it("blocks stale native clicks and rearms on foreground resume", async () => {
    const token = generateAccessToken();
    let clock = Date.parse("2026-06-01T00:00:00.000Z");
    let grantCalls = 0;
    const createBrowserGrant = vi.fn(async () => {
      grantCalls += 1;
      return parseBrowserGrant(
        browserGrantPayload({
          expiresAt: new Date(clock + 30_000).toISOString(),
        }),
      );
    });
    const controller = new MediaFlowController({
      api: readyApi(createBrowserGrant) as unknown as MediaApi,
      session: new FlowSession(),
      generateToken: () => token,
      pickerSupported: () => false,
      secureContext: () => true,
      documentHidden: () => false,
      save: vi.fn() as unknown as typeof saveArtifactStream,
      now: () => clock,
    });
    await reachReady(controller);
    expect(controller.onNativeDownloadClick()).toBe(true);

    clock += 45_000;
    expect(controller.onNativeDownloadClick()).toBe(false);
    expect(controller.snapshot().downloadHref).toBeNull();
    await vi.waitFor(() => expect(grantCalls).toBeGreaterThanOrEqual(2));
    expect(controller.snapshot().canNativeDownload).toBe(true);

    clock += 45_000;
    const beforeResume = grantCalls;
    controller.onForegroundResume();
    expect(controller.snapshot().downloadHref).toBeNull();
    await vi.waitFor(() => expect(grantCalls).toBe(beforeResume + 1));
    expect(controller.snapshot().canNativeDownload).toBe(true);
  });

  it("retries grant access without recreating the download job", async () => {
    const token = generateAccessToken();
    let grantCalls = 0;
    let failOnce = true;
    const createBrowserGrant = vi.fn(async () => {
      grantCalls += 1;
      if (failOnce) {
        failOnce = false;
        throw Object.assign(new Error("transient"), { code: "INTERNAL_ERROR" });
      }
      return parseBrowserGrant(browserGrantPayload());
    });
    const api = readyApi(createBrowserGrant);
    const controller = new MediaFlowController({
      api: api as unknown as MediaApi,
      session: new FlowSession(),
      generateToken: () => token,
      pickerSupported: () => false,
      secureContext: () => true,
      documentHidden: () => false,
      save: vi.fn() as unknown as typeof saveArtifactStream,
    });
    await reachReady(controller);
    await vi.waitFor(() => expect(controller.snapshot().canRetryGrant).toBe(true));
    expect(api.createDownloadJob).toHaveBeenCalledTimes(1);
    expect(controller.snapshot().phase).toBe("ready");

    controller.retryGrantAccess();
    controller.retryGrantAccess();
    await vi.waitFor(() => expect(controller.snapshot().canNativeDownload).toBe(true));
    expect(api.createDownloadJob).toHaveBeenCalledTimes(1);
    expect(grantCalls).toBe(2);
    expect(controller.snapshot().downloadHref).toBe(BROWSER_GRANT_PATH);
  });

  it("shows Retry download access when grant arming fails", () => {
    document.body.innerHTML = `
      <section data-media-flow>
        <a data-flow-native-download hidden download>Download file</a>
        <button data-flow-grant-retry hidden>Retry download access</button>
      </section>
    `;
    renderFlow(
      document,
      snapshot({
        phase: "ready",
        canRetryGrant: true,
        canNativeDownload: false,
        downloadHref: null,
      }),
    );
    const retry = document.querySelector<HTMLButtonElement>("[data-flow-grant-retry]");
    expect(retry?.hidden).toBe(false);
    expect(retry?.disabled).toBe(false);
    expect(astroSource).toContain("Retry download access");
  });
});
