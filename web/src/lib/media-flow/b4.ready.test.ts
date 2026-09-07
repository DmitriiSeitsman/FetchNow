/**
 * @vitest-environment jsdom
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { generateAccessToken } from "./credentials";
import { formatApproxBytes, formatByteSize, formatExactBytes } from "./bytes";
import {
  parseBrowserGrant,
  parseDownloadJob,
  parseInspectionJob,
  type MediaFormat,
} from "./contracts";
import { MediaFlowController, type FlowSnapshot } from "./controller";
import {
  estimateDownloadSeconds,
  formatDeliverySpeed,
  formatEstimatedDownloadTime,
  pluralizeRussian,
} from "./estimate";
import {
  browserGrantPayload,
  downloadPayload,
  GRANT_ID,
  inspectionPayload,
  inspectedPayload,
  OPTION_ID,
} from "./fixtures";
import { renderFlow } from "./render";
import { FlowSession } from "./session";
import { FLOW_PHASES } from "./state-machine";
import type { MediaApi } from "./api";

const ASTRO_PATH = resolve(
  __dirname,
  "../../components/MediaFlow.astro",
);
const astroSource = readFileSync(ASTRO_PATH, "utf-8");

const SAMPLE_FORMAT: MediaFormat = {
  formatOptionId: OPTION_ID,
  container: "mp4",
  width: 1920,
  height: 1080,
  fps: 30,
  hasVideo: true,
  hasAudio: true,
  category: "progressive",
  videoCodec: "avc",
  audioCodec: "aac",
  approxBytes: 1_400_000_000,
  qualityLabel: "1080p",
  freeTierEligible: true,
  mediaKind: "normal_video",
  requiresPremium: false,
  bitrateKbps: null,
};

function baseSnapshot(overrides: Partial<FlowSnapshot> = {}): FlowSnapshot {
  return {
    phase: "idle",
    statusText: "",
    errorText: null,
    result: null,
    formats: [],
    selectedFormatId: null,
    selectedFormat: null,
    deliveryRateBytesPerSecond: null,
    downloadEligible: false,
    canSelectQuality: true,
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
    ...overrides,
  };
}

function mountFlowMarkup(): void {
  document.body.innerHTML = `
    <section class="flow" data-media-flow>
      <form data-flow-form>
        <input data-flow-url type="url" value="" />
        <button type="submit" data-flow-submit>Fetch</button>
        <button type="button" data-flow-reset hidden>Start over</button>
      </form>
      <p class="status" data-flow-status aria-live="polite"></p>
      <p class="hint" data-flow-restored hidden>Restored</p>
      <article class="meta-card" data-flow-meta hidden>
        <h2 data-flow-title></h2>
        <span data-flow-provider></span>
        <span data-flow-duration></span>
      </article>
      <fieldset class="quality-card" data-flow-quality hidden>
        <legend>Quality</legend>
        <div class="quality-list" data-flow-formats hidden></div>
      </fieldset>
      <section class="progress-card" data-flow-progress hidden>
        <span class="loader" data-flow-loader hidden>
          <span class="spinner" data-flow-spinner></span>
        </span>
        <p class="progress-stage" data-flow-progress-label></p>
        <div class="progress-track" data-flow-progress-bar>
          <span class="progress-fill" data-flow-progress-fill></span>
        </div>
      </section>
      <section class="ready-choice" data-flow-ready hidden>
        <div class="ready-summary">
          <p class="ready-summary-badge" data-flow-ready-badge>✓ Готово к скачиванию</p>
          <p class="ready-summary-details" data-flow-ready-details></p>
        </div>
        <div class="ready-choice-grid">
          <article class="ready-card ready-card--free" data-flow-ready-free>
            <h3 class="ready-card-title">Бесплатно</h3>
            <div class="ready-card-body">
              <p class="ready-card-speed" data-flow-ready-speed hidden></p>
              <p class="ready-card-time" data-flow-ready-time hidden></p>
            </div>
            <div class="ready-card-actions">
              <button class="btn btn-primary" type="button" data-flow-save-as hidden>Сохранить как…</button>
              <a class="btn btn-primary" data-flow-native-download hidden download>Скачать бесплатно</a>
              <button class="btn btn-primary" type="button" data-flow-grant-retry hidden>Обновить доступ</button>
            </div>
          </article>
        </div>
      </section>
      <div class="flow-actions">
        <button class="btn btn-primary" type="button" data-flow-download hidden>Подготовить скачивание</button>
      </div>
    </section>
  `;
}

function el<T extends Element = HTMLElement>(selector: string): T {
  const node = document.querySelector<T>(selector);
  if (!node) {
    throw new Error(`Element not found: ${selector}`);
  }
  return node;
}

describe("PRD1E-B4: Size semantics (Part A)", () => {
  it("8. inspected quality size remains approximate: ≈ X МБ / ≈ X ГБ", () => {
    expect(formatApproxBytes(SAMPLE_FORMAT.approxBytes)).toBe("≈ 1.3 ГБ");
    expect(formatApproxBytes(12_000_000)).toBe("≈ 11.4 МБ");
  });

  it("9. missing estimate remains graceful with unknown size label", () => {
    expect(formatApproxBytes(null)).toBe("Размер станет известен после подготовки");
    expect(formatByteSize(-1)).toBe("Размер станет известен после подготовки");
    expect(formatByteSize(Number.NaN)).toBe("Размер станет известен после подготовки");
  });

  it("10. READY hides quality-card and format options", () => {
    mountFlowMarkup();
    renderFlow(
      document,
      baseSnapshot({
        phase: "ready",
        artifactBytes: 576_454_656,
        selectedFormat: SAMPLE_FORMAT,
        formats: [SAMPLE_FORMAT],
        canSelectQuality: true,
      }),
    );
    expect(el("[data-flow-quality]").hidden).toBe(true);
    expect(el("[data-flow-formats]").hidden).toBe(true);
  });

  it("11. READY renders exact artifactBytes in ready details", () => {
    mountFlowMarkup();
    renderFlow(
      document,
      baseSnapshot({
        phase: "ready",
        artifactBytes: 576_402_227, // 549.7 MB
        selectedFormat: SAMPLE_FORMAT,
        formats: [SAMPLE_FORMAT],
      }),
    );
    const details = el("[data-flow-ready-details]").textContent ?? "";
    expect(details).toContain("549.7 МБ");
    expect(details).toContain("MP4");
    expect(details).toContain("1080p");
  });

  it("12. READY exact size has no ≈ sign", () => {
    mountFlowMarkup();
    renderFlow(
      document,
      baseSnapshot({
        phase: "ready",
        artifactBytes: 576_402_227,
        selectedFormat: SAMPLE_FORMAT,
        formats: [SAMPLE_FORMAT],
      }),
    );
    const details = el("[data-flow-ready-details]").textContent ?? "";
    expect(details).not.toContain("≈");
    expect(formatExactBytes(576_402_227)).toBe("549.7 МБ");
    expect(formatExactBytes(576_402_227)).not.toContain("≈");
  });

  it("13. stale preflight approx size is not visible anywhere in READY", () => {
    mountFlowMarkup();
    renderFlow(
      document,
      baseSnapshot({
        phase: "ready",
        artifactBytes: 576_402_227, // 549.7 MB
        selectedFormat: SAMPLE_FORMAT, // approxBytes: 1.4 GB
        formats: [SAMPLE_FORMAT],
      }),
    );
    const text = document.body.textContent ?? "";
    expect(text).not.toContain("1.3 ГБ");
    expect(text).not.toContain("1.4 ГБ");
    expect(text).toContain("549.7 МБ");
  });

  it("14. reload/restore READY still uses artifactBytes", async () => {
    mountFlowMarkup();
    const token = generateAccessToken();
    const store = new Map<string, string>();
    const downloadJob = parseDownloadJob(
      downloadPayload({
        state: "ready",
        artifactReady: true,
        artifactBytes: 576_402_227,
        deliveryRateBytesPerSecond: 524288,
        selectedFormat: SAMPLE_FORMAT,
        completedAt: "2026-08-13T04:22:27Z",
      }),
    );
    const api = {
      createInspectionJob: vi.fn(),
      getInspectionJob: vi.fn(
        async () =>
          parseInspectionJob(
            inspectedPayload({ formats: [SAMPLE_FORMAT] }),
          ),
      ),
      createDownloadJob: vi.fn(),
      getDownloadJob: vi.fn(async () => downloadJob),
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
      downloadJobId: downloadJob.id,
      formatOptionId: OPTION_ID,
      phase: "ready",
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
      expect(controller.snapshot().phase).toBe("ready");
    });
    expect(el("[data-flow-ready-details]").textContent).toContain("549.7 МБ");
    expect(el("[data-flow-ready-details]").textContent).not.toContain("≈");
    expect(el("[data-flow-quality]").hidden).toBe(true);
  });
});

describe("PRD1E-B4: Duration estimation & formatting (Part C)", () => {
  it("15. known size + 524288 produces expected seconds (ceil division)", () => {
    // 23_592_960 / 524288 = 45.0
    expect(estimateDownloadSeconds(23_592_960, 524288)).toBe(45);
    // 566_231_040 / 524288 = 1080.0 (18 minutes)
    expect(estimateDownloadSeconds(566_231_040, 524288)).toBe(1080);
    // 2_202_009_600 / 524288 = 4200.0 (70 minutes = 1h 10m)
    expect(estimateDownloadSeconds(2_202_009_600, 524288)).toBe(4200);
    // Ceiling test: 524289 / 524288 -> 2
    expect(estimateDownloadSeconds(524289, 524288)).toBe(2);
  });

  it("16. seconds format (< 60 seconds) with Russian declensions", () => {
    expect(pluralizeRussian(1, "секунда", "секунды", "секунд")).toBe("секунда");
    expect(pluralizeRussian(2, "секунда", "секунды", "секунд")).toBe("секунды");
    expect(pluralizeRussian(5, "секунда", "секунды", "секунд")).toBe("секунд");
    expect(formatEstimatedDownloadTime(45)).toBe(
      "Примерное время скачивания: ≈ 45 секунд",
    );
    expect(formatEstimatedDownloadTime(1)).toBe(
      "Примерное время скачивания: ≈ 1 секунда",
    );
    expect(formatEstimatedDownloadTime(2)).toBe(
      "Примерное время скачивания: ≈ 2 секунды",
    );
    expect(formatEstimatedDownloadTime(21)).toBe(
      "Примерное время скачивания: ≈ 21 секунда",
    );
    expect(formatEstimatedDownloadTime(24)).toBe(
      "Примерное время скачивания: ≈ 24 секунды",
    );
    expect(formatEstimatedDownloadTime(25)).toBe(
      "Примерное время скачивания: ≈ 25 секунд",
    );
  });

  it("17. minutes format (1–59 minutes) with Russian declensions", () => {
    expect(formatEstimatedDownloadTime(1080)).toBe(
      "Примерное время скачивания: ≈ 18 минут",
    );
    expect(formatEstimatedDownloadTime(60)).toBe(
      "Примерное время скачивания: ≈ 1 минута",
    );
    expect(formatEstimatedDownloadTime(120)).toBe(
      "Примерное время скачивания: ≈ 2 минуты",
    );
    expect(formatEstimatedDownloadTime(1260)).toBe(
      "Примерное время скачивания: ≈ 21 минута",
    );
  });

  it("18. hours/minutes format (>= 60 minutes)", () => {
    expect(formatEstimatedDownloadTime(4200)).toBe(
      "Примерное время скачивания: ≈ 1 ч 10 мин",
    );
    expect(formatEstimatedDownloadTime(3600)).toBe(
      "Примерное время скачивания: ≈ 1 ч",
    );
    expect(formatEstimatedDownloadTime(7200)).toBe(
      "Примерное время скачивания: ≈ 2 ч",
    );
    expect(formatEstimatedDownloadTime(7260)).toBe(
      "Примерное время скачивания: ≈ 2 ч 1 мин",
    );
  });

  it("19. rate null -> estimated-time line omitted", () => {
    expect(estimateDownloadSeconds(576_454_656, null)).toBeNull();
    expect(formatEstimatedDownloadTime(null)).toBeNull();
    mountFlowMarkup();
    renderFlow(
      document,
      baseSnapshot({
        phase: "ready",
        artifactBytes: 576_454_656,
        deliveryRateBytesPerSecond: null,
      }),
    );
    expect(el("[data-flow-ready-time]").hidden).toBe(true);
    expect(el("[data-flow-ready-time]").textContent).toBe("");
  });

  it("20. rate <= 0 -> safe handling", () => {
    expect(estimateDownloadSeconds(576_454_656, 0)).toBeNull();
    expect(estimateDownloadSeconds(576_454_656, -500)).toBeNull();
    expect(formatDeliverySpeed(0)).toBeNull();
    expect(formatDeliverySpeed(-500)).toBeNull();
  });

  it("21. invalid artifact size -> safe handling", () => {
    expect(estimateDownloadSeconds(0, 524288)).toBeNull();
    expect(estimateDownloadSeconds(-100, 524288)).toBeNull();
    expect(estimateDownloadSeconds(Number.NaN, 524288)).toBeNull();
  });

  it("22. frontend source files do not contain a magic 524288 constant for policy", () => {
    const renderSrc = readFileSync(
      resolve(__dirname, "render.ts"),
      "utf-8",
    );
    const controllerSrc = readFileSync(
      resolve(__dirname, "controller.ts"),
      "utf-8",
    );
    const estimateSrc = readFileSync(
      resolve(__dirname, "estimate.ts"),
      "utf-8",
    );
    expect(renderSrc).not.toContain("524288");
    expect(controllerSrc).not.toContain("524288");
    expect(estimateSrc).not.toContain("524288");
  });
});

describe("PRD1E-B4: Speed copy formatting (Part D)", () => {
  it("23. 524288 -> 'Скорость до 0,5 МБ/с'", () => {
    expect(formatDeliverySpeed(524288)).toBe("Скорость до 0,5 МБ/с");
  });

  it("24. 1048576 -> 'Скорость до 1 МБ/с'", () => {
    expect(formatDeliverySpeed(1048576)).toBe("Скорость до 1 МБ/с");
    expect(formatDeliverySpeed(2097152)).toBe("Скорость до 2 МБ/с");
    expect(formatDeliverySpeed(1572864)).toBe("Скорость до 1,5 МБ/с");
  });

  it("25. null -> speed restriction line omitted", () => {
    expect(formatDeliverySpeed(null)).toBeNull();
    mountFlowMarkup();
    renderFlow(
      document,
      baseSnapshot({
        phase: "ready",
        artifactBytes: 576_454_656,
        deliveryRateBytesPerSecond: null,
      }),
    );
    expect(el("[data-flow-ready-speed]").hidden).toBe(true);
    expect(el("[data-flow-ready-speed]").textContent).toBe("");
  });
});

describe("PRD1E-B4: Ready UI & interaction (Part E)", () => {
  it("26. not-ready states do not show READY choice section", () => {
    mountFlowMarkup();
    const nonReadyPhases = FLOW_PHASES.filter((p) => p !== "ready");
    for (const phase of nonReadyPhases) {
      renderFlow(document, baseSnapshot({ phase }));
      expect(el("[data-flow-ready]").hidden, `phase ${phase}`).toBe(true);
    }
  });

  it("27. READY shows Free card with speed, time, and CTA", () => {
    mountFlowMarkup();
    renderFlow(
      document,
      baseSnapshot({
        phase: "ready",
        artifactBytes: 566_231_040,
        deliveryRateBytesPerSecond: 524288,
        selectedFormat: SAMPLE_FORMAT,
        canNativeDownload: true,
        downloadHref: `/api/v1/media/browser-grants/${GRANT_ID}/content`,
      }),
    );
    expect(el("[data-flow-ready]").hidden).toBe(false);
    expect(el("[data-flow-ready-free]").hidden).toBe(false);
    expect(el("[data-flow-ready-speed]").hidden).toBe(false);
    expect(el("[data-flow-ready-speed]").textContent).toBe("Скорость до 0,5 МБ/с");
    expect(el("[data-flow-ready-time]").hidden).toBe(false);
    expect(el("[data-flow-ready-time]").textContent).toBe(
      "Примерное время скачивания: ≈ 18 минут",
    );
    const link = el<HTMLAnchorElement>("[data-flow-native-download]");
    expect(link.hidden).toBe(false);
    expect(link.textContent).toBe("Скачать бесплатно");
    expect(link.href).toContain(`/api/v1/media/browser-grants/${GRANT_ID}/content`);
  });

  it("28. Free CTA issues/uses browser grant via existing flow", () => {
    mountFlowMarkup();
    renderFlow(
      document,
      baseSnapshot({
        phase: "ready",
        canNativeDownload: true,
        downloadHref: `/api/v1/media/browser-grants/${GRANT_ID}/content`,
      }),
    );
    const link = el<HTMLAnchorElement>("[data-flow-native-download]");
    expect(link.tagName).toBe("A");
    expect(link.getAttribute("download")).not.toBeNull();
    expect(link.getAttribute("href")).toBe(
      `/api/v1/media/browser-grants/${GRANT_ID}/content`,
    );
  });

  it("29. duplicate click protection preserved (handoff marks link and updates label)", () => {
    mountFlowMarkup();
    renderFlow(
      document,
      baseSnapshot({
        phase: "ready",
        canNativeDownload: true,
        nativeDownloadHandoff: true,
        downloadHref: `/api/v1/media/browser-grants/${GRANT_ID}/content`,
      }),
    );
    const link = el<HTMLAnchorElement>("[data-flow-native-download]");
    expect(link.textContent).toBe("Скачать снова");
  });

  it("30. expired artifact cannot download and displays error", () => {
    mountFlowMarkup();
    renderFlow(
      document,
      baseSnapshot({
        phase: "expired",
        errorText: "Это скачивание устарело.",
      }),
    );
    expect(el("[data-flow-ready]").hidden).toBe(true);
    expect(el("[data-flow-status]").textContent).toBe("Это скачивание устарело.");
    expect(el("[data-flow-native-download]").hidden).toBe(true);
  });

  it("31. no Premium CTA/card is rendered in Astro markup or rendered DOM", () => {
    mountFlowMarkup();
    renderFlow(
      document,
      baseSnapshot({
        phase: "ready",
        artifactBytes: 576_454_656,
        deliveryRateBytesPerSecond: 524288,
        selectedFormat: SAMPLE_FORMAT,
      }),
    );
    expect(document.querySelector(".ready-card--premium")).toBeNull();
    expect(document.querySelector("[data-flow-premium]")).toBeNull();
    expect(document.body.textContent).not.toContain("Premium");
    expect(document.body.textContent).not.toContain("Премиум");
    expect(astroSource).not.toContain("ready-card--premium");
    expect(astroSource).not.toContain("data-flow-premium");
  });

  it("32. no fake payment action exists in rendered DOM or markup", () => {
    mountFlowMarkup();
    renderFlow(
      document,
      baseSnapshot({
        phase: "ready",
        artifactBytes: 576_454_656,
        deliveryRateBytesPerSecond: 524288,
      }),
    );
    expect(document.querySelector("button:disabled.btn-premium")).toBeNull();
    expect(document.querySelector("[data-payment]")).toBeNull();
    expect(astroSource).not.toContain("data-payment");
    expect(astroSource).not.toContain("robokassa");
  });
});
