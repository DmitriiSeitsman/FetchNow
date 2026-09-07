import { MediaApi } from "../media-flow/api";
import { clearPaymentOrder, readPaymentOrder } from "./payment";

export type PaymentStatusView =
  | "checking"
  | "processing"
  | "active"
  | "unidentified"
  | "timeout"
  | "error";

export const PAYMENT_POLL_DELAYS_MS = [0, 1000, 2000, 3000, 5000, 8000] as const;

export async function resolvePaymentSuccess(
  api: Pick<MediaApi, "getPaymentOrder" | "getPremiumStatus">,
  onState: (state: PaymentStatusView, expiresAt?: string) => void,
  options: {
    orderId?: string | null;
    wait?: (ms: number) => Promise<void>;
    signal?: AbortSignal;
    storage?: Storage;
  } = {},
): Promise<void> {
  const orderId =
    options.orderId === undefined ? readPaymentOrder(options.storage) : options.orderId;
  if (!orderId) {
    onState("unidentified");
    return;
  }
  const wait =
    options.wait ?? ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
  onState("checking");
  try {
    for (const delay of PAYMENT_POLL_DELAYS_MS) {
      if (delay) await wait(delay);
      if (options.signal?.aborted) return;
      const order = await api.getPaymentOrder(orderId, options.signal);
      if (order.status !== "PAID") {
        onState("processing");
        continue;
      }
      const premium = await api.getPremiumStatus(options.signal);
      if (premium.active) {
        onState("active", premium.expiresAt);
        clearPaymentOrder(options.storage);
        return;
      }
      onState("processing");
    }
    onState("timeout");
  } catch {
    if (!options.signal?.aborted) onState("error");
  }
}

export function mountPaymentSuccess(root: ParentNode): AbortController {
  const abort = new AbortController();
  const status = root.querySelector<HTMLElement>("[data-payment-status]");
  const expiry = root.querySelector<HTMLElement>("[data-payment-expiry]");
  void resolvePaymentSuccess(
    new MediaApi(),
    (state, expiresAt) => {
      if (!status) return;
      const copy: Record<PaymentStatusView, string> = {
        checking: "Проверяем статус оплаты…",
        processing: "Платёж обрабатывается…",
        active: "Тестовая оплата подтверждена. Premium активен.",
        unidentified: "Не удалось определить тестовый платёж в этом браузере.",
        timeout: "Подтверждение оплаты ещё не получено. Статус можно проверить позже.",
        error:
          "Не удалось проверить статус оплаты. Попробуйте обновить страницу позже.",
      };
      status.textContent = copy[state];
      status.dataset.state = state;
      if (expiry) {
        expiry.hidden = state !== "active" || !expiresAt;
        expiry.textContent = expiresAt
          ? `Premium активен до ${new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeStyle: "short" }).format(new Date(expiresAt))}.`
          : "";
      }
    },
    { signal: abort.signal },
  );
  return abort;
}
