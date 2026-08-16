/**
 * @vitest-environment jsdom
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";
import { renderFlow } from "./render";
import { isStaleDownloadPoll, type FlowSnapshot } from "./controller";
import { progressView, STAGE_COMPLETION } from "./progress";
import { UNKNOWN_SIZE_LABEL } from "./bytes";
import {
  contentDispositionHeader,
  parseContentDispositionFilename,
  saveArtifactStream,
  suggestedFilename,
} from "./download";
import {
  DOWNLOAD_ID,
  EXPIRES,
  downloadPayload,
  progressiveFormat,
} from "./fixtures";
import { parseDownloadJob, type MediaFormat } from "./contracts";
import { FlowError } from "./errors";

const here = dirname(fileURLToPath(import.meta.url));
const astroSource = readFileSync(
  join(here, "../../components/MediaFlow.astro"),
  "utf8",
);

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

function mountFlow(): void {
  document.body.innerHTML = `
    <section class="flow" data-media-flow>
      <p class="status" data-flow-status aria-live="polite"></p>
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
      </section>
      <p class="hint" data-flow-mux hidden></p>
      <button data-flow-download hidden>Prepare download</button>
      <a data-flow-native-download hidden download>Download file</a>
      <button data-flow-save-as hidden>Save as…</button>
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

describe("PR12 size, filename, percent, and placeholder", () => {
  it("pins the exact URL placeholder string", () => {
    expect(astroSource).toContain('placeholder="https://…"');
    expect(astroSource).not.toContain("vk.com/video");
  });

  it("shows approximate size or the unknown-size message in quality rows", () => {
    mountFlow();
    const formats = [progressiveFormat, alternateFormat];
    renderFlow(
      document,
      snapshot({
        phase: "inspected",
        result: { ...RESULT, formats },
        formats,
        selectedFormatId: progressiveFormat.formatOptionId,
        downloadEligible: true,
      }),
    );
    expect(el(".format-detail").textContent).toContain("≈ 11.4 МБ");
    const unknown: MediaFormat = { ...progressiveFormat, approxBytes: null };
    const unknownFormats = [unknown, alternateFormat];
    renderFlow(
      document,
      snapshot({
        phase: "inspected",
        result: { ...RESULT, formats: unknownFormats },
        formats: unknownFormats,
        selectedFormatId: unknown.formatOptionId,
        downloadEligible: true,
      }),
    );
    expect(el(".format-detail").textContent).toContain(UNKNOWN_SIZE_LABEL);
    expect(el(".format-detail").textContent).not.toContain("≈");
  });

  it("shows the exact prepared size after ready without an approximate sign", () => {
    mountFlow();
    renderFlow(
      document,
      snapshot({
        phase: "ready",
        progressStage: "ready",
        artifactBytes: 12_000_000,
        result: RESULT,
        formats: [progressiveFormat],
      }),
    );
    expect(el("[data-flow-progress-label]").textContent).toBe("Готово к скачиванию · 11.4 МБ");
    expect(el("[data-flow-progress-label]").textContent).not.toContain("≈");
  });

    it("includes a real percentage only when the backend supplies one", () => {
    const withPercent = progressView("downloading", "downloading_video", {
      progressPercent: 42,
    });
    expect(withPercent.label).toBe("Скачиваем файл (42%)");
    expect(withPercent.valueText).toBe("Скачиваем файл (42%)");
    const without = progressView("downloading", "downloading_video", {
      progressPercent: null,
    });
    expect(without.label).toBe("Скачиваем файл…");
    expect(without.label).not.toMatch(/\(\d+%\)/);
    const audio = progressView("downloading", "downloading_audio", {
      progressPercent: 18,
    });
    expect(audio.label).toBe("Скачиваем звук (18%)");
  });

  it("does not change a direct download label because of an unrelated video_only option", () => {
    const videoOnly: MediaFormat = {
      ...progressiveFormat,
      formatOptionId: "fmt_bbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      category: "video_only",
      hasAudio: false,
    };
    const direct = progressView("downloading", "downloading_video", {
      progressPercent: 42,
      formats: [progressiveFormat, videoOnly],
    });
    expect(direct.label).toBe("Скачиваем файл (42%)");
    expect(direct.label).not.toContain("Downloading video");
    const unrelated_video_only_changes_direct_label = direct.label.includes("video");
    expect(unrelated_video_only_changes_direct_label).toBe(false);
    const direct_download_label_accurate = direct.label === "Скачиваем файл (42%)";
    expect(direct_download_label_accurate).toBe(true);
  });

  it("does not claim that the percent is not a download percentage", () => {
    expect(astroSource).toContain(
      "Прогресс следует этапам подготовки. Проценты скачивания появляются, когда известен примерный размер.",
    );
    expect(astroSource).not.toMatch(/not a download percentage/i);
    const ui_claims_percent_is_not_download_percent = /not a download percentage/i.test(
      astroSource,
    );
    expect(ui_claims_percent_is_not_download_percent).toBe(false);
    const progress_copy_matches_backend_semantics = astroSource.includes(
      "Проценты скачивания появляются, когда известен примерный размер",
    );
    expect(progress_copy_matches_backend_semantics).toBe(true);
  });

  it("interpolates the stage bar from a real percent and never uses a timer", () => {
    const start = progressView("downloading", "downloading_video", {
      progressPercent: null,
    }).percent;
    const mid = progressView("downloading", "downloading_video", {
      progressPercent: 50,
    }).percent;
    const end = progressView("downloading", "downloading_video", {
      progressPercent: 99,
    }).percent;
    expect(start).toBe(STAGE_COMPLETION.inspecting);
    expect(mid).toBeGreaterThan(start);
    expect(mid).toBeLessThan(end);
    expect(end).toBe(STAGE_COMPLETION.downloading_video);
    expect(progressView.toString()).not.toMatch(/setInterval|setTimeout/);
  });

  it("resets sub-progress on retry", () => {
    const downloading = progressView("downloading", "downloading_video", {
      progressPercent: 80,
    });
    const retrying = progressView("downloading", "retrying", { progressPercent: null });
    expect(retrying.percent).toBeLessThan(downloading.percent);
    expect(retrying.label).toBe("Повторяем подготовку…");
  });

  it("keeps accessibility attributes on interpolated download progress", () => {
    mountFlow();
    renderFlow(
      document,
      snapshot({
        phase: "downloading",
        progressStage: "downloading_video",
        progressPercent: 42,
        busy: true,
      }),
    );
    const bar = el("[data-flow-progress-bar]");
    expect(bar.getAttribute("role")).toBe("progressbar");
    expect(bar.getAttribute("aria-valuetext")).toBe("Скачиваем файл (42%)");
    expect(Number(bar.getAttribute("aria-valuenow"))).toBeGreaterThan(0);
    expect(el("[data-flow-loader]").hidden).toBe(false);
  });

  it("does not show an idle loader", () => {
    mountFlow();
    renderFlow(document, snapshot());
    expect(el("[data-flow-loader]").hidden).toBe(true);
    expect(el("[data-flow-progress]").hidden).toBe(true);
  });

  it("passes a Unicode suggested filename to the picker", async () => {
    const name = "Название видео.mp4";
    const ascii = suggestedFilename(DOWNLOAD_ID, "mp4");
    let suggestedName = "";
    const writable = {
      write: async () => undefined,
      close: vi.fn(async () => undefined),
      abort: vi.fn(async () => undefined),
    };
    await saveArtifactStream({
      downloadJobId: DOWNLOAD_ID,
      token: "tokentokentokentokentokentokentoken12",
      container: "mp4",
      suggestedFilename: name,
      signal: new AbortController().signal,
      deps: {
        origin: "http://localhost",
        showSaveFilePicker: async (options) => {
          suggestedName = options.suggestedName;
          return { createWritable: async () => writable };
        },
        fetchImpl: (async () =>
          new Response(new Uint8Array([1, 2, 3]), {
            status: 200,
            headers: {
              "Content-Type": "video/mp4",
              "Content-Length": "3",
              "Content-Disposition": contentDispositionHeader(name, ascii),
            },
          })) as unknown as typeof fetch,
      },
    });
    expect(suggestedName).toBe(name);
    expect(writable.close).toHaveBeenCalledOnce();
  });

  it("rejects malformed, duplicate, and mismatched Content-Disposition headers", () => {
    const expected = "Название видео.mp4";
    const ascii = suggestedFilename(DOWNLOAD_ID, "mp4");
    const good = contentDispositionHeader(expected, ascii);
    expect(parseContentDispositionFilename(good, expected)).toBe(expected);
    expect(
      parseContentDispositionFilename(`${good}; filename*=UTF-8''dup`, expected),
    ).toBeNull();
    expect(
      parseContentDispositionFilename(
        `attachment; filename="${ascii}"; filename*=UTF-8''%ZZ`,
        expected,
      ),
    ).toBeNull();
    expect(
      parseContentDispositionFilename(
        `attachment; filename="${ascii}"; filename*=UTF-8''other.mp4`,
        expected,
      ),
    ).toBeNull();
    expect(
      parseContentDispositionFilename(`attachment; filename="${ascii}"`, expected),
    ).toBeNull();
  });

  it("cancels the body when filename* does not match the job name", async () => {
    const cancel = vi.fn(async () => undefined);
    const releaseLock = vi.fn(() => undefined);
    const abort = vi.fn(async () => undefined);
    const ascii = suggestedFilename(DOWNLOAD_ID, "mp4");
    await expect(
      saveArtifactStream({
        downloadJobId: DOWNLOAD_ID,
        token: "tokentokentokentokentokentokentoken12",
        container: "mp4",
        suggestedFilename: "Название видео.mp4",
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
            ({
              status: 200,
              headers: new Headers({
                "Content-Type": "video/mp4",
                "Content-Length": "3",
                "Content-Disposition": contentDispositionHeader(ascii, ascii),
              }),
              body: {
                getReader: () => ({
                  read: async () => ({ done: true, value: undefined }),
                  cancel,
                  releaseLock,
                }),
              },
            }) as unknown as Response) as unknown as typeof fetch,
        },
      }),
    ).rejects.toBeInstanceOf(FlowError);
    expect(cancel).toHaveBeenCalledOnce();
    expect(releaseLock).toHaveBeenCalledOnce();
    expect(abort).toHaveBeenCalledOnce();
  });

  it("ignores a stale poll with an older updatedAt", () => {
    const current = parseDownloadJob(
      downloadPayload({
        state: "downloading",
        progressStage: "downloading_video",
        progressPercent: 80,
        updatedAt: "2026-08-13T04:22:40Z",
        attempt: 1,
      }),
    );
    const stale = parseDownloadJob(
      downloadPayload({
        state: "downloading",
        progressStage: "downloading_video",
        progressPercent: 10,
        updatedAt: "2026-08-13T04:22:30Z",
        attempt: 1,
      }),
    );
    const retry = parseDownloadJob(
      downloadPayload({
        state: "queued",
        progressStage: "retrying",
        nextAttemptAt: EXPIRES,
        progressPercent: null,
        updatedAt: "2026-08-13T04:22:41Z",
        attempt: 2,
      }),
    );
    expect(isStaleDownloadPoll(stale, current)).toBe(true);
    expect(isStaleDownloadPoll(retry, current)).toBe(false);
  });
});
