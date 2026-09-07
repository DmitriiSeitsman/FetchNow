/**
 * @vitest-environment jsdom
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CreatedPaymentOrder } from "../media-flow/contracts";
import {
  PAYMENT_ORDER_SESSION_KEY,
  clearPaymentOrder,
  createIdempotencyKey,
  readPaymentOrder,
  storePaymentOrder,
  submitServerPaymentForm,
} from "./payment";

const ORDER_ID = "11111111-2222-4333-8444-555555555555";

function created(): CreatedPaymentOrder {
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
        Receipt: "%7B%22items%22%3A%5B%5D%7D",
        Culture: "ru",
      },
    },
  };
}

describe("temporary Premium checkout handoff", () => {
  beforeEach(() => {
    sessionStorage.clear();
    document.body.replaceChildren();
    vi.restoreAllMocks();
  });

  it("stores only the opaque public order reference for this browser session", () => {
    storePaymentOrder(ORDER_ID);
    expect(readPaymentOrder()).toBe(ORDER_ID);
    expect(sessionStorage.length).toBe(1);
    expect(sessionStorage.getItem(PAYMENT_ORDER_SESSION_KEY)).toBe(ORDER_ID);
    expect(sessionStorage.getItem(PAYMENT_ORDER_SESSION_KEY)).not.toContain(
      "SignatureValue",
    );
    clearPaymentOrder();
    expect(readPaymentOrder()).toBeNull();
  });

  it("rejects malformed or tampered order references", () => {
    expect(() => storePaymentOrder("../../../premium=true")).toThrow();
    sessionStorage.setItem(PAYMENT_ORDER_SESSION_KEY, "premium=true");
    expect(readPaymentOrder()).toBeNull();
  });

  it("creates a fresh browser UUID for each intentional attempt", () => {
    const randomUUID = vi
      .fn()
      .mockReturnValueOnce(ORDER_ID)
      .mockReturnValueOnce("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee");
    const source = { randomUUID } as unknown as Crypto;
    expect(createIdempotencyKey(source)).not.toBe(createIdempotencyKey(source));
    expect(randomUUID).toHaveBeenCalledTimes(2);
  });

  it("submits the server form unchanged and never computes signed fields", () => {
    const submit = vi
      .spyOn(HTMLFormElement.prototype, "submit")
      .mockImplementation(() => {});
    const response = created();
    submitServerPaymentForm(response);
    const form = document.querySelector<HTMLFormElement>("form");
    expect(form?.method).toBe("post");
    expect(form?.action).toBe(response.paymentForm.action);
    expect(
      Object.fromEntries(
        [...(form?.querySelectorAll<HTMLInputElement>("input") ?? [])].map((input) => [
          input.name,
          input.value,
        ]),
      ),
    ).toEqual(response.paymentForm.fields);
    expect(submit).toHaveBeenCalledOnce();
  });

  it("refuses a non-TEST form before navigation", () => {
    const submit = vi
      .spyOn(HTMLFormElement.prototype, "submit")
      .mockImplementation(() => {});
    const response = created();
    response.paymentForm.fields.IsTest = "0" as "1";
    expect(() => submitServerPaymentForm(response)).toThrow(/TEST/);
    expect(submit).not.toHaveBeenCalled();
  });
});
