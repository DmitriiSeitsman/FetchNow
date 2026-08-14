import type { ProgressStage } from "./contracts";
import type { FlowPhase } from "./state-machine";

/**
 * Stage-based preparation progress.
 *
 * The backend reports the preparation stage, never the number of downloaded
 * bytes, so the indicator describes how many preparation steps are done. It is
 * deliberately not a download byte percentage.
 */
export type ProgressTone = "active" | "done";

export type ProgressView = {
  /** True when a preparation or save step is actually running or finished. */
  visible: boolean;
  /** Current stage label shown in the progress card. */
  label: string;
  /** Completed share of the preparation steps, 0–100. */
  percent: number;
  /** Accessible text for the same value. */
  valueText: string;
  tone: ProgressTone;
  /** True while an operation is in flight, so the spinner belongs to the card. */
  spinner: boolean;
};

export const STAGE_LABEL: Readonly<Record<ProgressStage, string>> = Object.freeze({
  queued: "Waiting to start…",
  inspecting: "Confirming the selected quality…",
  downloading_video: "Downloading video…",
  downloading_audio: "Downloading audio…",
  muxing: "Combining video and audio…",
  verifying: "Checking the prepared file…",
  publishing: "Placing the file in temporary storage…",
  retrying: "Retrying preparation…",
  ready: "Ready to save",
  failed: "The file could not be prepared.",
  cancelled: "Download cancelled",
  expired: "This download expired.",
});

export const PHASE_LABEL: Readonly<Partial<Record<FlowPhase, string>>> = Object.freeze({
  submitting: "Checking the link…",
  inspecting: "Reading media information…",
  inspected: "Choose a quality to download.",
  enqueueing_download: "Waiting to start…",
  downloading: "Waiting to start…",
  ready: "Ready to save",
  saving: "Saving to your computer…",
  completed: "Saved to your computer",
  cancelled: "Download cancelled",
});

/**
 * Completed share of the preparation steps per stage. A direct progressive job
 * can jump from `downloading_video` to `publishing`; the audio and muxing steps
 * are never required for the value to keep growing.
 */
export const STAGE_COMPLETION: Readonly<Record<ProgressStage, number>> = Object.freeze({
  queued: 8,
  retrying: 12,
  inspecting: 25,
  downloading_video: 45,
  downloading_audio: 62,
  muxing: 78,
  verifying: 88,
  publishing: 96,
  ready: 100,
  failed: 0,
  cancelled: 0,
  expired: 0,
});

const PHASE_COMPLETION: Readonly<Partial<Record<FlowPhase, number>>> = Object.freeze({
  submitting: 4,
  inspecting: 12,
  enqueueing_download: 8,
  downloading: 8,
  ready: 100,
  saving: 100,
  completed: 100,
});

export const PROGRESS_NOTE =
  "These are preparation steps on our side, not a download percentage.";

const STAGE_DRIVEN_PHASES: ReadonlySet<FlowPhase> = new Set([
  "enqueueing_download",
  "downloading",
]);

const PROGRESS_PHASES: ReadonlySet<FlowPhase> = new Set([
  "submitting",
  "inspecting",
  "enqueueing_download",
  "downloading",
  "ready",
  "saving",
  "completed",
]);

const DONE_PHASES: ReadonlySet<FlowPhase> = new Set(["ready", "completed"]);

const HALTED_STAGES: ReadonlySet<ProgressStage> = new Set([
  "failed",
  "cancelled",
  "expired",
]);

/** Status copy for the current phase, using the stage while a job is running. */
export function flowStatusText(phase: FlowPhase, stage: ProgressStage | null): string {
  if (STAGE_DRIVEN_PHASES.has(phase) && stage) {
    return STAGE_LABEL[stage] ?? PHASE_LABEL[phase] ?? "";
  }
  return PHASE_LABEL[phase] ?? "";
}

export function progressView(
  phase: FlowPhase,
  stage: ProgressStage | null,
): ProgressView {
  const halted = stage !== null && HALTED_STAGES.has(stage);
  const visible = PROGRESS_PHASES.has(phase) && !halted;
  const done = DONE_PHASES.has(phase);
  const label = visible ? flowStatusText(phase, stage) : "";
  const percent = visible ? completion(phase, stage) : 0;
  return {
    visible,
    label,
    percent,
    valueText: label,
    tone: done ? "done" : "active",
    spinner: visible && !done,
  };
}

function completion(phase: FlowPhase, stage: ProgressStage | null): number {
  if (DONE_PHASES.has(phase) || phase === "saving") {
    return PHASE_COMPLETION[phase] ?? 100;
  }
  if (STAGE_DRIVEN_PHASES.has(phase) && stage) {
    return STAGE_COMPLETION[stage] ?? PHASE_COMPLETION[phase] ?? 0;
  }
  return PHASE_COMPLETION[phase] ?? 0;
}
