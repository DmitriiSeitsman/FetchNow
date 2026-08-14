/**
 * @vitest-environment jsdom
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";
import { renderFlow } from "./render";
import type { FlowSnapshot } from "./controller";
import { MediaFlowController } from "./controller";
import { MediaApi } from "./api";
import { FlowSession } from "./session";
import { generateAccessToken } from "./credentials";
import {
  downloadPayload,
  inspectedPayload,
  inspectionPayload,
  progressiveFormat,
} from "./fixtures";
import {
  parseDownloadJob,
  parseInspectionJob,
  type MediaFormat,
} from "./contracts";

function snapshot(partial: Partial<FlowSnapshot>): FlowSnapshot {
  return {
    phase: "downloading",
    statusText: "Preparing video…",
    errorText: null,
    result: null,
    formats: [],
    selectedFormatId: null,
    downloadEligible: false,
    muxingBlocked: false,
    browserUnsupported: false,
    busy: true,
    canSubmit: false,
    canSave: false,
    canStartOver: true,
    canCancelTask: true,
    restored: false,
    progressStage: "downloading_video",
    progressPercent: null,
    artifactBytes: null,
    ...partial,
  };
}

function postPr9Formats(): MediaFormat[] {
  const heights = [144, 240, 360, 480, 720, 1080];
  return heights.map((height, index) => ({
    ...progressiveFormat,
    formatOptionId: `fmt_${String(index + 1).padStart(28, "0")}`,
    width: height === 1080 ? 1920 : Math.round((height * 16) / 9),
    height,
    qualityLabel: `p${height}`,
    freeTierEligible: height <= 720,
  }));
}

describe("PR10 loader, format, and contrast UX", () => {
  it("shows stage copy, hides spinner from assistive tech, and never claims a percent", () => {
    document.body.innerHTML = `
      <p data-flow-status></p>
      <section data-flow-progress hidden>
        <span class="loader" data-flow-loader hidden>
          <span class="spinner" data-flow-spinner></span>
        </span>
        <p data-flow-progress-label></p>
        <div data-flow-progress-bar><span data-flow-progress-fill></span></div>
      </section>
      <button data-flow-cancel hidden>Cancel task</button>
      <button data-flow-reset hidden>Start over</button>
      <p data-flow-restored hidden>Restored an existing download task.</p>
    `;
    renderFlow(
      document,
      snapshot({
        statusText: "Downloading file…",
        progressStage: "downloading_video",
      }),
    );
    const stage = document.querySelector("[data-flow-progress-label]");
    expect(stage?.textContent).toBe("Downloading file…");
    const status = document.querySelector("[data-flow-status]");
    expect(status?.textContent).toBe("");
    expect(status?.getAttribute("aria-live")).toBe("polite");
    expect(document.body.textContent ?? "").not.toMatch(/\d+%/);
    const loader = document.querySelector<HTMLElement>("[data-flow-loader]");
    expect(loader?.hidden).toBe(false);
    const spinner = document.querySelector("[data-flow-spinner]");
    expect(spinner?.getAttribute("aria-hidden")).toBe("true");
    const cancel = document.querySelector<HTMLButtonElement>("[data-flow-cancel]");
    expect(cancel?.hidden).toBe(false);
    expect(cancel?.textContent).toMatch(/Cancel task/);
    const startOver = document.querySelector<HTMLButtonElement>("[data-flow-reset]");
    expect(startOver?.hidden).toBe(false);
    expect(startOver?.textContent).toMatch(/Start over/);
  });

  it("makes every post-PR9 eligible quality selectable and defaults visually to 720p", () => {
    const formats = postPr9Formats();
    const selected = formats.find((item) => item.height === 720)?.formatOptionId ?? null;
    document.body.innerHTML = `
      <div data-flow-formats></div>
      <p data-flow-mux>Combined video+audio files are required; muxing is not offered yet</p>
    `;
    renderFlow(
      document,
      snapshot({
        phase: "inspected",
        statusText: "Choose a quality to download.",
        busy: false,
        canCancelTask: false,
        downloadEligible: true,
        muxingBlocked: false,
        result: {
          providerId: "vk",
          canonicalProviderUrl: "https://vk.com/video-1_2",
          mediaId: "-1_2",
          title: "Example",
          durationSeconds: 12,
          formats,
          muxingRequired: false,
        },
        formats,
        selectedFormatId: selected,
      }),
    );
    const radios = [
      ...document.querySelectorAll<HTMLInputElement>("input[type=radio]"),
    ];
    expect(radios).toHaveLength(6);
    const enabled = radios.filter((radio) => !radio.disabled);
    expect(enabled).toHaveLength(5);
    const mux = document.querySelector<HTMLElement>("[data-flow-mux]");
    expect(mux?.hidden).toBe(true);
    expect(document.querySelector("[data-flow-formats]")?.textContent ?? "").not.toMatch(
      /muxing is not offered/i,
    );
    const selectedLabel = document.querySelector(".format-selected .format-label");
    expect(selectedLabel?.textContent).toBe("720p");
    const disabledReason = document.querySelector(".format-reason");
    expect(disabledReason?.textContent).toMatch(/720p limit/i);
  });

  it("keeps Start over local and uses Cancel task for server jobs", async () => {
    const token = generateAccessToken();
    const cancelDownloadJob = vi.fn(async () =>
      parseDownloadJob(
        downloadPayload({
          state: "cancelled",
          completedAt: "2026-08-13T04:22:27Z",
        }),
      ),
    );
    const hangUntilAbort = async (signal?: AbortSignal) => {
      await new Promise<void>((_resolve, reject) => {
        if (signal?.aborted) {
          reject(new DOMException("Aborted", "AbortError"));
          return;
        }
        signal?.addEventListener("abort", () => {
          reject(new DOMException("Aborted", "AbortError"));
        });
      });
      throw new Error("unreachable");
    };
    const api = {
      createInspectionJob: vi.fn(async () => parseInspectionJob(inspectionPayload())),
      getInspectionJob: vi.fn(async () => parseInspectionJob(inspectedPayload())),
      createDownloadJob: vi.fn(async () =>
        parseDownloadJob(downloadPayload({ state: "queued" })),
      ),
      getDownloadJob: vi.fn(async (_id: string, _token: string, signal?: AbortSignal) =>
        hangUntilAbort(signal),
      ),
      cancelDownloadJob,
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
    const pending = controller.enqueueDownload();
    await Promise.resolve();
    await Promise.resolve();
    expect(controller.snapshot().canCancelTask).toBe(true);
    expect(controller.snapshot().canStartOver).toBe(true);
    controller.startOver();
    await pending;
    expect(cancelDownloadJob).not.toHaveBeenCalled();
    expect(controller.snapshot().phase).toBe("idle");
  });

  it("waits for a confirmed cancelled terminal after Cancel task", async () => {
    const token = generateAccessToken();
    const cancelDownloadJob = vi.fn(async () =>
      parseDownloadJob(
        downloadPayload({
          state: "cancelled",
          completedAt: "2026-08-13T04:22:27Z",
        }),
      ),
    );
    const hangUntilAbort = async (signal?: AbortSignal) => {
      await new Promise<void>((_resolve, reject) => {
        if (signal?.aborted) {
          reject(new DOMException("Aborted", "AbortError"));
          return;
        }
        signal?.addEventListener("abort", () => {
          reject(new DOMException("Aborted", "AbortError"));
        });
      });
      throw new Error("unreachable");
    };
    const api = {
      createInspectionJob: vi.fn(async () => parseInspectionJob(inspectionPayload())),
      getInspectionJob: vi.fn(async () => parseInspectionJob(inspectedPayload())),
      createDownloadJob: vi.fn(async () =>
        parseDownloadJob(downloadPayload({ state: "queued" })),
      ),
      getDownloadJob: vi.fn(async (_id: string, _token: string, signal?: AbortSignal) =>
        hangUntilAbort(signal),
      ),
      cancelDownloadJob,
    };
    const controller = new MediaFlowController({
      api: api as unknown as MediaApi,
      session: new FlowSession({
        getItem: () => null,
        setItem: () => undefined,
        removeItem: () => undefined,
      }),
      generateToken: () => token,
      pickerSupported: () => true,
      documentHidden: () => false,
    });
    await controller.submit("https://vk.com/video-1_2");
    const pending = controller.enqueueDownload();
    await Promise.resolve();
    await Promise.resolve();
    await controller.cancelTask();
    await pending;
    expect(cancelDownloadJob).toHaveBeenCalledOnce();
    expect(controller.snapshot().phase).toBe("cancelled");
    expect(controller.snapshot().statusText).toBe("Download cancelled");
  });

  it("uses dark navy tokens with AA contrast and non-color-only disabled styles", () => {
    const cssPath = join(
      dirname(fileURLToPath(import.meta.url)),
      "../../styles/global.css",
    );
    const css = readFileSync(cssPath, "utf8");
    expect(css).toContain("--ink: #0b1f3a");
    expect(css).toContain("--ink-muted: #304766");
    expect(css).toMatch(/prefers-reduced-motion:\s*reduce/);
    expect(css).toMatch(/\.spinner[\s\S]*animation:\s*none/);
    expect(css).toContain(".format-disabled");
    expect(css).toContain("border-style: dashed");
    expect(css).toContain("--disabled-fill:");
    expect(css).not.toMatch(/\.btn:disabled\s*\{[^}]*opacity:\s*0\.\d+/);

    const contrast = (hexFg: string, hexBg: string): number => {
      const toLin = (channel: number) => {
        const value = channel / 255;
        return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
      };
      const parse = (hex: string) => {
        const raw = hex.replace("#", "");
        return [
          toLin(Number.parseInt(raw.slice(0, 2), 16)),
          toLin(Number.parseInt(raw.slice(2, 4), 16)),
          toLin(Number.parseInt(raw.slice(4, 6), 16)),
        ] as const;
      };
      const lum = (hex: string) => {
        const [r, g, b] = parse(hex);
        return 0.2126 * r + 0.7152 * g + 0.0722 * b;
      };
      const l1 = lum(hexFg);
      const l2 = lum(hexBg);
      const lighter = Math.max(l1, l2);
      const darker = Math.min(l1, l2);
      return (lighter + 0.05) / (darker + 0.05);
    };
    expect(contrast("#0b1f3a", "#f3f7f2")).toBeGreaterThanOrEqual(4.5);
    expect(contrast("#304766", "#f3f7f2")).toBeGreaterThanOrEqual(4.5);
    expect(contrast("#3d5a73", "#f3f7f2")).toBeGreaterThanOrEqual(4.5);
    expect(contrast("#304766", "#e4ebf2")).toBeGreaterThanOrEqual(4.5);
  });
});
