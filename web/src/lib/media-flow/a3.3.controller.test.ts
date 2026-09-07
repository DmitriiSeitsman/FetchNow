import { describe, expect, it, vi } from "vitest";
import { MediaFlowController } from "./controller";
import type { MediaApi } from "./api";
import { FlowSession } from "./session";
import { generateAccessToken } from "./credentials";
import { inspectedPayload, inspectionPayload, progressiveFormat } from "./fixtures";
import { flowErrorFromCode } from "./errors";
import { parseInspectionJob, type CreatedPaymentOrder } from "./contracts";

const AUDIO_ID = "fmt_cccccccccccccccccccccccccccc";
const ORDER_ID = "11111111-2222-4333-8444-555555555555";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function createdOrder(): CreatedPaymentOrder {
  return {
    orderId: ORDER_ID,
    status: "PENDING",
    paymentForm: {
      action: "https://auth.robokassa.ru/Merchant/Index.aspx",
      method: "POST",
      fields: {
        MerchantLogin: "fetchnow",
        OutSum: "1.00",
        InvId: "42",
        Description: "Доступ FetchNow на 24 часа",
        SignatureValue: "a".repeat(64),
        IsTest: "1",
        Receipt: "%7B%7D",
        Culture: "ru",
      },
    },
  };
}

function memoryStore(): Storage {
  const values = new Map<string, string>();
  return {
    get length() {
      return values.size;
    },
    clear: () => values.clear(),
    getItem: (key) => values.get(key) ?? null,
    key: (index) => [...values.keys()][index] ?? null,
    removeItem: (key) => {
      values.delete(key);
    },
    setItem: (key, value) => {
      values.set(key, value);
    },
  };
}

function controller(
  api: Record<string, unknown>,
  hooks: {
    storePaymentOrder?: (orderId: string) => void;
    submitPaymentForm?: (created: CreatedPaymentOrder) => void;
  } = {},
): MediaFlowController {
  return new MediaFlowController({
    api: api as unknown as MediaApi,
    session: new FlowSession(memoryStore()),
    generateToken: generateAccessToken,
    pickerSupported: () => false,
    secureContext: () => true,
    documentHidden: () => false,
    createIdempotencyKey: () => "A".repeat(32),
    ...hooks,
  });
}

describe("PRD2-A3.3 controller boundaries", () => {
  it("coalesces rapid TEST checkout clicks into one in-flight order", async () => {
    const gate = deferred<CreatedPaymentOrder>();
    const api = {
      getFreeQuota: vi.fn(async () => null),
      getPremiumStatus: vi.fn(async () => ({ active: false as const })),
      getPaymentConfig: vi.fn(async () => ({ testCheckoutAvailable: true })),
      createPaymentOrder: vi.fn(() => gate.promise),
    };
    const store = vi.fn();
    const submit = vi.fn();
    const flow = controller(api, {
      storePaymentOrder: store,
      submitPaymentForm: submit,
    });
    await flow.initializeAccount();
    const first = flow.startTestCheckout();
    const second = flow.startTestCheckout();
    expect(flow.snapshot().checkoutBusy).toBe(true);
    expect(api.createPaymentOrder).toHaveBeenCalledOnce();
    gate.resolve(createdOrder());
    await Promise.all([first, second]);
    expect(store).toHaveBeenCalledExactlyOnceWith(ORDER_ID);
    expect(submit).toHaveBeenCalledOnce();
    expect(flow.snapshot().checkoutBusy).toBe(false);
  });

  it("recovers the TEST CTA after order creation fails", async () => {
    const api = {
      getFreeQuota: vi.fn(async () => null),
      getPremiumStatus: vi.fn(async () => ({ active: false as const })),
      getPaymentConfig: vi.fn(async () => ({ testCheckoutAvailable: true })),
      createPaymentOrder: vi.fn(async () => {
        throw new Error("offline");
      }),
    };
    const flow = controller(api);
    await flow.initializeAccount();
    await flow.startTestCheckout();
    expect(flow.snapshot().checkoutBusy).toBe(false);
    expect(flow.snapshot().testCheckoutAvailable).toBe(true);
    expect(flow.snapshot().errorText).toContain("Не удалось начать тестовую оплату");
  });

  it("does not collapse identity/bootstrap failure into a Free Premium state", async () => {
    const api = {
      getFreeQuota: vi.fn(async () => null),
      getPremiumStatus: vi.fn(async () => {
        throw flowErrorFromCode("PAYMENT_IDENTITY_REQUIRED");
      }),
      getPaymentConfig: vi.fn(async () => ({ testCheckoutAvailable: true })),
      createPaymentOrder: vi.fn(),
    };
    const flow = controller(api);
    await flow.initializeAccount();
    expect(flow.snapshot().premiumState).toBe("error");
    expect(flow.snapshot().premiumStatus).toBeNull();
    await flow.startTestCheckout();
    expect(api.createPaymentOrder).not.toHaveBeenCalled();
  });

  it("refreshes Premium and quota when the downloader returns from payment via bfcache", async () => {
    const api = {
      getFreeQuota: vi.fn(async () => null),
      getPremiumStatus: vi
        .fn()
        .mockResolvedValueOnce({ active: false })
        .mockResolvedValueOnce({
          active: true,
          expiresAt: "2026-09-08T12:00:00Z",
          productCode: "premium_24h",
          remainingSeconds: 86_400,
        }),
      getPaymentConfig: vi.fn(async () => ({ testCheckoutAvailable: true })),
    };
    const flow = controller(api);
    await flow.initializeAccount();
    expect(flow.snapshot().premiumState).toBe("free");
    await flow.onPageShow(true);
    expect(flow.snapshot().premiumState).toBe("active");
    expect(api.getFreeQuota).toHaveBeenCalledTimes(2);
    expect(api.getPremiumStatus).toHaveBeenCalledTimes(2);
  });

  it("coalesces overlapping account and foreground Premium refreshes", async () => {
    const premium = deferred<{ active: false }>();
    const config = deferred<{ testCheckoutAvailable: boolean }>();
    const api = {
      getFreeQuota: vi.fn(async () => null),
      getPremiumStatus: vi.fn(() => premium.promise),
      getPaymentConfig: vi.fn(() => config.promise),
      createPaymentOrder: vi.fn(),
    };
    const flow = controller(api);
    const initializing = flow.initializeAccount();
    await vi.waitFor(() => {
      expect(api.getPremiumStatus).toHaveBeenCalledOnce();
      expect(api.getPaymentConfig).toHaveBeenCalledOnce();
    });
    flow.onForegroundResume();
    flow.onForegroundResume();
    expect(api.getPremiumStatus).toHaveBeenCalledOnce();
    expect(api.getPaymentConfig).toHaveBeenCalledOnce();
    expect(api.createPaymentOrder).not.toHaveBeenCalled();
    premium.resolve({ active: false });
    config.resolve({ testCheckoutAvailable: true });
    await initializing;
    expect(flow.snapshot().premiumState).toBe("free");
    expect(flow.snapshot().testCheckoutAvailable).toBe(true);
  });

  it("refreshes stale Premium state after backend standalone denial without retry", async () => {
    const audioOnly = {
      ...progressiveFormat,
      formatOptionId: AUDIO_ID,
      width: null,
      height: null,
      fps: null,
      hasVideo: false,
      category: "audio_only",
      videoCodec: "none",
      bitrateKbps: 128,
      qualityLabel: "128k",
      mediaKind: "audio_only",
      freeTierEligible: false,
      requiresPremium: true,
    };
    const premiumActive = {
      active: true as const,
      expiresAt: "2026-09-08T12:00:00Z",
      productCode: "premium_24h" as const,
      remainingSeconds: 86_400,
    };
    const api = {
      getFreeQuota: vi.fn(async () => null),
      getPremiumStatus: vi
        .fn()
        .mockResolvedValueOnce(premiumActive)
        .mockResolvedValueOnce({ active: false }),
      getPaymentConfig: vi.fn(async () => ({ testCheckoutAvailable: false })),
      createInspectionJob: vi.fn(async () => parseInspectionJob(inspectionPayload())),
      getInspectionJob: vi.fn(async () =>
        parseInspectionJob(
          inspectedPayload({ formats: [progressiveFormat, audioOnly] }),
        ),
      ),
      createDownloadJob: vi.fn(async () => {
        throw flowErrorFromCode("MEDIA_CAPABILITY_REQUIRES_PREMIUM");
      }),
    };
    const flow = controller(api);
    await flow.initializeAccount();
    await flow.submit("https://vk.com/video-1_2");
    flow.selectFormat(AUDIO_ID);
    expect(flow.snapshot().selectedFormatId).toBe(AUDIO_ID);
    await flow.enqueueDownload();
    expect(api.createDownloadJob).toHaveBeenCalledOnce();
    expect(flow.snapshot().phase).toBe("inspected");
    expect(flow.snapshot().premiumState).toBe("free");
    expect(flow.snapshot().downloadEligible).toBe(false);
    expect(flow.snapshot().errorText).toContain("Срок Premium истёк");
  });
});
