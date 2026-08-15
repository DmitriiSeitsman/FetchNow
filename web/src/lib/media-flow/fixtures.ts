import type { DownloadJob, InspectionJob, MediaFormat } from "./contracts";

export const OPTION_ID = "fmt_aaaaaaaaaaaaaaaaaaaaaaaaaaaa";
export const JOB_ID = "11111111-2222-4333-8444-555555555555";
export const DOWNLOAD_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
export const GRANT_ID = "bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee";
export const BROWSER_GRANT_PATH = `/api/v1/media/browser-grants/${GRANT_ID}/content`;
export const STAMP = "2026-08-13T04:22:27Z";
export const EXPIRES = "2099-01-01T00:00:00Z";

export const progressiveFormat: MediaFormat = {
  formatOptionId: OPTION_ID,
  container: "mp4",
  width: 1280,
  height: 720,
  fps: 30,
  hasVideo: true,
  hasAudio: true,
  category: "progressive",
  videoCodec: "avc",
  audioCodec: "aac",
  approxBytes: 12_000_000,
  qualityLabel: "p720",
  freeTierEligible: true,
};

export function inspectionResult(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    providerId: "vk",
    canonicalProviderUrl: "https://vk.com/video-1_2",
    mediaId: "-1_2",
    title: "Example",
    durationSeconds: 90,
    formats: [progressiveFormat],
    extractionTool: "ytdlp",
    extractionToolVersion: "2024.08.01",
    muxingRequired: false,
    ...overrides,
  };
}

export function inspectionPayload(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    id: JOB_ID,
    state: "queued",
    createdAt: STAMP,
    updatedAt: STAMP,
    expiresAt: EXPIRES,
    completedAt: null,
    errorCode: null,
    result: null,
    statusPath: `/api/v1/media/jobs/${JOB_ID}`,
    ...overrides,
  };
}

export function inspectedPayload(
  resultOverrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return inspectionPayload({
    state: "inspected",
    completedAt: STAMP,
    result: inspectionResult(resultOverrides),
  });
}

export function downloadPayload(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  const merged: Record<string, unknown> = {
    id: DOWNLOAD_ID,
    mediaJobId: JOB_ID,
    state: "queued",
    formatOptionId: OPTION_ID,
    selectedFormat: progressiveFormat,
    createdAt: STAMP,
    updatedAt: STAMP,
    expiresAt: EXPIRES,
    completedAt: null,
    artifactReady: false,
    errorCode: null,
    progressStage: "queued",
    attempt: 0,
    maxAttempts: 3,
    nextAttemptAt: null,
    cancellable: true,
    progressPercent: null,
    artifactBytes: null,
    suggestedFilename: `fetchnow-${DOWNLOAD_ID}.mp4`,
    ...overrides,
  };
  const state = merged.state;
  if (state !== "queued" && state !== "downloading") {
    merged.cancellable = false;
  }
  if (state === "ready") {
    if (!Object.prototype.hasOwnProperty.call(overrides, "artifactBytes")) {
      merged.artifactBytes = 12_000_000;
    }
    if (merged.progressStage === "queued") {
      merged.progressStage = "ready";
    }
  }
  if (state === "failed" && merged.progressStage === "queued") {
    merged.progressStage = "failed";
  }
  if (state === "cancelled" && merged.progressStage === "queued") {
    merged.progressStage = "cancelled";
  }
  if (state === "expired" && merged.progressStage === "queued") {
    merged.progressStage = "expired";
  }
  if (state === "downloading" && merged.progressStage === "queued") {
    merged.progressStage = "inspecting";
  }
  return merged;
}

export function browserGrantPayload(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    downloadPath: BROWSER_GRANT_PATH,
    expiresAt: EXPIRES,
    ...overrides,
  };
}

export function asInspection(payload: Record<string, unknown>): InspectionJob {
  return payload as unknown as InspectionJob;
}

export function asDownload(payload: Record<string, unknown>): DownloadJob {
  return payload as unknown as DownloadJob;
}
