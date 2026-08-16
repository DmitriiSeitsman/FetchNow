/**
 * @vitest-environment jsdom
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";
import { renderFlow } from "./render";
import { MediaFlowController, type FlowSnapshot } from "./controller";
import { MediaApi } from "./api";
import { FlowSession } from "./session";
import { generateAccessToken } from "./credentials";
import { progressView, STAGE_COMPLETION } from "./progress";
import {
  browserGrantPayload,
  downloadPayload,
  inspectedPayload,
  inspectionPayload,
  progressiveFormat,
  OPTION_ID,
} from "./fixtures";
import { parseBrowserGrant, parseDownloadJob, parseInspectionJob, type MediaFormat } from "./contracts";
import { flowErrorFromCode } from "./errors";

const here = dirname(fileURLToPath(import.meta.url));
const astroSource = readFileSync(
  join(here, "../../components/MediaFlow.astro"),
  "utf8",
);
const cssSource = readFileSync(join(here, "../../styles/global.css"), "utf8");

const alternateFormat: MediaFormat = {
  ...progressiveFormat,
  formatOptionId: "fmt_bbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  height: 480,
  width: 854,
  qualityLabel: "p480",
  approxBytes: 6_000_000,
};

const RESULT: FlowSnapshot["result"] = {
  providerId: "vk",
  canonicalProviderUrl: "https://vk.com/video-1_2",
  mediaId: "-1_2",
  title: "Example",
  durationSeconds: 90,
  formats: [progressiveFormat],
  muxingRequired: false,
};

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

/** Mirrors the initial markup of MediaFlow.astro, including hidden attributes. */
function mountFlow(): void {
  document.body.innerHTML = `
    <section class="flow" data-media-flow>
      <p class="status" data-flow-status aria-live="polite"></p>
      <p class="hint" data-flow-restored hidden>Restored an existing download task.</p>
      <article class="meta-card" data-flow-meta hidden>
        <h2 data-flow-title></h2>
        <p><span data-flow-provider></span><span data-flow-duration></span></p>
      </article>
      <fieldset class="quality-card" data-flow-quality hidden>
        <legend class="card-title">Choose quality</legend>
        <div class="quality-list" data-flow-formats hidden></div>
      </fieldset>
      <section class="progress-card" data-flow-progress data-tone="active" hidden>
        <div class="progress-head">
          <span class="loader" data-flow-loader hidden>
            <span class="spinner" data-flow-spinner aria-hidden="true"></span>
          </span>
          <p class="progress-stage" data-flow-progress-label role="status" aria-live="polite"></p>
        </div>
        <div
          class="progress-track"
          data-flow-progress-bar
          role="progressbar"
          aria-valuemin="0"
          aria-valuemax="100"
          aria-valuenow="0"
          aria-valuetext="Ожидание начала…"
        >
          <span class="progress-fill" data-flow-progress-fill></span>
        </div>
        <p class="hint progress-note">
          Progress follows preparation stages. Download percentages appear when an estimated size is available.
        </p>
        <div class="progress-actions" data-flow-progress-actions hidden>
          <button class="btn btn-ghost" type="button" data-flow-cancel hidden>
            Cancel task
          </button>
        </div>
      </section>
      <p class="hint" data-flow-mux hidden></p>
      <button data-flow-download hidden>Prepare download</button>
      <a data-flow-native-download hidden download>Download file</a>
      <button data-flow-save-as hidden>Save as…</button>
      <button data-flow-reset hidden>Start over</button>
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

function duplicateFormats(): MediaFormat[] {
  return [
    { ...progressiveFormat, formatOptionId: `fmt_${"a1".padStart(28, "0")}` },
    {
      ...progressiveFormat,
      formatOptionId: `fmt_${"b2".padStart(28, "0")}`,
      container: "webm",
    },
    {
      ...progressiveFormat,
      formatOptionId: `fmt_${"c3".padStart(28, "0")}`,
      container: "mp4",
      fps: 25,
    },
    // Second displayable quality so the selector stays visible after grouping.
    alternateFormat,
  ];
}

describe("PR11 loader, quality and stage progress UI", () => {
  it("keeps the loader and progress card hidden on the first idle render", () => {
    mountFlow();
    renderFlow(document, snapshot());
    expect(el("[data-flow-loader]").hidden).toBe(true);
    expect(el("[data-flow-progress]").hidden).toBe(true);
    expect(el("[data-flow-progress-actions]").hidden).toBe(true);
    expect(el("[data-flow-quality]").hidden).toBe(true);
    expect(el("[data-flow-progress-label]").textContent).toBe("");
    expect(el("[data-flow-progress-bar]").getAttribute("aria-valuenow")).toBe("0");
  });

  it("keeps Cancel task inside the progress card footer", () => {
    const progressIdx = astroSource.indexOf('data-flow-progress');
    const cancelIdx = astroSource.indexOf("data-flow-cancel");
    const progressEnd = astroSource.indexOf("</section>", progressIdx);
    expect(progressIdx).toBeGreaterThan(-1);
    expect(cancelIdx).toBeGreaterThan(progressIdx);
    expect(cancelIdx).toBeLessThan(progressEnd);
    expect(astroSource).toMatch(
      /data-flow-progress-actions hidden>[\s\S]*data-flow-cancel/,
    );
    expect(cssSource).toContain(".progress-actions[hidden]");
    expect(cssSource).toContain(".progress-actions");

    mountFlow();
    renderFlow(
      document,
      snapshot({
        phase: "downloading",
        progressStage: "downloading_video",
        canCancelTask: true,
        canStartOver: true,
        canSubmit: false,
        busy: true,
      }),
    );
    expect(el("[data-flow-progress]").hidden).toBe(false);
    expect(el("[data-flow-progress-actions]").hidden).toBe(false);
    expect(el("[data-flow-cancel]").hidden).toBe(false);
    expect(el("[data-flow-progress]").contains(el("[data-flow-cancel]"))).toBe(true);
  });

  it("declares the loader hidden in markup and lets [hidden] win over the layout CSS", () => {
    expect(astroSource).toMatch(/<span class="loader" data-flow-loader hidden>/);
    expect(astroSource).toMatch(/<section class="progress-card"[^>]*hidden>/);
    expect(astroSource).toMatch(
      /<fieldset class="quality-card" data-flow-quality hidden>/,
    );
    expect(astroSource.indexOf("data-flow-progress")).toBeLessThan(
      astroSource.indexOf("data-flow-loader"),
    );
    expect(cssSource).toMatch(/\.loader\[hidden\][\s\S]{0,220}display:\s*none/);
    expect(cssSource).toContain(".progress-card[hidden]");
    expect(cssSource).toContain(".quality-card[hidden]");

    mountFlow();
    const style = document.createElement("style");
    style.textContent = cssSource;
    document.head.append(style);
    renderFlow(document, snapshot());
    const loader = el("[data-flow-loader]");
    expect(getComputedStyle(loader).display).toBe("none");
    loader.hidden = false;
    expect(getComputedStyle(loader).display).toBe("flex");
  });

  it("keeps the standalone loader hidden while a quality is being chosen", () => {
    mountFlow();
    const formats = [progressiveFormat, alternateFormat];
    renderFlow(
      document,
      snapshot({
        phase: "inspected",
        statusText: "Выберите качество для скачивания.",
        result: { ...RESULT, formats },
        formats,
        selectedFormatId: OPTION_ID,
        downloadEligible: true,
        canStartOver: true,
        canSubmit: false,
      }),
    );
    expect(el("[data-flow-loader]").hidden).toBe(true);
    expect(el("[data-flow-progress]").hidden).toBe(true);
    expect(el("[data-flow-quality]").hidden).toBe(false);
    expect(el("[data-flow-status]").textContent).toBe("Выберите качество для скачивания.");
  });

  it("hides the quality selector when only one grouped option exists", () => {
    mountFlow();
    renderFlow(
      document,
      snapshot({
        phase: "inspected",
        statusText: "Выберите качество для скачивания.",
        result: RESULT,
        formats: [progressiveFormat],
        selectedFormatId: OPTION_ID,
        downloadEligible: true,
        canStartOver: true,
        canSubmit: false,
      }),
    );
    expect(el("[data-flow-quality]").hidden).toBe(true);
    expect(
      document.querySelector<HTMLButtonElement>("[data-flow-download]")?.disabled,
    ).toBe(false);
  });

  it("shows the spinner only while an operation is running", () => {
    mountFlow();
    for (const phase of [
      "submitting",
      "inspecting",
      "downloading",
      "saving",
    ] as const) {
      renderFlow(document, snapshot({ phase, progressStage: null }));
      expect(el("[data-flow-loader]").hidden).toBe(false);
      expect(el("[data-flow-progress]").hidden).toBe(false);
    }
    for (const phase of ["idle", "inspected", "ready", "completed"] as const) {
      renderFlow(document, snapshot({ phase }));
      expect(el("[data-flow-loader]").hidden).toBe(true);
    }
    expect(el("[data-flow-spinner]").getAttribute("aria-hidden")).toBe("true");
  });

  it("renders one row per quality and drops duplicate variants", () => {
    mountFlow();
    const formats = duplicateFormats();
    renderFlow(
      document,
      snapshot({
        phase: "inspected",
        result: { ...RESULT, formats },
        formats,
        selectedFormatId: formats[0].formatOptionId,
        downloadEligible: true,
      }),
    );
    const radios = [
      ...document.querySelectorAll<HTMLInputElement>("input[type=radio]"),
    ];
    expect(radios).toHaveLength(2);
    expect(radios[0].value).toBe(formats[0].formatOptionId);
    expect(radios[0].checked).toBe(true);
    const labels = [...document.querySelectorAll(".format-label")].map(
      (node) => node.textContent,
    );
    expect(labels).toEqual(["Высокое (720p)", "Среднее (480p)"]);
    expect(el("[data-flow-formats]").textContent).toContain("MP4");
    expect(el("[data-flow-formats]").textContent).toContain("30 fps");
  });

  it("keeps a restored duplicate selection checked without inventing an id", () => {
    mountFlow();
    const formats = duplicateFormats();
    const restored = formats[1].formatOptionId;
    renderFlow(
      document,
      snapshot({
        phase: "inspected",
        result: { ...RESULT, formats },
        formats,
        selectedFormatId: restored,
        downloadEligible: true,
        restored: true,
      }),
    );
    const radios = [
      ...document.querySelectorAll<HTMLInputElement>("input[type=radio]"),
    ];
    expect(radios).toHaveLength(2);
    expect(radios[0].value).toBe(restored);
    expect(radios[0].checked).toBe(true);
    expect(document.querySelector(".format-selected")).not.toBeNull();
    expect(el("[data-flow-restored]").hidden).toBe(false);
  });

  it("omits secondary details that the response does not provide", () => {
    mountFlow();
    const sparse: MediaFormat = {
      ...progressiveFormat,
      fps: null,
      approxBytes: null,
    };
    const formats = [sparse, alternateFormat];
    renderFlow(
      document,
      snapshot({
        phase: "inspected",
        result: { ...RESULT, formats },
        formats,
        selectedFormatId: sparse.formatOptionId,
        downloadEligible: true,
      }),
    );
    expect(el(".format-detail").textContent).toBe(
      "MP4 · Размер станет известен после подготовки",
    );
  });

  it("disables an unavailable quality with a short reason", () => {
    mountFlow();
    const blocked: MediaFormat = {
      ...progressiveFormat,
      formatOptionId: `fmt_${"d4".padStart(28, "0")}`,
      height: 1080,
      qualityLabel: "p1080",
      freeTierEligible: false,
    };
    const formats = [progressiveFormat, blocked];
    renderFlow(
      document,
      snapshot({
        phase: "inspected",
        result: { ...RESULT, formats },
        formats,
        selectedFormatId: OPTION_ID,
        downloadEligible: true,
      }),
    );
    const radios = [
      ...document.querySelectorAll<HTMLInputElement>("input[type=radio]"),
    ];
    expect(radios).toHaveLength(2);
    expect(radios[0].disabled).toBe(true);
    expect(radios[1].disabled).toBe(false);
    expect(el(".format-reason").textContent).toBe("Выше лимита бесплатного режима 720p.");
    expect(el(".format-disabled")).not.toBeNull();
  });

  it("maps every stage to honest copy and a growing step completion", () => {
    mountFlow();
    const seen: number[] = [];
    const cases: Array<[FlowSnapshot["progressStage"], string]> = [
      ["queued", "Ожидание начала…"],
      ["inspecting", "Проверяем выбранное качество…"],
      ["downloading_video", "Скачиваем файл…"],
      ["downloading_audio", "Скачиваем звук…"],
      ["muxing", "Собираем видео и звук…"],
      ["verifying", "Проверяем подготовленный файл…"],
      ["publishing", "Размещаем файл во временном хранилище…"],
    ];
    for (const [stage, label] of cases) {
      renderFlow(document, snapshot({ phase: "downloading", progressStage: stage }));
      expect(el("[data-flow-progress-label]").textContent).toBe(label);
      const bar = el("[data-flow-progress-bar]");
      expect(bar.getAttribute("role")).toBe("progressbar");
      expect(bar.getAttribute("aria-valuemin")).toBe("0");
      expect(bar.getAttribute("aria-valuemax")).toBe("100");
      expect(bar.getAttribute("aria-valuetext")).toBe(label);
      const value = Number(bar.getAttribute("aria-valuenow"));
      expect(value).toBeGreaterThan(0);
      expect(value).toBeLessThan(100);
      seen.push(value);
      expect(el("[data-flow-progress-fill]").style.width).toBe(`${value}%`);
    }
    expect([...seen].sort((a, b) => a - b)).toEqual(seen);
    expect(document.body.textContent ?? "").not.toMatch(/\d+\s*%/);
    expect(el(".progress-note").textContent).toMatch(
      /Download percentages appear when an estimated size is available/i,
    );
    expect(el(".progress-note").textContent ?? "").not.toMatch(/not a download percentage/i);
  });

  it("lets a direct progressive job skip the audio and muxing stages", () => {
    const video = progressView("downloading", "downloading_video");
    const publishing = progressView("downloading", "publishing");
    expect(publishing.percent).toBeGreaterThan(video.percent);
    expect(publishing.label).toBe("Размещаем файл во временном хранилище…");
    expect(STAGE_COMPLETION.downloading_audio).toBeGreaterThan(
      STAGE_COMPLETION.downloading_video,
    );
    expect(STAGE_COMPLETION.publishing).toBeGreaterThan(STAGE_COMPLETION.muxing);

    mountFlow();
    renderFlow(
      document,
      snapshot({ phase: "downloading", progressStage: "publishing" }),
    );
    expect(el("[data-flow-progress]").hidden).toBe(false);
    expect(el("[data-flow-progress-label]").textContent).toBe(
      "Размещаем файл во временном хранилище…",
    );
  });

  it("shows retrying, ready, saving and completed states honestly", () => {
    mountFlow();
    renderFlow(document, snapshot({ phase: "downloading", progressStage: "retrying" }));
    expect(el("[data-flow-progress-label]").textContent).toBe("Повторяем подготовку…");

    renderFlow(
      document,
      snapshot({ phase: "ready", progressStage: "ready", statusText: "Готово к скачиванию" }),
    );
    expect(el("[data-flow-progress-bar]").getAttribute("aria-valuenow")).toBe("100");
    expect(el("[data-flow-progress-label]").textContent).toBe("Готово к скачиванию");
    expect(el("[data-flow-progress]").dataset.tone).toBe("done");
    expect(el("[data-flow-loader]").hidden).toBe(true);

    renderFlow(document, snapshot({ phase: "saving" }));
    expect(el("[data-flow-progress-label]").textContent).toBe(
      "Сохраняем на ваш компьютер…",
    );
    expect(el("[data-flow-loader]").hidden).toBe(false);

    renderFlow(document, snapshot({ phase: "completed" }));
    expect(el("[data-flow-progress-label]").textContent).toBe("Сохранено на ваш компьютер");
    expect(el("[data-flow-progress-bar]").getAttribute("aria-valuenow")).toBe("100");
    expect(el("[data-flow-loader]").hidden).toBe(true);
  });

  it("never renders error, cancel or expired states as active progress", () => {
    mountFlow();
    const terminals: Array<Partial<FlowSnapshot>> = [
      { phase: "cancelled", statusText: "Скачивание отменено" },
      { phase: "expired", errorText: "Это скачивание устарело." },
      { phase: "download_failed", errorText: "Не удалось подготовить файл." },
      { phase: "network_error", errorText: "Connection lost." },
      { phase: "downloading", progressStage: "cancelled" },
      { phase: "downloading", progressStage: "failed" },
    ];
    for (const partial of terminals) {
      renderFlow(document, snapshot(partial));
      expect(el("[data-flow-progress]").hidden).toBe(true);
      expect(el("[data-flow-loader]").hidden).toBe(true);
      expect(el("[data-flow-progress-label]").textContent).toBe("");
    }
    renderFlow(
      document,
      snapshot({ phase: "expired", errorText: "Это скачивание устарело." }),
    );
    expect(el("[data-flow-status]").textContent).toBe("Это скачивание устарело.");
    expect(el("[data-flow-status]").dataset.tone).toBe("error");
  });

  it("keeps tokens, links and raw markup out of the progress and quality UI", () => {
    mountFlow();
    const token = generateAccessToken();
    const hostile: MediaFormat = { ...progressiveFormat };
    renderFlow(
      document,
      snapshot({
        phase: "downloading",
        progressStage: "downloading_video",
        result: {
          ...RESULT,
          title: "<img src=x onerror=alert(1)>",
          formats: [hostile],
        },
        formats: [hostile],
        selectedFormatId: OPTION_ID,
        canCancelTask: true,
        canStartOver: true,
      }),
    );
    const progressText = el("[data-flow-progress]").textContent ?? "";
    expect(progressText).not.toContain("vk.com");
    expect(progressText).not.toContain(token);
    expect(progressText).not.toContain(OPTION_ID);
    expect(el("[data-flow-title]").innerHTML).toBe(
      "&lt;img src=x onerror=alert(1)&gt;",
    );
    expect(document.querySelector("[data-flow-meta] img")).toBeNull();
    expect(document.body.innerHTML).not.toContain(token);
  });

  it("does not repaint progress for a stale generation", async () => {
    mountFlow();
    let release = (): void => undefined;
    const gate = new Promise<void>((resolve) => {
      release = () => resolve();
    });
    const api = {
      createInspectionJob: vi.fn(async () => parseInspectionJob(inspectionPayload())),
      getInspectionJob: vi.fn(async () => parseInspectionJob(inspectedPayload())),
      createDownloadJob: vi.fn(async () =>
        parseDownloadJob(downloadPayload({ state: "queued" })),
      ),
      getDownloadJob: vi.fn(async () => {
        await gate;
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
    let renders = 0;
    const controller = new MediaFlowController({
      api: api as unknown as MediaApi,
      session: new FlowSession({
        getItem: () => null,
        setItem: () => undefined,
        removeItem: () => undefined,
      }),
      generateToken: () => generateAccessToken(),
      pickerSupported: () => true,
      secureContext: () => true,
      documentHidden: () => false,
      onChange: (snap) => {
        renders += 1;
        renderFlow(document, snap);
      },
    });
    await controller.submit("https://vk.com/video-1_2");
    const pending = controller.enqueueDownload();
    await Promise.resolve();
    await Promise.resolve();
    expect(el("[data-flow-progress]").hidden).toBe(false);
    controller.startOver();
    expect(el("[data-flow-progress]").hidden).toBe(true);
    const before = renders;
    release();
    await pending;
    expect(renders).toBe(before);
    expect(el("[data-flow-progress]").hidden).toBe(true);
    expect(el("[data-flow-progress-label]").textContent).toBe("");
    expect(controller.snapshot().phase).toBe("idle");
  });

  it("does not repaint progress from a malformed poll payload", async () => {
    mountFlow();
    let reads = 0;
    const api = {
      createInspectionJob: vi.fn(async () => parseInspectionJob(inspectionPayload())),
      getInspectionJob: vi.fn(async () => parseInspectionJob(inspectedPayload())),
      createDownloadJob: vi.fn(async () =>
        parseDownloadJob(downloadPayload({ state: "downloading" })),
      ),
      getDownloadJob: vi.fn(async () => {
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
      }),
      cancelDownloadJob: vi.fn(),
      createBrowserGrant: vi.fn(async () => parseBrowserGrant(browserGrantPayload())),
    };
    const labels: string[] = [];
    const controller = new MediaFlowController({
      api: api as unknown as MediaApi,
      session: new FlowSession({
        getItem: () => null,
        setItem: () => undefined,
        removeItem: () => undefined,
      }),
      generateToken: () => generateAccessToken(),
      pickerSupported: () => true,
      secureContext: () => true,
      documentHidden: () => false,
      onChange: (snap) => {
        renderFlow(document, snap);
        labels.push(el("[data-flow-progress-label]").textContent ?? "");
      },
    });
    await controller.submit("https://vk.com/video-1_2");
    await controller.enqueueDownload();
    expect(reads).toBeGreaterThanOrEqual(2);
    expect(controller.snapshot().phase).toBe("ready");
    expect(controller.snapshot().errorText).toBeNull();
    expect(labels).not.toContain("Не удалось подготовить файл.");
    expect(el("[data-flow-progress-label]").textContent).toBe(
      "Готово к скачиванию · 11.4 МБ",
    );
  });

  it("restores an active download into the progress card", async () => {
    mountFlow();
    const token = generateAccessToken();
    const store = new Map<string, string>();
    const api = {
      createInspectionJob: vi.fn(),
      getInspectionJob: vi.fn(async () => parseInspectionJob(inspectedPayload())),
      createDownloadJob: vi.fn(),
      getDownloadJob: vi.fn(async () =>
        parseDownloadJob(
          downloadPayload({
            state: "ready",
            artifactReady: true,
            completedAt: "2026-08-13T04:22:27Z",
          }),
        ),
      ),
      cancelDownloadJob: vi.fn(),
      createBrowserGrant: vi.fn(async () => parseBrowserGrant(browserGrantPayload())),
    };
    const session = new FlowSession({
      getItem: (k) => store.get(k) ?? null,
      setItem: (k, v) => {
        store.set(k, v);
      },
      removeItem: (k) => {
        store.delete(k);
      },
    });
    session.write({
      v: 2,
      token,
      mediaJobId: parseInspectionJob(inspectionPayload()).id,
      downloadJobId: parseDownloadJob(downloadPayload()).id,
      formatOptionId: OPTION_ID,
      phase: "downloading",
      expiresAt: "2099-01-01T00:00:00Z",
    });
    const controller = new MediaFlowController({
      api: api as unknown as MediaApi,
      session,
      generateToken: () => token,
      pickerSupported: () => true,
      secureContext: () => true,
      documentHidden: () => false,
      onChange: (snap) => renderFlow(document, snap),
    });
    await controller.restore();
    await vi.waitFor(() => {
      expect(controller.snapshot().canNativeDownload).toBe(true);
    });
    expect(controller.snapshot().restored).toBe(true);
    expect(controller.snapshot().phase).toBe("ready");
    expect(el("[data-flow-restored]").hidden).toBe(false);
    expect(el("[data-flow-progress]").hidden).toBe(false);
    expect(el("[data-flow-progress-label]").textContent).toBe(
      "Готово к скачиванию · 11.4 МБ",
    );
    expect(el("[data-flow-progress-bar]").getAttribute("aria-valuenow")).toBe("100");
  });
});
