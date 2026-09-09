/**
 * @vitest-environment jsdom
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { beforeEach, describe, expect, it } from "vitest";
import type { FlowSnapshot } from "./controller";
import { progressiveFormat } from "./fixtures";
import { renderFlow } from "./render";

const here = dirname(fileURLToPath(import.meta.url));

const videoOnly = {
  ...progressiveFormat,
  formatOptionId: "fmt_bbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  hasAudio: false,
  category: "video_only" as const,
  audioCodec: "none",
  mediaKind: "video_only" as const,
  freeTierEligible: false,
  requiresPremium: true,
};
const audioOnly = {
  ...progressiveFormat,
  formatOptionId: "fmt_cccccccccccccccccccccccccccc",
  width: null,
  height: null,
  fps: null,
  hasVideo: false,
  category: "audio_only" as const,
  videoCodec: "none",
  bitrateKbps: 128,
  qualityLabel: "128k",
  mediaKind: "audio_only" as const,
  freeTierEligible: false,
  requiresPremium: true,
};

function snapshot(partial: Partial<FlowSnapshot> = {}): FlowSnapshot {
  const formats = [progressiveFormat, videoOnly, audioOnly];
  return {
    phase: "inspected",
    statusText: "Выберите качество для скачивания.",
    errorText: null,
    result: {
      providerId: "vk",
      canonicalProviderUrl: "https://vk.com/video-1_2",
      mediaId: "-1_2",
      title: "Example",
      durationSeconds: 90,
      formats,
      muxingRequired: false,
    },
    formats,
    selectedFormatId: progressiveFormat.formatOptionId,
    downloadEligible: true,
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
    canSubmit: false,
    canStartOver: true,
    canCancelTask: false,
    restored: false,
    progressStage: null,
    progressPercent: null,
    artifactBytes: null,
    premiumState: "free",
    premiumStatus: { active: false },
    testCheckoutAvailable: false,
    checkoutBusy: false,
    ...partial,
  };
}

function mount(): void {
  document.body.innerHTML = `
    <p data-flow-quota hidden></p>
    <p data-flow-quota-reset hidden></p>
    <p data-flow-premium-status role="status" aria-live="polite" hidden></p>
    <fieldset data-flow-quality hidden>
      <div data-flow-formats hidden></div>
      <div data-flow-premium-checkout hidden>
        <div data-flow-free-plan-quota></div>
        <p data-flow-premium-next-step></p>
        <div data-flow-premium-test-checkout hidden>
          <span class="test-badge">ТЕСТ</span>
          <p>Тестовый режим оплаты. Не является коммерческой покупкой.</p>
          <button type="button" data-flow-premium-cta>Тестовая оплата Premium</button>
        </div>
      </div>
    </fieldset>
    <button data-flow-download></button>
  `;
}

describe("PRD2-A3.3 Premium UI", () => {
  beforeEach(mount);

  it("keeps normal video enabled and source-supported standalone formats visibly locked", () => {
    renderFlow(document, snapshot());
    const radios = [
      ...document.querySelectorAll<HTMLInputElement>("input[type=radio]"),
    ];
    expect(radios).toHaveLength(3);
    expect(radios[0]?.disabled).toBe(false);
    expect(radios.slice(1).every((radio) => radio.disabled)).toBe(true);
    const text = document.body.textContent ?? "";
    expect(text).toContain("Видео + аудио");
    expect(text).toContain("Без звуковой дорожки");
    expect(text).toContain("128 кбит/с");
    expect(text.match(/Доступно с Premium/g)).toHaveLength(2);
    expect(document.querySelector("input[aria-describedby]")).not.toBeNull();
  });

  it("keeps Premium information visible but gates the TEST CTA by the server flag", () => {
    const checkout = document.querySelector<HTMLElement>(
      "[data-flow-premium-checkout]",
    );
    const testCheckout = document.querySelector<HTMLElement>(
      "[data-flow-premium-test-checkout]",
    );
    renderFlow(document, snapshot({ testCheckoutAvailable: false }));
    expect(checkout?.hidden).toBe(false);
    expect(testCheckout?.hidden).toBe(true);

    const css = readFileSync(join(here, "../../styles/global.css"), "utf8");
    expect(css).toMatch(
      /\.premium-checkout\[hidden\],[\s\S]*?\{[\s\S]*?display:\s*none;/,
    );

    renderFlow(document, snapshot({ testCheckoutAvailable: true }));
    expect(checkout?.hidden).toBe(false);
    expect(testCheckout?.hidden).toBe(false);
    expect(checkout?.textContent).toContain("ТЕСТ");
    expect(checkout?.textContent).toContain("Не является коммерческой покупкой");
    expect(checkout?.textContent).not.toContain("1 ₽");

    renderFlow(
      document,
      snapshot({
        premiumState: "active",
        premiumStatus: {
          active: true,
          expiresAt: "2026-09-08T12:00:00Z",
          productCode: "premium_24h",
          remainingSeconds: 86_400,
        },
        testCheckoutAvailable: true,
      }),
    );
    expect(checkout?.hidden).toBe(true);
  });

  it("unlocks standalone options from backend Premium state even with checkout disabled", () => {
    renderFlow(
      document,
      snapshot({
        premiumState: "active",
        premiumStatus: {
          active: true,
          expiresAt: "2026-09-08T12:00:00Z",
          productCode: "premium_24h",
          remainingSeconds: 86_400,
        },
        testCheckoutAvailable: false,
        freeQuota: {
          tier: "premium",
          downloadLimit: null,
          downloadsUsed: null,
          downloadsReserved: null,
          downloadsRemaining: null,
          resetAt: null,
          windowSeconds: null,
          premiumExpiresAt: "2026-09-08T12:00:00Z",
        },
      }),
    );
    expect(
      [...document.querySelectorAll<HTMLInputElement>("input[type=radio]")].every(
        (radio) => !radio.disabled,
      ),
    ).toBe(true);
    expect(
      document.querySelector<HTMLElement>("[data-flow-premium-checkout]")?.hidden,
    ).toBe(true);
    expect(document.querySelector("[data-flow-premium-status]")?.textContent).toContain(
      "Premium активен · Доступ до",
    );
    expect(document.querySelector("[data-flow-quota]")?.textContent).toBe(
      "Без лимита загрузок",
    );
    expect(document.body.textContent).not.toMatch(/null|NaN|999999/);
  });

  it("keeps loading and lookup errors explicit instead of silently presenting Free", () => {
    renderFlow(
      document,
      snapshot({
        premiumState: "loading",
        premiumStatus: null,
        testCheckoutAvailable: true,
      }),
    );
    expect(document.querySelector("[data-flow-premium-status]")?.textContent).toBe(
      "Проверяем статус Premium…",
    );
    expect(
      document.querySelector<HTMLElement>("[data-flow-premium-checkout]")?.hidden,
    ).toBe(true);
    renderFlow(
      document,
      snapshot({
        premiumState: "error",
        premiumStatus: null,
        testCheckoutAvailable: true,
      }),
    );
    expect(document.querySelector("[data-flow-premium-status]")?.textContent).toBe(
      "Не удалось проверить статус Premium.",
    );
    expect(
      document.querySelector<HTMLElement>("[data-flow-premium-checkout]")?.hidden,
    ).toBe(true);
  });

  it("keeps Premium visible without inventing standalone locks", () => {
    const formats = [
      progressiveFormat,
      { ...progressiveFormat, formatOptionId: videoOnly.formatOptionId, height: 480 },
    ];
    renderFlow(
      document,
      snapshot({
        formats,
        result: { ...snapshot().result!, formats },
        testCheckoutAvailable: true,
      }),
    );
    expect(document.body.textContent).not.toContain("Доступно в Premium");
    expect(
      document.querySelector<HTMLElement>("[data-flow-premium-checkout]")?.hidden,
    ).toBe(false);
  });

  it("shows the Premium next step when Free quota is exhausted", () => {
    renderFlow(
      document,
      snapshot({
        testCheckoutAvailable: true,
        freeQuota: {
          tier: "free",
          downloadLimit: 3,
          downloadsUsed: 3,
          downloadsReserved: 0,
          downloadsRemaining: 0,
          windowSeconds: 86_400,
          premiumExpiresAt: null,
          resetAt: "2026-09-09T12:00:00Z",
        },
      }),
    );
    expect(document.querySelector("[data-flow-quota]")?.textContent).toContain(
      "Лимит бесплатных загрузок исчерпан",
    );
    expect(document.querySelector("[data-flow-free-plan-quota]")?.textContent).toBe(
      "3 загрузки за 24 ч",
    );
    expect(document.querySelector("[data-flow-premium-next-step]")?.textContent).toContain(
      "Оформите Premium на 24 часа",
    );
    expect(
      document.querySelector<HTMLElement>("[data-flow-premium-test-checkout]")?.hidden,
    ).toBe(false);
  });

  it("contains the existing 390px-safe responsive treatment for Premium controls", () => {
    const css = readFileSync(join(here, "../../styles/global.css"), "utf8");
    expect(css).toContain("@media (max-width: 640px)");
    expect(css).toMatch(/\.premium-checkout \.btn\s*\{[\s\S]*?width: 100%/);
    expect(css).toMatch(/\.format \.premium-badge\s*\{[\s\S]*?grid-column: 2 \/ -1/);
  });
});
