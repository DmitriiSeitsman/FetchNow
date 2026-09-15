import type { FlowSnapshot } from "./controller";
import { formatApproxBytes, formatExactBytes } from "./bytes";
import {
  formatDeliverySpeed,
  formatEstimatedDownloadTime,
  estimateDownloadSeconds,
  PREMIUM_HIGHLIGHT_ETA_SECONDS,
} from "./estimate";
import {
  providerDisplayName,
  type MediaKind,
  type PaymentProductSummary,
} from "./contracts";
import { progressView } from "./progress";
import {
  groupQualityOptions,
  groupStandaloneOptions,
  qualityTechnicalLabel,
  type QualityOption,
} from "./quality";

function setText(el: Element | null, text: string): void {
  if (el) {
    el.textContent = text;
  }
}

function freeQuotaPlanCopy(snapshot: FlowSnapshot): string {
  const quota = snapshot.freeQuota;
  if (quota?.tier !== "free") {
    return "Лимит бесплатных загрузок";
  }
  const hours =
    quota.windowSeconds !== undefined && quota.windowSeconds % 3600 === 0
      ? quota.windowSeconds / 3600
      : null;
  if (hours === null) {
    return `До ${quota.downloadLimit} бесплатных загрузок`;
  }
  const downloadWord =
    quota.downloadLimit % 10 === 1 && quota.downloadLimit % 100 !== 11
      ? "загрузка"
      : quota.downloadLimit % 10 >= 2 &&
          quota.downloadLimit % 10 <= 4 &&
          (quota.downloadLimit % 100 < 12 || quota.downloadLimit % 100 > 14)
        ? "загрузки"
        : "загрузок";
  return `${quota.downloadLimit} ${downloadWord} за ${hours} ч`;
}

/** Server-published minor units only; the client never invents a price. */
function formatProductPrice(
  product: PaymentProductSummary | null | undefined,
): string | null {
  if (!product) {
    return null;
  }
  const whole = product.amountMinor % 100 === 0;
  return new Intl.NumberFormat("ru-RU", {
    style: "currency",
    currency: product.currency,
    minimumFractionDigits: whole ? 0 : 2,
    maximumFractionDigits: 2,
  }).format(product.amountMinor / 100);
}

function disabledReason(
  format: FlowSnapshot["formats"][number],
  muxingBlocked: boolean,
): string {
  if (format.requiresPremium) {
    return "Доступно в Premium";
  }
  if (muxingBlocked) {
    return "Это медиа недоступно как единый файл с видео и звуком.";
  }
  if (!format.freeTierEligible) {
    return "Этот вариант недоступен по текущей политике скачивания.";
  }
  if (!format.hasVideo || !format.hasAudio || format.category !== "progressive") {
    return "Нужен единый файл с видео и звуком.";
  }
  return "Этот вариант технически недоступен.";
}

function semanticLabel(format: FlowSnapshot["formats"][number]): string {
  if (format.mediaKind === "audio_only") {
    return format.bitrateKbps !== null && format.bitrateKbps > 0
      ? `${Math.round(format.bitrateKbps)} кбит/с`
      : "Аудиодорожка";
  }
  return qualityTechnicalLabel(format);
}

const CATEGORIES = [
  {
    kind: "normal_video",
    title: "Видео + аудио",
    tab: "Видео + аудио",
    subtitle: "Обычная загрузка",
    empty: "Для этой ссылки нет доступных вариантов видео со звуком",
  },
  {
    kind: "video_only",
    title: "Только видео",
    tab: "Видео",
    subtitle: "Без звуковой дорожки",
    empty: "Для этой ссылки нет доступных вариантов видео без звука",
  },
  {
    kind: "audio_only",
    title: "Только аудио",
    tab: "Аудио",
    subtitle: "Без изображения",
    empty: "Для этой ссылки нет доступных вариантов аудио",
  },
] as const;

// Presentation state belongs to this selector, never to the download/session model.
const categoryViews = new WeakMap<
  HTMLElement,
  { active: MediaKind; selected: string | null; result: FlowSnapshot["result"] }
>();
let selectorSequence = 0;

function renderCategories(
  list: HTMLElement,
  options: QualityOption[],
  snapshot: FlowSnapshot,
): void {
  const selected = snapshot.formats.find(
    (f) => f.formatOptionId === snapshot.selectedFormatId,
  );
  let view = categoryViews.get(list);
  if (!view || view.result !== snapshot.result) {
    view = {
      active: selected?.mediaKind ?? "normal_video",
      selected: snapshot.selectedFormatId,
      result: snapshot.result,
    };
    categoryViews.set(list, view);
  } else if (view.selected !== snapshot.selectedFormatId) {
    view.active = selected?.mediaKind ?? view.active;
    view.selected = snapshot.selectedFormatId;
  }
  const focused = list.contains(document.activeElement)
    ? (document.activeElement as HTMLElement)
    : null;
  const focusedValue = focused instanceof HTMLInputElement ? focused.value : null;
  const focusedTab = focused?.dataset.categoryTab;
  const prefix = list.dataset.selectorId ?? `quality-${++selectorSequence}`;
  list.dataset.selectorId = prefix;
  list.replaceChildren();
  const tabs = document.createElement("div");
  tabs.className = "category-tabs";
  tabs.setAttribute("role", "tablist");
  tabs.setAttribute("aria-label", "Тип скачивания");
  const cards = document.createElement("div");
  cards.className = "category-cards";
  const activate = (kind: MediaKind) => {
    view.active = kind;
    tabs.querySelectorAll<HTMLButtonElement>("[role=tab]").forEach((tab) => {
      const active = tab.dataset.categoryTab === kind;
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
    });
    cards.querySelectorAll<HTMLElement>("[data-category-panel]").forEach((panel) => {
      panel.dataset.active = String(panel.dataset.categoryPanel === kind);
    });
  };
  for (const category of CATEGORIES) {
    const grouped = options.filter((o) => o.representative.mediaKind === category.kind);
    const premium = category.kind !== "normal_video" && grouped.length > 0;
    const tab = document.createElement("button");
    tab.type = "button";
    tab.setAttribute("role", "tab");
    tab.id = `${prefix}-tab-${category.kind}`;
    tab.dataset.categoryTab = category.kind;
    tab.setAttribute("aria-controls", `${prefix}-panel-${category.kind}`);
    tab.textContent = category.tab;
    if (premium) {
      const marker = document.createElement("span");
      marker.className = "category-tab-premium";
      marker.textContent = "Premium";
      tab.append(marker);
    }
    tab.addEventListener("click", () => activate(category.kind));
    tab.addEventListener("keydown", (event) => {
      const index = CATEGORIES.findIndex((c) => c.kind === category.kind);
      const next =
        event.key === "ArrowRight"
          ? (index + 1) % 3
          : event.key === "ArrowLeft"
            ? (index + 2) % 3
            : event.key === "Home"
              ? 0
              : event.key === "End"
                ? 2
                : null;
      if (next === null) return;
      event.preventDefault();
      activate(CATEGORIES[next].kind);
      tabs
        .querySelector<HTMLButtonElement>(
          `[data-category-tab="${CATEGORIES[next].kind}"]`,
        )
        ?.focus();
    });
    tabs.append(tab);
    const panel = document.createElement("section");
    panel.className = "category-card";
    panel.dataset.categoryPanel = category.kind;
    panel.id = `${prefix}-panel-${category.kind}`;
    panel.setAttribute("role", "tabpanel");
    panel.tabIndex = 0;
    panel.setAttribute("aria-labelledby", `${prefix}-heading-${category.kind}`);
    const heading = document.createElement("h3");
    heading.id = `${prefix}-heading-${category.kind}`;
    heading.textContent = category.title;
    if (premium) {
      const badge = document.createElement("span");
      badge.className = "premium-badge";
      badge.textContent = "Premium";
      heading.append(badge);
    }
    const subtitle = document.createElement("p");
    subtitle.className = "category-subtitle";
    subtitle.textContent = category.subtitle;
    const help = document.createElement("p");
    help.className = "category-help";
    help.id = `${prefix}-help-${category.kind}`;
    // Unknown Premium state is presented as Free: locks stay closed silently.
    help.textContent = !grouped.length
      ? category.empty
      : !premium || snapshot.premiumState === "active"
        ? ""
        : "Доступно с Premium";
    help.hidden = !help.textContent;
    panel.append(heading, subtitle, help);
    for (const option of grouped) {
      const row = qualityRow(option, snapshot, snapshot.canSelectQuality !== false);
      const radio = row.querySelector("input")!;
      radio.setAttribute(
        "aria-label",
        `${category.title}, ${category.subtitle}, ${semanticLabel(option.representative)}, ${formatDetail(option.representative)}`,
      );
      if (help.textContent) radio.setAttribute("aria-describedby", help.id);
      panel.append(row);
    }
    cards.append(panel);
  }
  list.append(tabs, cards);
  activate(view.active);
  if (focusedValue) {
    [...list.querySelectorAll<HTMLInputElement>("input")]
      .find((r) => r.value === focusedValue && !r.disabled)
      ?.focus({ preventScroll: true });
  } else if (focusedTab) {
    tabs
      .querySelector<HTMLButtonElement>(`[data-category-tab="${focusedTab}"]`)
      ?.focus({ preventScroll: true });
  }
}

function formatDetail(format: FlowSnapshot["formats"][number]): string {
  const parts: string[] = [format.container.toUpperCase()];
  if (
    format.mediaKind !== "audio_only" &&
    typeof format.fps === "number" &&
    Number.isFinite(format.fps) &&
    format.fps > 0
  ) {
    parts.push(`${Math.round(format.fps)} fps`);
  }
  if (format.approxBytes !== null) parts.push(formatApproxBytes(format.approxBytes));
  return parts.join(" · ");
}

function formatReadyDetails(snapshot: FlowSnapshot): string {
  const parts: string[] = [];
  if (snapshot.artifactBytes != null && snapshot.artifactBytes > 0) {
    parts.push(formatExactBytes(snapshot.artifactBytes));
  }
  if (snapshot.selectedFormat) {
    parts.push(snapshot.selectedFormat.container.toUpperCase());
    parts.push(qualityTechnicalLabel(snapshot.selectedFormat));
  }
  return parts.join(" · ");
}

function qualityRow(
  option: QualityOption,
  snapshot: FlowSnapshot,
  selectable: boolean,
): HTMLLabelElement {
  const format = option.representative;
  const item = document.createElement("label");
  item.className = "format" + (option.eligible ? "" : " format-disabled");
  const radio = document.createElement("input");
  radio.type = "radio";
  radio.name = "formatOption";
  radio.value = format.formatOptionId;
  radio.disabled = !option.eligible || !selectable;
  radio.checked = snapshot.selectedFormatId === format.formatOptionId;
  const title = document.createElement("span");
  title.className = "format-label";
  title.textContent = semanticLabel(format);
  const detail = document.createElement("span");
  detail.className = "format-detail";
  detail.textContent = formatDetail(format);
  item.append(radio, title, detail);
  if (!option.eligible && !format.requiresPremium) {
    const reason = document.createElement("span");
    reason.className = "format-reason";
    reason.textContent = disabledReason(format, snapshot.muxingBlocked);
    item.append(reason);
  }
  if (radio.checked && option.eligible) {
    item.classList.add("format-selected");
  }
  return item;
}

/**
 * The Premium half of the READY comparison. It is an upsell only while Premium
 * is not held; once the job delivers under Premium the card steps aside and the
 * single download action carries the flow.
 */
function renderReadyPremium(
  section: HTMLElement,
  snapshot: FlowSnapshot,
  estimatedSeconds: number | null,
  premiumDelivery: boolean,
): void {
  const card = section.querySelector<HTMLElement>("[data-flow-ready-premium]");
  const premiumHeld = snapshot.premiumState === "active" || premiumDelivery;
  const product = snapshot.paymentProduct ?? null;
  if (card) {
    card.hidden = premiumHeld || product === null;
    card.dataset.emphasis =
      estimatedSeconds !== null && estimatedSeconds >= PREMIUM_HIGHLIGHT_ETA_SECONDS
        ? "highlight"
        : "compact";
  }

  const price = section.querySelector<HTMLElement>("[data-flow-ready-premium-price]");
  if (price) {
    const priceText = formatProductPrice(product);
    price.hidden = priceText === null;
    setText(price, priceText ?? "");
  }

  const badge = section.querySelector<HTMLElement>("[data-flow-ready-premium-test]");
  if (badge) {
    badge.hidden = snapshot.testCheckoutAvailable !== true;
  }

  const cta = section.querySelector<HTMLButtonElement>("[data-flow-ready-premium-cta]");
  if (cta) {
    cta.hidden =
      snapshot.testCheckoutAvailable !== true || product === null || premiumHeld;
    cta.disabled = snapshot.checkoutBusy === true || snapshot.busy;
  }

  const note = section.querySelector<HTMLElement>("[data-flow-ready-premium-note]");
  if (note) {
    const noteText = snapshot.upgradePending
      ? "Переводим загрузку на Premium…"
      : premiumDelivery
        ? "Premium активен: скачивание без ограничения скорости."
        : null;
    note.hidden = noteText === null;
    setText(note, noteText ?? "");
  }

  const error = section.querySelector<HTMLElement>("[data-flow-ready-premium-error]");
  if (error) {
    const show = snapshot.premiumError !== null && premiumHeld;
    error.hidden = !show;
    setText(error, show ? (snapshot.premiumError ?? "") : "");
  }

  const retry = section.querySelector<HTMLButtonElement>(
    "[data-flow-premium-upgrade-retry]",
  );
  if (retry) {
    retry.hidden =
      snapshot.premiumState !== "active" ||
      premiumDelivery ||
      snapshot.upgradePending === true;
    retry.disabled = snapshot.busy;
  }
}

export function renderFlow(root: ParentNode, snapshot: FlowSnapshot): void {
  if (root instanceof Element && !root.isConnected) {
    return;
  }
  const isReady = snapshot.phase === "ready";
  const quotaState = snapshot.freeQuota ?? null;
  const quota = root.querySelector<HTMLElement>("[data-flow-quota]");
  if (quota) {
    quota.hidden = quotaState === null;
    if (quotaState !== null && quotaState.tier === "free") {
      quota.textContent =
        quotaState.downloadsRemaining === 0
          ? "Лимит бесплатных загрузок исчерпан."
          : `Доступно бесплатных загрузок: ${quotaState.downloadsRemaining} из ${quotaState.downloadLimit}`;
    } else if (quotaState?.tier === "premium") {
      quota.textContent = "Без лимита загрузок";
    }
  }
  // Premium status is silent unless it is genuinely active: loading, free and
  // background failures all render as nothing so the page never narrates a
  // check the visitor did not ask for.
  const premium = root.querySelector<HTMLElement>("[data-flow-premium-status]");
  if (premium) {
    const activePremium =
      snapshot.premiumState === "active" && snapshot.premiumStatus?.active === true;
    premium.hidden = !activePremium;
    premium.dataset.state = snapshot.premiumState;
    if (activePremium && snapshot.premiumStatus?.active) {
      const expiry = new Intl.DateTimeFormat("ru-RU", {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(new Date(snapshot.premiumStatus.expiresAt));
      const icon = document.createElement("span");
      icon.className = "premium-status-icon";
      icon.setAttribute("aria-hidden", "true");
      icon.textContent = "✦";
      const detail = document.createElement("span");
      detail.className = "premium-status-expiry";
      detail.textContent = `Доступ до ${expiry}`;
      premium.replaceChildren(
        icon,
        document.createTextNode("Premium активен · "),
        detail,
      );
    } else {
      premium.replaceChildren();
    }
  }

  // Premium failures only speak next to the action that caused them: the
  // checkout button while Premium is not held, the upgrade otherwise.
  const premiumErrorText = snapshot.premiumError ?? null;
  const checkoutError = root.querySelector<HTMLElement>("[data-flow-premium-error]");
  if (checkoutError) {
    const show = premiumErrorText !== null && snapshot.premiumState !== "active";
    checkoutError.hidden = !show;
    setText(checkoutError, show ? premiumErrorText : "");
  }
  const quotaReset = root.querySelector<HTMLElement>("[data-flow-quota-reset]");
  if (quotaReset) {
    const resetAt = quotaState?.tier === "free" ? quotaState.resetAt : null;
    quotaReset.hidden = resetAt === null;
    quotaReset.textContent = resetAt
      ? `Следующая загрузка станет доступна ${new Intl.DateTimeFormat("ru-RU", {
          dateStyle: "short",
          timeStyle: "short",
        }).format(new Date(resetAt))}.`
      : "";
  }
  const hideProgressAfterHandoff = isReady && snapshot.nativeDownloadHandoff;
  const progress = progressView(snapshot.phase, snapshot.progressStage, {
    progressPercent: snapshot.progressPercent,
    artifactBytes: snapshot.artifactBytes,
    formats: snapshot.formats,
  });
  const progressVisible = progress.visible && !hideProgressAfterHandoff;
  const status = root.querySelector("[data-flow-status]");
  setText(status, snapshot.errorText ?? (progressVisible ? "" : snapshot.statusText));
  if (status instanceof HTMLElement) {
    status.dataset.tone = snapshot.errorText ? "error" : "info";
    status.setAttribute("role", snapshot.errorText ? "alert" : "status");
    status.setAttribute("aria-live", snapshot.errorText ? "assertive" : "polite");
  }

  const submit = root.querySelector<HTMLButtonElement>("[data-flow-submit]");
  if (submit) {
    submit.disabled = !snapshot.canSubmit || snapshot.busy;
  }
  const input = root.querySelector<HTMLInputElement>("[data-flow-url]");
  if (input) {
    input.disabled = !snapshot.canSubmit;
  }
  const paste = root.querySelector<HTMLButtonElement>("[data-flow-paste]");
  if (paste) {
    paste.disabled = !snapshot.canSubmit || snapshot.busy;
  }

  // The browser download is the only delivery path, so it is always primary.
  const premiumDelivery = snapshot.promoted === true;
  const nativeDownload = root.querySelector<HTMLAnchorElement>(
    "[data-flow-native-download]",
  );
  if (nativeDownload) {
    const showPrimary =
      snapshot.canNativeDownload ||
      (snapshot.nativeDownloadHandoff && snapshot.downloadHref !== null);
    nativeDownload.hidden = !showPrimary;
    nativeDownload.classList.add("btn-primary");
    nativeDownload.classList.remove("btn-ghost");
    nativeDownload.textContent = snapshot.nativeDownloadHandoff
      ? "Скачать снова"
      : premiumDelivery
        ? "Скачать"
        : "Скачать бесплатно";
    if (snapshot.downloadHref) {
      nativeDownload.href = snapshot.downloadHref;
    } else {
      nativeDownload.removeAttribute("href");
    }
    nativeDownload.setAttribute("aria-disabled", showPrimary ? "false" : "true");
  }

  const grantRetry = root.querySelector<HTMLButtonElement>("[data-flow-grant-retry]");
  if (grantRetry) {
    grantRetry.hidden = !snapshot.canRetryGrant;
    grantRetry.disabled = !snapshot.canRetryGrant || snapshot.busy;
  }

  const grantPending = root.querySelector("[data-flow-grant-pending]");
  if (grantPending instanceof HTMLElement) {
    grantPending.hidden = !snapshot.grantArming;
  }

  const handoff = root.querySelector("[data-flow-handoff]");
  if (handoff instanceof HTMLElement) {
    handoff.hidden = !snapshot.nativeDownloadHandoff;
  }

  const httpsRequired = root.querySelector("[data-flow-https]");
  if (httpsRequired instanceof HTMLElement) {
    httpsRequired.hidden = !snapshot.httpsRequired;
  }

  const enqueue = root.querySelector<HTMLButtonElement>("[data-flow-download]");
  if (enqueue) {
    enqueue.disabled =
      !snapshot.downloadEligible ||
      snapshot.busy ||
      snapshot.phase !== "inspected" ||
      snapshot.freeQuota?.downloadsRemaining === 0;
    enqueue.hidden = snapshot.phase !== "inspected";
  }
  const startOver = root.querySelector<HTMLButtonElement>("[data-flow-reset]");
  if (startOver) {
    startOver.hidden = !snapshot.canStartOver;
    startOver.disabled = snapshot.upgradePending === true;
  }
  const cancel = root.querySelector<HTMLButtonElement>("[data-flow-cancel]");
  if (cancel) {
    cancel.hidden = !snapshot.canCancelTask;
    cancel.disabled = !snapshot.canCancelTask;
  }
  const progressActions = root.querySelector<HTMLElement>(
    "[data-flow-progress-actions]",
  );
  if (progressActions) {
    progressActions.hidden = !snapshot.canCancelTask;
  }

  const restored = root.querySelector("[data-flow-restored]");
  if (restored instanceof HTMLElement) {
    restored.hidden = !snapshot.restored;
  }

  // READY dedicated choice section
  const readySection = root.querySelector<HTMLElement>("[data-flow-ready]");
  if (readySection) {
    readySection.hidden = !isReady;
    if (isReady) {
      const readyDetails = readySection.querySelector("[data-flow-ready-details]");
      setText(readyDetails, formatReadyDetails(snapshot));

      const speedEl = readySection.querySelector<HTMLElement>(
        "[data-flow-ready-speed]",
      );
      if (speedEl) {
        const speedText = formatDeliverySpeed(snapshot.deliveryRateBytesPerSecond);
        speedEl.hidden = speedText === null;
        setText(speedEl, speedText ?? "");
      }

      const estSeconds = estimateDownloadSeconds(
        snapshot.artifactBytes,
        snapshot.deliveryRateBytesPerSecond,
      );

      const timeEl = readySection.querySelector<HTMLElement>("[data-flow-ready-time]");
      if (timeEl) {
        const timeText = formatEstimatedDownloadTime(estSeconds);
        timeEl.hidden = timeText === null;
        setText(timeEl, timeText ?? "");
      }

      const freeTitle = readySection.querySelector<HTMLElement>(
        "[data-flow-ready-free-title]",
      );
      if (freeTitle) {
        setText(freeTitle, premiumDelivery ? "Premium" : "Бесплатно");
      }

      renderReadyPremium(readySection, snapshot, estSeconds, premiumDelivery);
    }
  }

  const progressCard = root.querySelector("[data-flow-progress]");
  if (progressCard instanceof HTMLElement) {
    progressCard.hidden = !progressVisible || (isReady && readySection !== null);
    progressCard.dataset.tone = progress.tone;
    setText(progressCard.querySelector("[data-flow-progress-label]"), progress.label);
  }
  const bar = root.querySelector("[data-flow-progress-bar]");
  if (bar instanceof HTMLElement) {
    bar.setAttribute("role", "progressbar");
    bar.setAttribute("aria-valuemin", "0");
    bar.setAttribute("aria-valuemax", "100");
    bar.setAttribute("aria-valuenow", String(progress.percent));
    bar.setAttribute("aria-valuetext", progress.valueText);
  }
  const fill = root.querySelector("[data-flow-progress-fill]");
  if (fill instanceof HTMLElement) {
    fill.style.width = `${progress.percent}%`;
  }
  const loader = root.querySelector("[data-flow-loader]");
  if (loader instanceof HTMLElement) {
    loader.hidden = !progress.spinner || hideProgressAfterHandoff;
  }
  const spinner = root.querySelector("[data-flow-spinner]");
  if (spinner instanceof HTMLElement) {
    spinner.setAttribute("aria-hidden", "true");
  }

  const meta = root.querySelector("[data-flow-meta]");
  if (meta instanceof HTMLElement) {
    meta.hidden = snapshot.result === null;
    if (snapshot.result) {
      setText(
        meta.querySelector("[data-flow-title]"),
        snapshot.result.title ?? "Без названия",
      );
      setText(
        meta.querySelector("[data-flow-provider]"),
        providerDisplayName(snapshot.result.providerId),
      );
      const duration = snapshot.result.durationSeconds;
      if (duration === null) {
        setText(meta.querySelector("[data-flow-duration]"), "Длительность неизвестна");
      } else {
        const mins = Math.floor(duration / 60);
        const secs = Math.round(duration % 60)
          .toString()
          .padStart(2, "0");
        setText(meta.querySelector("[data-flow-duration]"), `${mins}:${secs}`);
      }
    }
  }

  const normalOptions = groupQualityOptions(snapshot.formats, {
    muxingBlocked: snapshot.muxingBlocked,
    selectedFormatId: snapshot.selectedFormatId,
  });
  const options = [
    ...normalOptions,
    ...groupStandaloneOptions(
      snapshot.formats,
      snapshot.premiumState === "active",
      snapshot.selectedFormatId,
    ),
  ];
  // Quality options only render during the inspected phase before preparation.
  const showQuality =
    snapshot.phase === "inspected" &&
    snapshot.canSelectQuality !== false &&
    snapshot.result !== null;
  const list = root.querySelector("[data-flow-formats]");
  if (list instanceof HTMLElement) {
    list.hidden = !showQuality;
    if (showQuality) {
      renderCategories(list, options, snapshot);
    } else {
      list.replaceChildren();
      categoryViews.delete(list);
    }
  }
  const qualityCard = root.querySelector("[data-flow-quality]");
  if (qualityCard instanceof HTMLElement) {
    qualityCard.hidden = !showQuality;
  }
  const summary = root.querySelector<HTMLElement>("[data-flow-selection-summary]");
  if (summary) {
    const selected = snapshot.formats.find(
      (f) => f.formatOptionId === snapshot.selectedFormatId,
    );
    summary.hidden = snapshot.phase !== "inspected" || !selected;
    summary.textContent = selected
      ? `Выбрано: ${CATEGORIES.find((c) => c.kind === selected.mediaKind)?.title} · ${semanticLabel(selected)} · ${formatDetail(selected)}`
      : "";
  }

  const mux = root.querySelector("[data-flow-mux]");
  if (mux instanceof HTMLElement) {
    const onlyIncomplete =
      snapshot.result !== null && normalOptions.every((option) => !option.eligible);
    mux.hidden = !onlyIncomplete || snapshot.phase === "idle";
  }
  const checkout = root.querySelector<HTMLElement>("[data-flow-premium-checkout]");
  if (checkout) {
    // Product visibility is independent of standalone stream availability.
    // The containing quality card already scopes this offer to inspected media.
    checkout.hidden = snapshot.premiumState !== "free";
  }
  setText(
    root.querySelector("[data-flow-free-plan-quota]"),
    freeQuotaPlanCopy(snapshot),
  );
  const exhausted =
    snapshot.freeQuota?.tier === "free" && snapshot.freeQuota.downloadsRemaining === 0;
  setText(
    root.querySelector("[data-flow-premium-next-step]"),
    exhausted
      ? "Бесплатные загрузки закончились. Оформите Premium на 24 часа, чтобы продолжить пользоваться сервисом."
      : "Premium снимает ограничения количества загрузок и скорости со стороны FetchNow.",
  );
  const testCheckout = root.querySelector<HTMLElement>(
    "[data-flow-premium-test-checkout]",
  );
  if (testCheckout) {
    testCheckout.hidden = !snapshot.testCheckoutAvailable;
  }
  const checkoutButton = root.querySelector<HTMLButtonElement>(
    "[data-flow-premium-cta]",
  );
  if (checkoutButton) {
    checkoutButton.disabled = snapshot.checkoutBusy === true;
    checkoutButton.setAttribute("aria-busy", snapshot.checkoutBusy ? "true" : "false");
    checkoutButton.textContent = snapshot.checkoutBusy
      ? "Открываем тестовую оплату…"
      : "Тестовая оплата Premium";
  }
}
