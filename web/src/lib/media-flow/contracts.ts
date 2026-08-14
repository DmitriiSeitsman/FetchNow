import { isCanonicalAccessToken } from "./credentials";
import { FlowError, flowErrorFromCode } from "./errors";

export const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const FORMAT_OPTION_RE = /^fmt_[0-9a-f]{28}$/;

export const INSPECTION_STATES = [
  "queued",
  "inspecting",
  "inspected",
  "failed",
  "expired",
] as const;
export type InspectionState = (typeof INSPECTION_STATES)[number];

export const DOWNLOAD_STATES = [
  "queued",
  "downloading",
  "ready",
  "failed",
  "cancelled",
  "expired",
] as const;
export type DownloadState = (typeof DOWNLOAD_STATES)[number];
export const PROGRESS_STAGES = [
  "queued",
  "inspecting",
  "downloading_video",
  "downloading_audio",
  "muxing",
  "verifying",
  "publishing",
  "retrying",
  "ready",
  "failed",
  "cancelled",
  "expired",
] as const;
export type ProgressStage = (typeof PROGRESS_STAGES)[number];

const PROGRESS_FOR_STATE: Record<DownloadState, readonly ProgressStage[]> = {
  queued: ["queued", "retrying"],
  downloading: [
    "inspecting",
    "downloading_video",
    "downloading_audio",
    "muxing",
    "verifying",
    "publishing",
  ],
  ready: ["ready"],
  failed: ["failed"],
  cancelled: ["cancelled"],
  expired: ["expired"],
};

export const FORMAT_CATEGORIES = ["progressive", "video_only", "audio_only"] as const;
export type FormatCategory = (typeof FORMAT_CATEGORIES)[number];

const FORBIDDEN_KEYS = new Set([
  "url",
  "urls",
  "directUrl",
  "directUrls",
  "signedUrl",
  "downloadUrl",
  "streamUrl",
  "manifestUrl",
  "cookie",
  "cookies",
  "authorization",
  "token",
  "providerFormatToken",
  "provider_format_token",
  "accessToken",
  "query",
  "fragment",
]);

const FORMAT_KEYS = new Set([
  "formatOptionId",
  "container",
  "width",
  "height",
  "fps",
  "hasVideo",
  "hasAudio",
  "category",
  "videoCodec",
  "audioCodec",
  "approxBytes",
  "qualityLabel",
  "freeTierEligible",
]);

const RESULT_KEYS = new Set([
  "providerId",
  "canonicalProviderUrl",
  "mediaId",
  "title",
  "durationSeconds",
  "formats",
  "extractionTool",
  "extractionToolVersion",
  "muxingRequired",
]);

const JOB_KEYS = new Set([
  "id",
  "state",
  "createdAt",
  "updatedAt",
  "expiresAt",
  "completedAt",
  "errorCode",
  "result",
  "statusPath",
]);

const DOWNLOAD_KEYS = new Set([
  "id",
  "mediaJobId",
  "state",
  "formatOptionId",
  "selectedFormat",
  "createdAt",
  "updatedAt",
  "expiresAt",
  "completedAt",
  "artifactReady",
  "errorCode",
  "progressStage",
  "attempt",
  "maxAttempts",
  "nextAttemptAt",
  "cancellable",
]);

export type MediaFormat = {
  formatOptionId: string;
  container: string;
  width: number | null;
  height: number | null;
  fps: number | null;
  hasVideo: boolean;
  hasAudio: boolean;
  category: FormatCategory;
  videoCodec: string;
  audioCodec: string;
  approxBytes: number | null;
  qualityLabel: string;
  freeTierEligible: boolean;
};

export type InspectionResult = {
  providerId: string;
  canonicalProviderUrl: string;
  mediaId: string;
  title: string | null;
  durationSeconds: number | null;
  formats: MediaFormat[];
  muxingRequired: boolean;
};

export type InspectionJob = {
  id: string;
  state: InspectionState;
  createdAt: string;
  updatedAt: string;
  expiresAt: string;
  completedAt: string | null;
  errorCode: string | null;
  result: InspectionResult | null;
  statusPath: string;
};

export type DownloadJob = {
  id: string;
  mediaJobId: string;
  state: DownloadState;
  formatOptionId: string;
  selectedFormat: MediaFormat;
  createdAt: string;
  updatedAt: string;
  expiresAt: string;
  completedAt: string | null;
  artifactReady: boolean;
  errorCode: string | null;
  progressStage: ProgressStage;
  attempt: number;
  maxAttempts: number;
  nextAttemptAt: string | null;
  cancellable: boolean;
};

export type ApiErrorBody = {
  error: { code: string; message: string; request_id?: string };
};

function fail(code = "CONTRACT"): never {
  throw flowErrorFromCode(code);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function rejectForbidden(record: Record<string, unknown>): void {
  for (const key of Object.keys(record)) {
    if (FORBIDDEN_KEYS.has(key)) {
      fail();
    }
  }
}

function rejectUnknown(record: Record<string, unknown>, allow: Set<string>): void {
  for (const key of Object.keys(record)) {
    if (!allow.has(key)) {
      fail();
    }
  }
}

export function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID_RE.test(value);
}

export function isFormatOptionId(value: unknown): value is string {
  return typeof value === "string" && FORMAT_OPTION_RE.test(value);
}

export function isIsoTimestamp(value: unknown): value is string {
  if (typeof value !== "string" || value.length < 10 || value.length > 64) {
    return false;
  }
  const ms = Date.parse(value);
  return Number.isFinite(ms);
}

function requireString(value: unknown, max = 256): string {
  if (typeof value !== "string" || value.length === 0 || value.length > max) {
    fail();
  }
  return value;
}

function optionalFiniteNumber(value: unknown): number | null {
  if (value === null) {
    return null;
  }
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    fail();
  }
  return value;
}

const SAFE_TOKEN_RE = /^[a-z][a-z0-9_]{0,63}$/;
const SAFE_MEDIA_ID_RE = /^[A-Za-z0-9._:-]{1,128}$/;
const TOOL_VERSION_RE = /^[A-Za-z0-9._+-]{1,64}$/;
const VK_PATH_RE = /^\/video(-?\d+)_(\d+)\/?$/;
const RUTUBE_PATH_RE = /^\/video\/([A-Za-z0-9_-]{1,64})\/?$/;

/** Exact public hosts from the backend provider registry (PR8 snapshot). */
export const VK_CANONICAL_HOSTS = Object.freeze([
  "vk.com",
  "www.vk.com",
  "m.vk.com",
  "vkvideo.ru",
  "www.vkvideo.ru",
  "m.vkvideo.ru",
] as const);
export const RUTUBE_CANONICAL_HOSTS = Object.freeze([
  "rutube.ru",
  "www.rutube.ru",
] as const);
const VK_HOSTS = new Set<string>(VK_CANONICAL_HOSTS);
const RUTUBE_HOSTS = new Set<string>(RUTUBE_CANONICAL_HOSTS);

function requireDurationSeconds(value: unknown): number | null {
  if (value === null) {
    return null;
  }
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    fail();
  }
  return value;
}

function requireSafeToken(value: unknown): string {
  const token = requireString(value, 64);
  if (!SAFE_TOKEN_RE.test(token)) {
    fail();
  }
  return token;
}

function requireToolVersion(value: unknown): string | null {
  if (value === null) {
    return null;
  }
  if (typeof value !== "string" || !TOOL_VERSION_RE.test(value)) {
    fail();
  }
  return value;
}

export function assertCanonicalProviderUrl(
  raw: string,
  providerId: string,
  mediaId: string,
): void {
  if (typeof raw !== "string" || raw.length === 0 || raw.length > 4096) {
    fail();
  }
  if (raw.includes("?") || raw.includes("#")) {
    fail();
  }
  if (!raw.toLowerCase().startsWith("https://")) {
    fail();
  }
  const authority = httpsAuthority(raw);
  if (authority.includes("@")) {
    fail();
  }
  if (explicitPortInAuthority(authority)) {
    fail();
  }
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    fail();
  }
  if (parsed.protocol !== "https:") {
    fail();
  }
  if (parsed.username !== "" || parsed.password !== "") {
    fail();
  }
  if (parsed.search !== "" || parsed.hash !== "") {
    fail();
  }
  const host = parsed.hostname.toLowerCase();
  const path = parsed.pathname || "/";
  if (providerId === "vk") {
    if (!VK_HOSTS.has(host)) {
      fail();
    }
    const match = VK_PATH_RE.exec(path);
    if (!match) {
      fail();
    }
    const identity = `${match[1]}_${match[2]}`;
    if (mediaId !== identity) {
      fail();
    }
    return;
  }
  if (providerId === "rutube") {
    if (!RUTUBE_HOSTS.has(host)) {
      fail();
    }
    const match = RUTUBE_PATH_RE.exec(path);
    if (!match || mediaId !== match[1]) {
      fail();
    }
    return;
  }
  fail();
}

function httpsAuthority(raw: string): string {
  const sep = raw.indexOf("://");
  if (sep === -1) {
    fail();
  }
  const rest = raw.slice(sep + 3);
  const end = rest.search(/[/?#]/);
  const authority = end === -1 ? rest : rest.slice(0, end);
  if (!authority) {
    fail();
  }
  return authority;
}

function explicitPortInAuthority(authority: string): boolean {
  if (authority.startsWith("[")) {
    const close = authority.indexOf("]");
    if (close === -1) {
      fail();
    }
    return authority.slice(close + 1).startsWith(":");
  }
  return authority.includes(":");
}

function requireBoolean(value: unknown): boolean {
  if (typeof value !== "boolean") {
    fail();
  }
  return value;
}

export function parseMediaFormat(value: unknown): MediaFormat {
  if (!isRecord(value)) {
    fail();
  }
  rejectForbidden(value);
  rejectUnknown(value, FORMAT_KEYS);
  const formatOptionId = value.formatOptionId;
  if (!isFormatOptionId(formatOptionId)) {
    fail();
  }
  const category = value.category;
  if (
    typeof category !== "string" ||
    !(FORMAT_CATEGORIES as readonly string[]).includes(category)
  ) {
    fail();
  }
  const typedCategory = category as FormatCategory;
  const hasVideo = requireBoolean(value.hasVideo);
  const hasAudio = requireBoolean(value.hasAudio);
  if (category === "progressive" && !(hasVideo && hasAudio)) {
    fail();
  }
  if (category === "video_only" && !(hasVideo && !hasAudio)) {
    fail();
  }
  if (category === "audio_only" && !(!hasVideo && hasAudio)) {
    fail();
  }
  const container = requireString(value.container, 16);
  if (!/^[a-z0-9]{2,8}$/.test(container)) {
    fail();
  }
  return {
    formatOptionId,
    container,
    width: optionalFiniteNumber(value.width),
    height: optionalFiniteNumber(value.height),
    fps: optionalFiniteNumber(value.fps),
    hasVideo,
    hasAudio,
    category: typedCategory,
    videoCodec: requireString(value.videoCodec, 32),
    audioCodec: requireString(value.audioCodec, 32),
    approxBytes: optionalFiniteNumber(value.approxBytes),
    qualityLabel: requireString(value.qualityLabel, 32),
    freeTierEligible: requireBoolean(value.freeTierEligible),
  };
}

export function isDownloadEligible(format: MediaFormat): boolean {
  return (
    format.category === "progressive" &&
    format.hasVideo &&
    format.hasAudio &&
    format.freeTierEligible &&
    isFormatOptionId(format.formatOptionId)
  );
}

function parseInspectionResult(value: unknown): InspectionResult {
  if (!isRecord(value)) {
    fail();
  }
  rejectForbidden(value);
  rejectUnknown(value, RESULT_KEYS);
  const providerId = requireSafeToken(value.providerId);
  if (providerId !== "vk" && providerId !== "rutube") {
    fail();
  }
  const mediaId = requireString(value.mediaId, 128);
  if (!SAFE_MEDIA_ID_RE.test(mediaId)) {
    fail();
  }
  const canonicalProviderUrl = requireString(value.canonicalProviderUrl, 4096);
  assertCanonicalProviderUrl(canonicalProviderUrl, providerId, mediaId);
  requireSafeToken(value.extractionTool);
  requireToolVersion(value.extractionToolVersion);
  if (!Array.isArray(value.formats) || value.formats.length > 256) {
    fail();
  }
  const formats = value.formats.map(parseMediaFormat);
  const title = value.title === null ? null : requireString(value.title, 200);
  return {
    providerId,
    canonicalProviderUrl,
    mediaId,
    title,
    durationSeconds: requireDurationSeconds(value.durationSeconds),
    formats,
    muxingRequired: requireBoolean(value.muxingRequired),
  };
}

export function parseInspectionJob(value: unknown): InspectionJob {
  if (!isRecord(value)) {
    fail();
  }
  rejectForbidden(value);
  rejectUnknown(value, JOB_KEYS);
  const id = value.id;
  if (!isUuid(id)) {
    fail();
  }
  const state = value.state;
  if (
    typeof state !== "string" ||
    !(INSPECTION_STATES as readonly string[]).includes(state)
  ) {
    fail();
  }
  const typedState = state as InspectionState;
  const statusPath = value.statusPath;
  if (statusPath !== `/api/v1/media/jobs/${id}`) {
    fail();
  }
  const result = value.result === null ? null : parseInspectionResult(value.result);
  if (state === "inspected") {
    if (result === null) {
      fail();
    }
  } else if (result !== null) {
    fail();
  }
  const errorCode =
    value.errorCode === null ? null : requireString(value.errorCode, 64);
  if (state === "failed" && !errorCode) {
    fail();
  }
  if (state !== "failed" && errorCode !== null) {
    fail();
  }
  const createdAt = requireIso(value.createdAt);
  const updatedAt = requireIso(value.updatedAt);
  const expiresAt = requireIso(value.expiresAt);
  const completedAt =
    value.completedAt === null ? null : requireIso(value.completedAt);
  assertInspectionTimestamps(
    typedState,
    createdAt,
    updatedAt,
    expiresAt,
    completedAt,
  );
  return {
    id,
    state: typedState,
    createdAt,
    updatedAt,
    expiresAt,
    completedAt,
    errorCode,
    result,
    statusPath,
  };
}

function requireIso(value: unknown): string {
  if (!isIsoTimestamp(value)) {
    fail();
  }
  return value;
}

function msOf(stamp: string): number {
  const ms = Date.parse(stamp);
  if (!Number.isFinite(ms)) {
    fail();
  }
  return ms;
}

function assertCreatedUpdatedExpires(
  createdAt: string,
  updatedAt: string,
  expiresAt: string,
  requireUpdatedBeforeExpiry: boolean,
): { created: number; updated: number; expires: number } {
  const created = msOf(createdAt);
  const updated = msOf(updatedAt);
  const expires = msOf(expiresAt);
  if (created > updated) {
    fail();
  }
  if (created > expires) {
    fail();
  }
  if (requireUpdatedBeforeExpiry && updated > expires) {
    fail();
  }
  return { created, updated, expires };
}

function assertCompletedCoherence(
  completedAt: string,
  created: number,
  expires: number,
  requireNotAfterExpiry: boolean,
): void {
  const completed = msOf(completedAt);
  if (completed < created) {
    fail();
  }
  if (requireNotAfterExpiry && completed > expires) {
    fail();
  }
}

function assertInspectionTimestamps(
  state: InspectionState,
  createdAt: string,
  updatedAt: string,
  expiresAt: string,
  completedAt: string | null,
): void {
  if (state === "expired") {
    const { created, expires } = assertCreatedUpdatedExpires(
      createdAt,
      updatedAt,
      expiresAt,
      false,
    );
    if (completedAt === null) {
      fail();
    }
    assertCompletedCoherence(completedAt, created, expires, false);
    return;
  }
  const { created, expires } = assertCreatedUpdatedExpires(
    createdAt,
    updatedAt,
    expiresAt,
    true,
  );
  if (state === "inspected" || state === "failed") {
    if (completedAt === null) {
      fail();
    }
    assertCompletedCoherence(completedAt, created, expires, true);
    return;
  }
  if (completedAt !== null) {
    fail();
  }
}

function assertDownloadTimestamps(
  state: DownloadState,
  createdAt: string,
  updatedAt: string,
  expiresAt: string,
  completedAt: string | null,
): void {
  if (state === "expired") {
    const { created, expires } = assertCreatedUpdatedExpires(
      createdAt,
      updatedAt,
      expiresAt,
      false,
    );
    if (completedAt === null) {
      fail();
    }
    assertCompletedCoherence(completedAt, created, expires, false);
    return;
  }
  const { created, expires } = assertCreatedUpdatedExpires(
    createdAt,
    updatedAt,
    expiresAt,
    true,
  );
  if (state === "ready" || state === "failed" || state === "cancelled") {
    if (completedAt === null) {
      fail();
    }
    assertCompletedCoherence(completedAt, created, expires, true);
    return;
  }
  if (completedAt !== null) {
    fail();
  }
}

export function parseDownloadJob(value: unknown): DownloadJob {
  if (!isRecord(value)) {
    fail();
  }
  rejectForbidden(value);
  rejectUnknown(value, DOWNLOAD_KEYS);
  const id = value.id;
  const mediaJobId = value.mediaJobId;
  if (!isUuid(id) || !isUuid(mediaJobId)) {
    fail();
  }
  const state = value.state;
  if (
    typeof state !== "string" ||
    !(DOWNLOAD_STATES as readonly string[]).includes(state)
  ) {
    fail();
  }
  const typedDownloadState = state as DownloadState;
  const selectedFormat = parseMediaFormat(value.selectedFormat);
  const formatOptionId = value.formatOptionId;
  if (
    !isFormatOptionId(formatOptionId) ||
    formatOptionId !== selectedFormat.formatOptionId
  ) {
    fail();
  }
  const artifactReady = requireBoolean(value.artifactReady);
  if (state === "ready" && artifactReady !== true) {
    fail();
  }
  if (state !== "ready" && artifactReady !== false) {
    fail();
  }
  const errorCode =
    value.errorCode === null ? null : requireString(value.errorCode, 64);
  if (state === "failed" && !errorCode) {
    fail();
  }
  if (state !== "failed" && errorCode !== null) {
    fail();
  }
  const progressStage = value.progressStage;
  if (
    typeof progressStage !== "string" ||
    !(PROGRESS_STAGES as readonly string[]).includes(progressStage)
  ) {
    fail();
  }
  const typedProgress = progressStage as ProgressStage;
  if (
    !(PROGRESS_FOR_STATE[typedDownloadState] as readonly string[]).includes(
      typedProgress,
    )
  ) {
    fail();
  }
  const attempt = value.attempt;
  const maxAttempts = value.maxAttempts;
  if (
    typeof attempt !== "number" ||
    !Number.isInteger(attempt) ||
    attempt < 0 ||
    attempt > 1_000_000
  ) {
    fail();
  }
  if (
    typeof maxAttempts !== "number" ||
    !Number.isInteger(maxAttempts) ||
    maxAttempts < 1 ||
    maxAttempts > 1_000_000
  ) {
    fail();
  }
  if (attempt > maxAttempts) {
    fail();
  }
  const nextAttemptAt =
    value.nextAttemptAt === null ? null : requireIso(value.nextAttemptAt);
  const retrying =
    typedDownloadState === "queued" && typedProgress === "retrying";
  if (retrying) {
    if (nextAttemptAt === null) {
      fail();
    }
  } else if (nextAttemptAt !== null) {
    fail();
  }
  const cancellable = requireBoolean(value.cancellable);
  if (
    typedDownloadState !== "queued" &&
    typedDownloadState !== "downloading" &&
    cancellable
  ) {
    fail();
  }
  const createdAt = requireIso(value.createdAt);
  const updatedAt = requireIso(value.updatedAt);
  const expiresAt = requireIso(value.expiresAt);
  const completedAt =
    value.completedAt === null ? null : requireIso(value.completedAt);
  assertDownloadTimestamps(
    typedDownloadState,
    createdAt,
    updatedAt,
    expiresAt,
    completedAt,
  );
  return {
    id,
    mediaJobId,
    state: typedDownloadState,
    formatOptionId,
    selectedFormat,
    createdAt,
    updatedAt,
    expiresAt,
    completedAt,
    artifactReady,
    errorCode,
    progressStage: progressStage as ProgressStage,
    attempt,
    maxAttempts,
    nextAttemptAt,
    cancellable,
  };
}

export function parseApiError(value: unknown): ApiErrorBody {
  if (!isRecord(value) || !isRecord(value.error)) {
    throw new FlowError(
      "CONTRACT",
      "Something went wrong. Please try again in a moment, or start over.",
    );
  }
  const code = value.error.code;
  if (typeof code !== "string" || code.length === 0 || code.length > 64) {
    throw new FlowError(
      "CONTRACT",
      "Something went wrong. Please try again in a moment, or start over.",
    );
  }
  return {
    error: {
      code,
      message: typeof value.error.message === "string" ? value.error.message : "",
    },
  };
}

export function assertCanonicalToken(token: string): void {
  if (!isCanonicalAccessToken(token)) {
    throw new FlowError(
      "INVALID_ACCESS_TOKEN",
      "Something went wrong. Please try again in a moment, or start over.",
    );
  }
}
