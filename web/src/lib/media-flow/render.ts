import type { FlowSnapshot } from "./controller";
import { formatApproxBytes, formatExactBytes } from "./bytes";
import {
  formatDeliverySpeed,
  formatEstimatedDownloadTime,
  estimateDownloadSeconds,
} from "./estimate";
import { providerDisplayName } from "./contracts";
import { progressView } from "./progress";
import {
  groupQualityOptions,
  qualityTechnicalLabel,
  type QualityOption,
} from "./quality";

function setText(el: Element | null, text: string): void {
  if (el) {
    el.textContent = text;
  }
}

function disabledReason(
  format: FlowSnapshot["formats"][number],
  muxingBlocked: boolean,
): string {
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

function formatDetail(format: FlowSnapshot["formats"][number]): string {
  const parts: string[] = [format.container.toUpperCase()];
  if (typeof format.fps === "number" && Number.isFinite(format.fps) && format.fps > 0) {
    parts.push(`${Math.round(format.fps)} fps`);
  }
  parts.push(formatApproxBytes(format.approxBytes));
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
  title.textContent = option.label;
  const detail = document.createElement("span");
  detail.className = "format-detail";
  detail.textContent = formatDetail(format);
  item.append(radio, title, detail);
  if (!option.eligible) {
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

export function renderFlow(root: ParentNode, snapshot: FlowSnapshot): void {
  if (root instanceof Element && !root.isConnected) {
    return;
  }
  const isReady = snapshot.phase === "ready";
  const quotaState = snapshot.freeQuota ?? null;
  const quota = root.querySelector<HTMLElement>("[data-flow-quota]");
  if (quota) {
    quota.hidden = quotaState === null;
    if (quotaState !== null) {
      quota.textContent =
        quotaState.downloadsRemaining === 0
          ? "Лимит бесплатных загрузок исчерпан."
          : `Доступно бесплатных загрузок: ${quotaState.downloadsRemaining} из ${quotaState.downloadLimit}`;
    }
  }
  const quotaReset = root.querySelector<HTMLElement>("[data-flow-quota-reset]");
  if (quotaReset) {
    const resetAt = quotaState?.resetAt ?? null;
    quotaReset.hidden = resetAt === null;
    quotaReset.textContent = resetAt
      ? `Следующая загрузка станет доступна ${new Intl.DateTimeFormat("ru-RU", {
          dateStyle: "short",
          timeStyle: "short",
        }).format(new Date(resetAt))}.`
      : "";
  }
  const hideProgressAfterHandoff =
    isReady && snapshot.nativeDownloadHandoff;
  const progress = progressView(snapshot.phase, snapshot.progressStage, {
    progressPercent: snapshot.progressPercent,
    artifactBytes: snapshot.artifactBytes,
    formats: snapshot.formats,
  });
  const progressVisible = progress.visible && !hideProgressAfterHandoff;
  const status = root.querySelector("[data-flow-status]");
  setText(
    status,
    snapshot.errorText ?? (progressVisible ? "" : snapshot.statusText),
  );
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

  // Save as… is the lead action wherever the picker exists, so the anchor steps
  // back to a secondary style there and stays primary everywhere else.
  const canPickLocation = !snapshot.browserUnsupported;
  const nativeDownload = root.querySelector<HTMLAnchorElement>(
    "[data-flow-native-download]",
  );
  if (nativeDownload) {
    const showPrimary =
      snapshot.canNativeDownload ||
      (snapshot.nativeDownloadHandoff && snapshot.downloadHref !== null);
    nativeDownload.hidden = !showPrimary;
    nativeDownload.classList.toggle("btn-primary", !canPickLocation);
    nativeDownload.classList.toggle("btn-ghost", canPickLocation);
    nativeDownload.textContent = snapshot.nativeDownloadHandoff
      ? "Скачать снова"
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

  const saveAs = root.querySelector<HTMLButtonElement>("[data-flow-save-as]");
  if (saveAs) {
    saveAs.disabled = !snapshot.canSaveAs || snapshot.busy;
    saveAs.hidden =
      !canPickLocation ||
      (snapshot.phase !== "ready" && snapshot.phase !== "saving");
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
    startOver.disabled = snapshot.phase === "saving";
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

      const speedEl = readySection.querySelector<HTMLElement>("[data-flow-ready-speed]");
      if (speedEl) {
        const speedText = formatDeliverySpeed(snapshot.deliveryRateBytesPerSecond);
        speedEl.hidden = speedText === null;
        setText(speedEl, speedText ?? "");
      }

      const timeEl = readySection.querySelector<HTMLElement>("[data-flow-ready-time]");
      if (timeEl) {
        const estSeconds = estimateDownloadSeconds(
          snapshot.artifactBytes,
          snapshot.deliveryRateBytesPerSecond,
        );
        const timeText = formatEstimatedDownloadTime(estSeconds);
        timeEl.hidden = timeText === null;
        setText(timeEl, timeText ?? "");
      }
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

  const options = groupQualityOptions(snapshot.formats, {
    muxingBlocked: snapshot.muxingBlocked,
    selectedFormatId: snapshot.selectedFormatId,
  });
  // Hide with 0–1 grouped options: a single auto-selected format still downloads.
  // Quality options only render during the inspected phase before preparation.
  const showQuality =
    snapshot.phase === "inspected" &&
    snapshot.canSelectQuality !== false &&
    options.length > 1 &&
    snapshot.result !== null;
  const list = root.querySelector("[data-flow-formats]");
  if (list instanceof HTMLElement) {
    list.replaceChildren();
    list.hidden = !showQuality;
    if (showQuality) {
      const selectable = snapshot.phase === "inspected" && snapshot.canSelectQuality !== false;
      for (const option of options) {
        list.append(qualityRow(option, snapshot, selectable));
      }
    }
  }
  const qualityCard = root.querySelector("[data-flow-quality]");
  if (qualityCard instanceof HTMLElement) {
    qualityCard.hidden = !showQuality;
  }

  const mux = root.querySelector("[data-flow-mux]");
  if (mux instanceof HTMLElement) {
    const onlyIncomplete =
      snapshot.result !== null && options.every((option) => !option.eligible);
    mux.hidden = !onlyIncomplete || snapshot.phase === "idle";
  }
}
