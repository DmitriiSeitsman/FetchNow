import type { FlowSnapshot } from "./controller";
import { isDownloadEligible } from "./contracts";

function setText(el: Element | null, text: string): void {
  if (el) {
    el.textContent = text;
  }
}

function formatBytes(value: number | null): string {
  if (value === null) {
    return "";
  }
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDims(format: FlowSnapshot["formats"][number]): string {
  const parts: string[] = [];
  if (format.width && format.height) {
    parts.push(`${format.width}×${format.height}`);
  }
  if (format.fps) {
    parts.push(`${Math.round(format.fps)} fps`);
  }
  parts.push(format.container);
  const size = formatBytes(format.approxBytes);
  if (size) {
    parts.push(size);
  }
  parts.push(format.hasVideo && format.hasAudio ? "video + audio" : "incomplete");
  return parts.join(" · ");
}

export function renderFlow(root: ParentNode, snapshot: FlowSnapshot): void {
  const status = root.querySelector("[data-flow-status]");
  setText(status, snapshot.errorText ?? snapshot.statusText);
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
  const save = root.querySelector<HTMLButtonElement>("[data-flow-save]");
  if (save) {
    save.disabled = !snapshot.canSave || snapshot.busy;
    save.hidden =
      snapshot.phase !== "ready" &&
      snapshot.phase !== "saving" &&
      snapshot.phase !== "completed";
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

  const list = root.querySelector("[data-flow-formats]");
  if (list instanceof HTMLElement) {
    list.replaceChildren();
    const show = snapshot.formats.length > 0 && snapshot.result !== null;
    list.hidden = !show;
    if (show) {
      for (const format of snapshot.formats) {
        const eligible = !snapshot.muxingBlocked && isDownloadEligible(format);
        const item = document.createElement("label");
        item.className = "format" + (eligible ? "" : " format-disabled");
        const radio = document.createElement("input");
        radio.type = "radio";
        radio.name = "formatOption";
        radio.value = format.formatOptionId;
        radio.disabled = !eligible || snapshot.phase !== "inspected";
        radio.checked = snapshot.selectedFormatId === format.formatOptionId;
        const title = document.createElement("span");
        title.className = "format-label";
        title.textContent = format.qualityLabel;
        const detail = document.createElement("span");
        detail.className = "format-detail";
        detail.textContent = formatDims(format);
        item.append(radio, title, detail);
        list.append(item);
      }
    }
  }

  const mux = root.querySelector("[data-flow-mux]");
  if (mux instanceof HTMLElement) {
    const onlyIncomplete =
      snapshot.result !== null &&
      (snapshot.muxingBlocked ||
        snapshot.formats.every((format) => !isDownloadEligible(format)));
    mux.hidden = !onlyIncomplete || snapshot.phase === "idle";
  }

  const unsupported = root.querySelector("[data-flow-browser]");
  if (unsupported instanceof HTMLElement) {
    unsupported.hidden = !snapshot.browserUnsupported;
  }
}
