import type { FlowSnapshot } from "./controller";
import { formatApproxBytes } from "./bytes";
import { progressView } from "./progress";
import { groupQualityOptions, type QualityOption } from "./quality";

function setText(el: Element | null, text: string): void {
  if (el) {
    el.textContent = text;
  }
}

function disabledReason(format: FlowSnapshot["formats"][number], muxingBlocked: boolean): string {
  if (muxingBlocked) {
    return "This media is not available as a combined video and audio file.";
  }
  if (!format.freeTierEligible) {
    return "Above the free 720p limit.";
  }
  if (!format.hasVideo || !format.hasAudio || format.category !== "progressive") {
    return "Needs a combined video and audio file.";
  }
  return "Not available in the current free download mode.";
}

function formatDetail(format: FlowSnapshot["formats"][number]): string {
  const parts: string[] = [format.container.toUpperCase()];
  if (typeof format.fps === "number" && Number.isFinite(format.fps) && format.fps > 0) {
    parts.push(`${Math.round(format.fps)} fps`);
  }
  parts.push(formatApproxBytes(format.approxBytes));
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
  const hideProgressAfterHandoff =
    snapshot.phase === "ready" && snapshot.nativeDownloadHandoff;
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
  const nativeDownload = root.querySelector<HTMLAnchorElement>("[data-flow-native-download]");
  if (nativeDownload) {
    const showPrimary =
      snapshot.canNativeDownload ||
      (snapshot.nativeDownloadHandoff && snapshot.downloadHref !== null);
    nativeDownload.hidden = !showPrimary;
    nativeDownload.classList.toggle("btn-primary", !canPickLocation);
    nativeDownload.classList.toggle("btn-ghost", canPickLocation);
    nativeDownload.textContent = snapshot.nativeDownloadHandoff
      ? "Download again"
      : "Download file";
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
      !snapshot.downloadEligible || snapshot.busy || snapshot.phase !== "inspected";
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

  const progressCard = root.querySelector("[data-flow-progress]");
  if (progressCard instanceof HTMLElement) {
    progressCard.hidden = !progressVisible;
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
        snapshot.result.title ?? "Untitled media",
      );
      setText(
        meta.querySelector("[data-flow-provider]"),
        snapshot.result.providerId === "vk" ? "VK" : "Rutube",
      );
      const duration = snapshot.result.durationSeconds;
      if (duration === null) {
        setText(meta.querySelector("[data-flow-duration]"), "Duration unavailable");
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
  const showQuality =
    snapshot.canSelectQuality !== false && options.length > 1 && snapshot.result !== null;
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
