/**
 * @vitest-environment jsdom
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MediaApi } from "./api";
import { parseBrowserGrant, parseDownloadJob, parseInspectionJob } from "./contracts";
import type { PaymentProductSummary } from "./contracts";
import {
  MAX_PREMIUM_UPGRADE_ATTEMPTS,
  MediaFlowController,
  type FlowSnapshot,
} from "./controller";
import { generateAccessToken } from "./credentials";
import { flowErrorFromCode } from "./errors";
import { PREMIUM_HIGHLIGHT_ETA_SECONDS } from "./estimate";
import {
  BROWSER_GRANT_PATH,
  browserGrantPayload,
  downloadPayload,
  inspectedPayload,
  inspectionPayload,
  OPTION_ID,
  progressiveFormat,
} from "./fixtures";
import { renderFlow } from "./render";
import { FlowSession } from "./session";

const here = dirname(fileURLToPath(import.meta.url));
const astroSource = readFileSync(
  join(here, "../../components/MediaFlow.astro"),
  "utf8",
);

const PRODUCT: PaymentProductSummary = {
  productCode: "premium_24h",
  amountMinor: 9900,
  currency: "RUB",
  entitlementDurationSeconds: 86_400,
};

/** 900 s at 512 KiB/s: the exact ETA where the Premium card earns emphasis. */
const SLOW_ARTIFACT_BYTES = PREMIUM_HIGHLIGHT_ETA_SECONDS * 524_288;

function snapshot(partial: Partial<FlowSnapshot> = {}): FlowSnapshot {
  return {
    phase: "ready",
    statusText: "",
    errorText: null,
    result: null,
    formats: [],
    selectedFormatId: null,
    selectedFormat: progressiveFormat,
    deliveryRateBytesPerSecond: 524_288,
    downloadEligible: false,
    muxingBlocked: false,
    httpsRequired: false,
    grantArming: false,
    canNativeDownload: true,
    canRetryGrant: false,
    downloadHref: BROWSER_GRANT_PATH,
    nativeDownloadHandoff: false,
    busy: false,
    canSubmit: false,
    canStartOver: true,
    canCancelTask: false,
    restored: false,
    progressStage: "ready",
    progressPercent: null,
    artifactBytes: 12_000_000,
    premiumState: "free",
    premiumStatus: { active: false },
    testCheckoutAvailable: true,
    paymentProduct: PRODUCT,
    checkoutBusy: false,
    premiumError: null,
    upgradePending: false,
    promoted: false,
    ...partial,
  };
}

function mountFlow(): void {
  document.body.innerHTML = `
    <section class="flow" data-media-flow>
      <p class="status" data-flow-status></p>
      <p data-flow-premium-status hidden></p>
      <fieldset data-flow-quality hidden>
        <div data-flow-formats hidden></div>
        <div data-flow-premium-checkout hidden>
          <div data-flow-free-plan-quota></div>
          <p data-flow-premium-next-step></p>
          <div data-flow-premium-test-checkout hidden>
            <button type="button" data-flow-premium-cta>Тестовая оплата Premium</button>
          </div>
          <p class="premium-error" data-flow-premium-error hidden></p>
        </div>
      </fieldset>
      <section class="ready-choice" data-flow-ready hidden>
        <p data-flow-ready-details></p>
        <div class="ready-choice-grid">
          <article class="ready-card ready-card--free" data-flow-ready-free>
            <h3 data-flow-ready-free-title>Бесплатно</h3>
            <p data-flow-ready-speed hidden></p>
            <p data-flow-ready-time hidden></p>
            <a class="btn btn-primary" data-flow-native-download hidden download></a>
            <button class="btn btn-primary" type="button" data-flow-grant-retry hidden></button>
          </article>
          <article
            class="ready-card ready-card--premium"
            data-flow-ready-premium
            data-emphasis="compact"
            hidden
          >
            <span class="test-badge" data-flow-ready-premium-test hidden>ТЕСТ</span>
            <p data-flow-ready-premium-price hidden></p>
            <ul class="ready-card-benefits">
              <li>Доступ на 24 часа</li>
              <li>Без ограничения скорости со стороны FetchNow</li>
              <li>Без лимита бесплатных загрузок</li>
            </ul>
            <button class="btn btn-primary" type="button" data-flow-ready-premium-cta hidden>
              Скачать быстрее с Premium
            </button>
            <p data-flow-ready-premium-note hidden></p>
          </article>
        </div>
        <div class="ready-premium-state">
          <p class="premium-error" data-flow-ready-premium-error hidden></p>
          <button class="btn btn-ghost" type="button" data-flow-premium-upgrade-retry hidden></button>
        </div>
      </section>
      <p data-flow-https hidden></p>
      <p data-flow-grant-pending hidden></p>
      <p data-flow-handoff hidden></p>
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

describe("PRD2-A4.2.1 READY Premium comparison", () => {
  beforeEach(mountFlow);

  it("puts Free and Premium side by side with a server-formatted price", () => {
    renderFlow(document, snapshot());
    expect(el("[data-flow-ready]").hidden).toBe(false);
    expect(el("[data-flow-ready-free]").hidden).toBe(false);
    expect(el("[data-flow-ready-premium]").hidden).toBe(false);
    const price = el("[data-flow-ready-premium-price]");
    expect(price.hidden).toBe(false);
    // 9900 minor units render as ninety-nine roubles, never as raw minor units.
    expect(price.textContent).toContain("99");
    expect(price.textContent).toContain("₽");
    expect(price.textContent).not.toContain("9900");
    expect(el("[data-flow-ready-premium-cta]").hidden).toBe(false);
    expect(el("[data-flow-ready-premium-test]").hidden).toBe(false);
    expect(el("[data-flow-ready-premium]").textContent).toContain(
      "Без ограничения скорости со стороны FetchNow",
    );
  });

  it("formats a price with kopecks without dropping them", () => {
    renderFlow(
      document,
      snapshot({ paymentProduct: { ...PRODUCT, amountMinor: 9950 } }),
    );
    const price = el("[data-flow-ready-premium-price]").textContent ?? "";
    expect(price).toMatch(/99[.,]50/);
  });

  it("emphasises Premium exactly from a 900 second estimate", () => {
    renderFlow(document, snapshot({ artifactBytes: SLOW_ARTIFACT_BYTES }));
    expect(el("[data-flow-ready-premium]").dataset.emphasis).toBe("highlight");

    renderFlow(document, snapshot({ artifactBytes: SLOW_ARTIFACT_BYTES - 524_288 }));
    expect(el("[data-flow-ready-premium]").dataset.emphasis).toBe("compact");
  });

  it("hides the purchase CTA without a product, without TEST checkout, or with Premium held", () => {
    renderFlow(document, snapshot({ paymentProduct: null }));
    expect(el("[data-flow-ready-premium]").hidden).toBe(true);
    expect(el("[data-flow-ready-premium-cta]").hidden).toBe(true);

    renderFlow(document, snapshot({ testCheckoutAvailable: false }));
    expect(el("[data-flow-ready-premium]").hidden).toBe(false);
    expect(el("[data-flow-ready-premium-cta]").hidden).toBe(true);
    expect(el("[data-flow-ready-premium-test]").hidden).toBe(true);

    renderFlow(
      document,
      snapshot({
        premiumState: "active",
        premiumStatus: {
          active: true,
          expiresAt: "2099-01-01T00:00:00Z",
          productCode: "premium_24h",
          remainingSeconds: 86_400,
        },
      }),
    );
    expect(el("[data-flow-ready-premium]").hidden).toBe(true);
    expect(el("[data-flow-ready-premium-cta]").hidden).toBe(true);
    expect(el("[data-flow-premium-status]").hidden).toBe(false);
  });

  it("keeps one primary download action and renames it once promoted", () => {
    renderFlow(document, snapshot());
    const link = el("[data-flow-native-download]");
    expect(link.textContent).toBe("Скачать бесплатно");
    expect(link.classList.contains("btn-primary")).toBe(true);
    expect(el("[data-flow-ready-free-title]").textContent).toBe("Бесплатно");

    renderFlow(document, snapshot({ nativeDownloadHandoff: true }));
    expect(el("[data-flow-native-download]").textContent).toBe("Скачать снова");

    renderFlow(document, snapshot({ promoted: true, premiumState: "active" }));
    expect(el("[data-flow-native-download]").textContent).toBe("Скачать");
    expect(el("[data-flow-native-download]").classList.contains("btn-primary")).toBe(
      true,
    );
    expect(el("[data-flow-ready-free-title]").textContent).toBe("Premium");
    expect(el("[data-flow-ready-premium]").hidden).toBe(true);
    expect(el("[data-flow-premium-upgrade-retry]").hidden).toBe(true);
  });

  it("shows an upgrade failure only next to the upgrade action", () => {
    renderFlow(
      document,
      snapshot({
        premiumState: "active",
        premiumError:
          "Скачивание уже идёт. Дождитесь его завершения и попробуйте снова.",
      }),
    );
    expect(el("[data-flow-ready-premium-error]").hidden).toBe(false);
    expect(el("[data-flow-ready-premium-error]").textContent).toContain(
      "Скачивание уже идёт",
    );
    expect(el("[data-flow-premium-error]").hidden).toBe(true);
    expect(el("[data-flow-premium-upgrade-retry]").hidden).toBe(false);
    expect(el("[data-flow-status]").textContent).toBe("");
  });

  it("keeps checkout failures beside the checkout button", () => {
    renderFlow(
      document,
      snapshot({
        phase: "inspected",
        premiumError: "Не удалось начать тестовую оплату. Попробуйте ещё раз.",
      }),
    );
    expect(el("[data-flow-premium-error]").hidden).toBe(false);
    expect(el("[data-flow-status]").textContent).toBe("");
  });

  it("declares the comparison layout in markup and CSS", () => {
    expect(astroSource).toContain("data-flow-ready-premium");
    expect(astroSource).toContain("Скачать быстрее с Premium");
    expect(astroSource).not.toContain("data-flow-save-as");
    const css = readFileSync(join(here, "../../styles/global.css"), "utf8");
    expect(css).toContain(".ready-card--premium");
    expect(css).toMatch(/@media \(min-width: 720px\)[\s\S]*?ready-choice-grid/);
    expect(css).toContain('.ready-card--premium[data-emphasis="highlight"]');
  });
});

function activePremium() {
  return {
    active: true as const,
    expiresAt: "2099-01-01T00:00:00Z",
    productCode: "premium_24h" as const,
    remainingSeconds: 86_400,
  };
}

function upgradeApi(
  overrides: Partial<Record<string, unknown>> = {},
): Record<string, unknown> {
  return {
    getFreeQuota: vi.fn(async () => null),
    getPremiumStatus: vi.fn(async () => activePremium()),
    getPaymentConfig: vi.fn(async () => ({
      testCheckoutAvailable: true,
      product: PRODUCT,
    })),
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
    upgradeDownloadJobToPremium: vi.fn(async () =>
      parseDownloadJob(
        downloadPayload({
          state: "ready",
          artifactReady: true,
          completedAt: "2026-08-13T04:22:27Z",
          deliveryRateBytesPerSecond: null,
        }),
      ),
    ),
    ...overrides,
  };
}

function makeController(api: Record<string, unknown>): MediaFlowController {
  return new MediaFlowController({
    api: api as unknown as MediaApi,
    session: new FlowSession({
      getItem: () => null,
      setItem: () => undefined,
      removeItem: () => undefined,
    }),
    generateToken: generateAccessToken,
    secureContext: () => true,
    documentHidden: () => false,
  });
}

async function reachReady(controller: MediaFlowController): Promise<void> {
  await controller.submit("https://vk.com/video-1_2");
  controller.selectFormat(OPTION_ID);
  await controller.enqueueDownload();
  await vi.waitFor(() => expect(controller.snapshot().phase).toBe("ready"));
}

describe("PRD2-A4.2.1 controller upgrade flow", () => {
  it("promotes a prepared Free job once when Premium turns out to be active", async () => {
    const api = upgradeApi();
    const controller = makeController(api);
    await reachReady(controller);
    await controller.refreshPremium();
    await vi.waitFor(() => expect(controller.snapshot().promoted).toBe(true));

    // Bounded and generation-guarded: repeated background triggers stay at one.
    await controller.refreshPremium();
    controller.onForegroundResume();
    await vi.waitFor(() => expect(controller.snapshot().canNativeDownload).toBe(true));
    expect(api.upgradeDownloadJobToPremium).toHaveBeenCalledOnce();

    const snap = controller.snapshot();
    expect(snap.upgradePending).toBe(false);
    expect(snap.premiumError).toBeNull();
    expect(snap.deliveryRateBytesPerSecond).toBeNull();
    // Re-armed, but never auto-clicked: the visitor still starts the download.
    expect(snap.nativeDownloadHandoff).toBe(false);
    expect(snap.canNativeDownload).toBe(true);
    expect(snap.downloadHref).toBe(BROWSER_GRANT_PATH);
    expect(
      (api.createBrowserGrant as ReturnType<typeof vi.fn>).mock.calls.length,
    ).toBeGreaterThanOrEqual(2);
    expect(api.getFreeQuota).toHaveBeenCalled();
  });

  it("invalidates the Free grant href before asking for the upgrade", async () => {
    const gate = { resolve: () => {} };
    const pending = new Promise<void>((resolve) => {
      gate.resolve = resolve;
    });
    const api = upgradeApi({
      upgradeDownloadJobToPremium: vi.fn(async () => {
        await pending;
        return parseDownloadJob(
          downloadPayload({
            state: "ready",
            artifactReady: true,
            completedAt: "2026-08-13T04:22:27Z",
          }),
        );
      }),
    });
    const controller = makeController(api);
    await reachReady(controller);
    const upgrading = controller.refreshPremium();
    await vi.waitFor(() => expect(controller.snapshot().upgradePending).toBe(true));
    const mid = controller.snapshot();
    expect(mid.downloadHref).toBeNull();
    expect(mid.canNativeDownload).toBe(false);
    gate.resolve();
    await upgrading;
    await vi.waitFor(() => expect(controller.snapshot().canNativeDownload).toBe(true));
    expect(controller.snapshot().promoted).toBe(true);
  });

  it("keeps the Free download and stays quiet when a background upgrade fails", async () => {
    const api = upgradeApi({
      upgradeDownloadJobToPremium: vi.fn(async () => {
        throw flowErrorFromCode("INTERNAL_ERROR");
      }),
    });
    const controller = makeController(api);
    await reachReady(controller);
    await controller.refreshPremium();
    await vi.waitFor(() =>
      expect(api.upgradeDownloadJobToPremium).toHaveBeenCalledOnce(),
    );
    await vi.waitFor(() => expect(controller.snapshot().canNativeDownload).toBe(true));
    const snap = controller.snapshot();
    expect(snap.promoted).toBe(false);
    expect(snap.premiumError).toBeNull();
    expect(snap.errorText).toBeNull();
    expect(snap.phase).toBe("ready");
    expect(snap.downloadHref).toBe(BROWSER_GRANT_PATH);
  });

  it("names the failure when the visitor asked, and always names a busy delivery", async () => {
    const api = upgradeApi({
      upgradeDownloadJobToPremium: vi.fn(async () => {
        throw flowErrorFromCode("INTERNAL_ERROR");
      }),
    });
    const controller = makeController(api);
    await reachReady(controller);
    await controller.refreshPremium();
    await vi.waitFor(() =>
      expect(api.upgradeDownloadJobToPremium).toHaveBeenCalledOnce(),
    );
    expect(controller.snapshot().premiumError).toBeNull();
    await controller.upgradeToPremium({ userInitiated: true });
    expect(controller.snapshot().premiumError).toContain("Что-то пошло не так");
    expect(controller.snapshot().errorText).toBeNull();

    const busy = upgradeApi({
      upgradeDownloadJobToPremium: vi.fn(async () => {
        throw flowErrorFromCode("DELIVERY_IN_PROGRESS");
      }),
    });
    const waiting = makeController(busy);
    await reachReady(waiting);
    await waiting.refreshPremium();
    await vi.waitFor(() =>
      expect(waiting.snapshot().premiumError).toContain("Скачивание уже идёт"),
    );
    expect(
      waiting.snapshot().canNativeDownload || waiting.snapshot().canRetryGrant,
    ).toBe(true);
  });

  it("bounds retries and reports an unconfirmed Premium to the visitor", async () => {
    const api = upgradeApi({
      upgradeDownloadJobToPremium: vi.fn(async () => {
        throw flowErrorFromCode("INTERNAL_ERROR");
      }),
    });
    const controller = makeController(api);
    await reachReady(controller);
    await controller.refreshPremium();
    for (let i = 0; i < MAX_PREMIUM_UPGRADE_ATTEMPTS + 2; i += 1) {
      await controller.upgradeToPremium({ userInitiated: true });
    }
    expect(api.upgradeDownloadJobToPremium).toHaveBeenCalledTimes(
      MAX_PREMIUM_UPGRADE_ATTEMPTS,
    );

    const free = upgradeApi({
      getPremiumStatus: vi.fn(async () => ({ active: false as const })),
    });
    const noPremium = makeController(free);
    await reachReady(noPremium);
    await noPremium.refreshPremium();
    await noPremium.upgradeToPremium({ userInitiated: true });
    expect(free.upgradeDownloadJobToPremium).not.toHaveBeenCalled();
    expect(noPremium.snapshot().premiumError).toContain(
      "Premium сейчас не подтверждён",
    );
  });

  it("drops upgrade state when a new flow starts", async () => {
    const api = upgradeApi();
    const controller = makeController(api);
    await reachReady(controller);
    await controller.refreshPremium();
    await vi.waitFor(() => expect(controller.snapshot().promoted).toBe(true));
    controller.startOver();
    const snap = controller.snapshot();
    expect(snap.promoted).toBe(false);
    expect(snap.upgradePending).toBe(false);
    expect(snap.premiumError).toBeNull();
  });
});
