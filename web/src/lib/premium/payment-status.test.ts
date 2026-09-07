import { describe, expect, it, vi } from "vitest";
import type { MediaApi } from "../media-flow/api";
import type { PaymentOrderStatus, PremiumStatus } from "../media-flow/contracts";
import { PAYMENT_POLL_DELAYS_MS, resolvePaymentSuccess } from "./payment-status";

const ORDER_ID = "11111111-2222-4333-8444-555555555555";
const pending: PaymentOrderStatus = {
  status: "PENDING",
  productCode: "premium_24h",
  amountMinor: 100,
  currency: "RUB",
  createdAt: "2026-09-07T12:00:00Z",
  paidAt: null,
};

function api(
  orders: PaymentOrderStatus[],
  premiums: PremiumStatus[] = [],
): Pick<MediaApi, "getPaymentOrder" | "getPremiumStatus"> {
  return {
    getPaymentOrder: vi.fn(async () => orders.shift() ?? pending),
    getPremiumStatus: vi.fn(async () => premiums.shift() ?? { active: false }),
  } as unknown as Pick<MediaApi, "getPaymentOrder" | "getPremiumStatus">;
}

describe("SuccessURL server-authoritative resolution", () => {
  it("does not grant Premium when opened without a browser order reference", async () => {
    const states: string[] = [];
    const client = api([]);
    await resolvePaymentSuccess(client, (state) => states.push(state), {
      orderId: null,
    });
    expect(states).toEqual(["unidentified"]);
    expect(client.getPaymentOrder).not.toHaveBeenCalled();
    expect(client.getPremiumStatus).not.toHaveBeenCalled();
  });

  it("polls finitely and reports a neutral timeout while the callback is pending", async () => {
    const states: string[] = [];
    const waits: number[] = [];
    const client = api([]);
    await resolvePaymentSuccess(client, (state) => states.push(state), {
      orderId: ORDER_ID,
      wait: async (ms) => {
        waits.push(ms);
      },
    });
    expect(client.getPaymentOrder).toHaveBeenCalledTimes(PAYMENT_POLL_DELAYS_MS.length);
    expect(client.getPremiumStatus).not.toHaveBeenCalled();
    expect(waits).toEqual(PAYMENT_POLL_DELAYS_MS.slice(1));
    expect(states.at(-1)).toBe("timeout");
    expect(states).not.toContain("active");
  });

  it("requires both PAID and backend-confirmed active entitlement", async () => {
    const paid: PaymentOrderStatus = {
      ...pending,
      status: "PAID",
      paidAt: "2026-09-07T12:01:00Z",
    };
    const states: string[] = [];
    const client = api(
      [paid, paid],
      [
        { active: false },
        {
          active: true,
          expiresAt: "2026-09-08T12:01:00Z",
          productCode: "premium_24h",
          remainingSeconds: 86_400,
        },
      ],
    );
    const storage = {
      getItem: vi.fn(() => ORDER_ID),
      setItem: vi.fn(),
      removeItem: vi.fn(),
      clear: vi.fn(),
      key: vi.fn(),
      length: 1,
    } satisfies Storage;
    await resolvePaymentSuccess(client, (state) => states.push(state), {
      storage,
      wait: async () => {},
    });
    expect(states).toContain("processing");
    expect(states.at(-1)).toBe("active");
    expect(client.getPremiumStatus).toHaveBeenCalledTimes(2);
    expect(storage.removeItem).toHaveBeenCalledOnce();
  });

  it("treats backend failures as an error, never a local Premium grant", async () => {
    const states: string[] = [];
    const client = api([]);
    vi.mocked(client.getPaymentOrder).mockRejectedValueOnce(new Error("offline"));
    await resolvePaymentSuccess(client, (state) => states.push(state), {
      orderId: ORDER_ID,
      wait: async () => {},
    });
    expect(states).toEqual(["checking", "error"]);
  });
});
