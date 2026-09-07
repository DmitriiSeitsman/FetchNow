import type { CreatedPaymentOrder } from "../media-flow/contracts";

export const PAYMENT_ORDER_SESSION_KEY = "fetchnow:premium-payment-order";
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

export function createIdempotencyKey(cryptoSource: Crypto = globalThis.crypto): string {
  return cryptoSource.randomUUID();
}

export function storePaymentOrder(
  orderId: string,
  storage: Storage = sessionStorage,
): void {
  if (!UUID_RE.test(orderId)) throw new Error("Invalid payment order reference");
  storage.setItem(PAYMENT_ORDER_SESSION_KEY, orderId);
}

export function readPaymentOrder(storage: Storage = sessionStorage): string | null {
  try {
    const value = storage.getItem(PAYMENT_ORDER_SESSION_KEY);
    return value && UUID_RE.test(value) ? value : null;
  } catch {
    return null;
  }
}

export function clearPaymentOrder(storage: Storage = sessionStorage): void {
  try {
    storage.removeItem(PAYMENT_ORDER_SESSION_KEY);
  } catch {
    /* unavailable */
  }
}

export function submitServerPaymentForm(
  created: CreatedPaymentOrder,
  doc: Document = document,
): void {
  if (created.paymentForm.fields.IsTest !== "1") {
    throw new Error("Temporary checkout requires a TEST payment form");
  }
  const form = doc.createElement("form");
  form.method = created.paymentForm.method;
  form.action = created.paymentForm.action;
  form.hidden = true;
  for (const [name, value] of Object.entries(created.paymentForm.fields)) {
    const input = doc.createElement("input");
    input.type = "hidden";
    input.name = name;
    input.value = value;
    form.append(input);
  }
  doc.body.append(form);
  form.submit();
}
