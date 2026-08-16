import { isCanonicalAccessToken } from "./credentials";
import { FlowError, flowErrorFromCode, GENERIC_USER_MESSAGE } from "./errors";

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

export const DOWNLOAD_PERCENT_STAGES = [
  "downloading_video",
  "downloading_audio",
] as const;

export const CAPABILITY_STATES = ["enabled", "disabled", "planned"] as const;
export type CapabilityState = (typeof CAPABILITY_STATES)[number];

const FORBIDDEN_FILENAME_CHARS = new Set([
  "/",
  "\\",
  ":",
  "*",
  "?",
  '"',
  "<",
  ">",
  "|",
  "\u0000",
]);
const BIDI_CODEPOINTS = new Set([
  0x061c, 0x200e, 0x200f, 0x202a, 0x202b, 0x202c, 0x202d, 0x202e, 0x2066, 0x2067,
  0x2068, 0x2069,
]);
const WINDOWS_RESERVED = new Set([
  "CON",
  "PRN",
  "AUX",
  "NUL",
  ...Array.from({ length: 9 }, (_, i) => `COM${i + 1}`),
  ...Array.from({ length: 9 }, (_, i) => `LPT${i + 1}`),
]);
const UUID_FALLBACK_NAME =
  /^fetchnow-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.[a-z0-9]{2,8}$/i;
/** Unicode code-point limits; UTF-16 `string.length` must not be used. */
const MAX_FILENAME_CHARS = 255;
const MAX_FILENAME_UTF8_BYTES = 240;
const MAX_STEM_CHARS = 80;
const MAX_STEM_UTF8_BYTES = 180;
const ALLOWED_CONTAINERS = new Set(["mp4", "webm", "mkv", "m4a", "mp3", "ogg"]);

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
  "providerCapabilities",
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
  "progressPercent",
  "artifactBytes",
  "suggestedFilename",
  "providerCapabilities",
]);

const PROVIDER_CAPABILITIES_KEYS = new Set([
  "providerId",
  "operations",
  "contentKinds",
  "metadata",
]);
const PROVIDER_CAPABILITY_OPERATIONS_KEYS = new Set([
  "downloadVideo",
  "extractAudio",
  "selectQuality",
  "selectContainer",
]);
const PROVIDER_CAPABILITY_CONTENT_KINDS_KEYS = new Set([
  "video",
  "clip",
  "live",
  "playlist",
]);
const PROVIDER_CAPABILITY_METADATA_KEYS = new Set(["title", "duration", "thumbnail"]);

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

export type ProviderCapabilities = {
  providerId: string;
  operations: {
    downloadVideo: CapabilityState;
    extractAudio: CapabilityState;
    selectQuality: CapabilityState;
    selectContainer: CapabilityState;
  };
  contentKinds: {
    video: CapabilityState;
    clip: CapabilityState;
    live: CapabilityState;
    playlist: CapabilityState;
  };
  metadata: {
    title: CapabilityState;
    duration: CapabilityState;
    thumbnail: CapabilityState;
  };
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
  providerCapabilities?: ProviderCapabilities | null;
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
  progressPercent: number | null;
  artifactBytes: number | null;
  suggestedFilename: string;
  providerCapabilities?: ProviderCapabilities | null;
};

export type ApiErrorBody = {
  error: { code: string; message: string; request_id?: string };
};

export const BROWSER_GRANT_PATH_RE =
  /^\/api\/v1\/media\/browser-grants\/[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\/content$/;

const BROWSER_GRANT_KEYS = new Set(["downloadPath", "expiresAt"]);

export type BrowserGrant = {
  downloadPath: string;
  expiresAt: string;
};

export function parseBrowserGrant(value: unknown): BrowserGrant {
  if (!isRecord(value)) {
    fail();
  }
  rejectForbidden(value);
  rejectUnknown(value, BROWSER_GRANT_KEYS);
  const downloadPath = value.downloadPath;
  if (typeof downloadPath !== "string" || !BROWSER_GRANT_PATH_RE.test(downloadPath)) {
    fail();
  }
  return {
    downloadPath,
    expiresAt: requireIso(value.expiresAt),
  };
}

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
/** Backend always emits /video…; /clip… is an input alias only (PR13). */
const VK_PATH_RE = /^\/video(-?\d+)_(\d+)\/?$/;
const RUTUBE_PATH_RE = /^\/video\/([A-Za-z0-9_-]{1,64})\/?$/;
const OK_PATH_RE = /^\/video\/([\d-]{1,32})\/?$/;
const DZEN_PATH_RE = /^\/video\/watch\/([a-z0-9-]{1,64})\/?$/;

export type ProviderCanonicalRule = {
  readonly displayName: string;
  readonly hosts: readonly string[];
  readonly pathRe: RegExp;
  readonly mediaIdFromMatch: (match: RegExpExecArray) => string;
};

/** Exact public hosts + path grammar from the backend provider registry. */
export const PROVIDER_CANONICAL_RULES: Readonly<
  Record<string, ProviderCanonicalRule>
> = Object.freeze({
  vk: Object.freeze({
    displayName: "VK",
    hosts: Object.freeze([
      "vk.com",
      "www.vk.com",
      "m.vk.com",
      "vk.ru",
      "www.vk.ru",
      "m.vk.ru",
      "vkvideo.ru",
      "www.vkvideo.ru",
      "m.vkvideo.ru",
    ]),
    pathRe: VK_PATH_RE,
    mediaIdFromMatch: (match: RegExpExecArray) => `${match[1]}_${match[2]}`,
  }),
  rutube: Object.freeze({
    displayName: "Rutube",
    hosts: Object.freeze(["rutube.ru", "www.rutube.ru"]),
    pathRe: RUTUBE_PATH_RE,
    mediaIdFromMatch: (match: RegExpExecArray) => match[1]!,
  }),
  ok: Object.freeze({
    displayName: "OK",
    hosts: Object.freeze([
      "ok.ru",
      "www.ok.ru",
      "m.ok.ru",
      "mobile.ok.ru",
      "odnoklassniki.ru",
      "www.odnoklassniki.ru",
      "m.odnoklassniki.ru",
      "mobile.odnoklassniki.ru",
    ]),
    pathRe: OK_PATH_RE,
    mediaIdFromMatch: (match: RegExpExecArray) => match[1]!,
  }),
  dzen: Object.freeze({
    displayName: "Дзен",
    hosts: Object.freeze([
      "dzen.ru",
      "www.dzen.ru",
      "zen.yandex.ru",
      "www.zen.yandex.ru",
    ]),
    pathRe: DZEN_PATH_RE,
    mediaIdFromMatch: (match: RegExpExecArray) => match[1]!,
  }),
});

/** Exact public hosts mirrored from the backend provider registry. */
export const VK_CANONICAL_HOSTS = PROVIDER_CANONICAL_RULES.vk!.hosts;
export const RUTUBE_CANONICAL_HOSTS = PROVIDER_CANONICAL_RULES.rutube!.hosts;
export const OK_CANONICAL_HOSTS = PROVIDER_CANONICAL_RULES.ok!.hosts;
export const DZEN_CANONICAL_HOSTS = PROVIDER_CANONICAL_RULES.dzen!.hosts;

export function providerDisplayName(providerId: string): string {
  return PROVIDER_CANONICAL_RULES[providerId]?.displayName ?? providerId;
}

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
  const rule = PROVIDER_CANONICAL_RULES[providerId];
  if (!rule) {
    fail();
  }
  const host = parsed.hostname.toLowerCase();
  const path = parsed.pathname || "/";
  if (!rule.hosts.includes(host)) {
    fail();
  }
  const match = rule.pathRe.exec(path);
  if (!match) {
    fail();
  }
  if (mediaId !== rule.mediaIdFromMatch(match)) {
    fail();
  }
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

function requireIntegerInRange(value: unknown, min: number, max: number): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < min || value > max) {
    fail();
  }
  return value;
}

function requireBoolean(value: unknown): boolean {
  if (typeof value !== "boolean") {
    fail();
  }
  return value;
}

function requireCapabilityState(value: unknown): CapabilityState {
  if (
    typeof value !== "string" ||
    !(CAPABILITY_STATES as readonly string[]).includes(value)
  ) {
    fail();
  }
  return value as CapabilityState;
}

function parseCapabilityMap(
  value: unknown,
  keys: Set<string>,
): Record<string, CapabilityState> {
  if (!isRecord(value)) {
    fail();
  }
  rejectForbidden(value);
  rejectUnknown(value, keys);
  const parsed: Record<string, CapabilityState> = {};
  for (const key of keys) {
    parsed[key] = requireCapabilityState(value[key]);
  }
  return parsed;
}

function parseProviderCapabilities(
  value: unknown,
): ProviderCapabilities | null | undefined {
  if (value === undefined || value === null) {
    return value;
  }
  if (!isRecord(value)) {
    fail();
  }
  rejectForbidden(value);
  rejectUnknown(value, PROVIDER_CAPABILITIES_KEYS);
  const operations = parseCapabilityMap(
    value.operations,
    PROVIDER_CAPABILITY_OPERATIONS_KEYS,
  );
  const contentKinds = parseCapabilityMap(
    value.contentKinds,
    PROVIDER_CAPABILITY_CONTENT_KINDS_KEYS,
  );
  const metadata = parseCapabilityMap(value.metadata, PROVIDER_CAPABILITY_METADATA_KEYS);
  return {
    providerId: requireSafeToken(value.providerId),
    operations: {
      downloadVideo: operations.downloadVideo,
      extractAudio: operations.extractAudio,
      selectQuality: operations.selectQuality,
      selectContainer: operations.selectContainer,
    },
    contentKinds: {
      video: contentKinds.video,
      clip: contentKinds.clip,
      live: contentKinds.live,
      playlist: contentKinds.playlist,
    },
    metadata: {
      title: metadata.title,
      duration: metadata.duration,
      thumbnail: metadata.thumbnail,
    },
  };
}

export function isSafeSuggestedFilename(name: unknown, container: string): name is string {
  if (typeof name !== "string" || !ALLOWED_CONTAINERS.has(container)) {
    return false;
  }
  const suffix = `.${container}`;
  if (!name.endsWith(suffix)) {
    return false;
  }
  if (unicodeCharCount(name) < 1 || unicodeCharCount(name) > MAX_FILENAME_CHARS) {
    return false;
  }
  const utf8 = new TextEncoder().encode(name);
  if (utf8.length < 1 || utf8.length > MAX_FILENAME_UTF8_BYTES) {
    return false;
  }
  if (
    [...name].some((ch) => FORBIDDEN_FILENAME_CHARS.has(ch)) ||
    name.includes("\r") ||
    name.includes("\n")
  ) {
    return false;
  }
  const stem = Array.from(name).slice(0, -suffix.length).join("");
  if (stem !== sanitizeFilenameStem(stem)) {
    return UUID_FALLBACK_NAME.test(name);
  }
  return true;
}

export function sanitizeFilenameStem(raw: string | null | undefined): string | null {
  if (raw === null || raw === undefined || typeof raw !== "string") {
    return null;
  }
  const text = raw.normalize("NFC");
  const cleaned: string[] = [];
  for (const char of text) {
    const code = char.codePointAt(0) ?? 0;
    if (BIDI_CODEPOINTS.has(code) || FORBIDDEN_FILENAME_CHARS.has(char)) {
      continue;
    }
    if (code >= 0xd800 && code <= 0xdfff) {
      continue;
    }
    if (/\p{C}/u.test(char)) {
      continue;
    }
    if (/\s/u.test(char)) {
      cleaned.push(" ");
      continue;
    }
    cleaned.push(char);
  }
  let collapsed = cleaned.join("").replace(/\s+/g, " ").replace(/^[ .]+|[ .]+$/g, "");
  if (!collapsed || collapsed === "." || collapsed === "..") {
    return null;
  }
  const head = collapsed.split(".", 1)[0]?.toUpperCase() ?? "";
  if (WINDOWS_RESERVED.has(head)) {
    return null;
  }
  if (unicodeCharCount(collapsed) > MAX_STEM_CHARS) {
    collapsed = unicodePrefix(collapsed, MAX_STEM_CHARS).replace(/[ .]+$/g, "");
  }
  let encoded = new TextEncoder().encode(collapsed);
  if (encoded.length > MAX_STEM_UTF8_BYTES) {
    encoded = encoded.subarray(0, MAX_STEM_UTF8_BYTES);
    collapsed = decodeUtf8IgnoreIncomplete(encoded).replace(/[ .]+$/g, "");
  }
  if (!collapsed || collapsed === "." || collapsed === "..") {
    return null;
  }
  if (WINDOWS_RESERVED.has(collapsed.toUpperCase())) {
    return null;
  }
  return collapsed;
}

function unicodeCharCount(text: string): number {
  return Array.from(text).length;
}

function unicodePrefix(text: string, maxChars: number): string {
  const chars = Array.from(text);
  if (chars.length <= maxChars) {
    return text;
  }
  return chars.slice(0, maxChars).join("");
}

function decodeUtf8IgnoreIncomplete(bytes: Uint8Array): string {
  let end = bytes.length;
  while (end > 0) {
    try {
      return new TextDecoder("utf-8", { fatal: true }).decode(bytes.subarray(0, end));
    } catch {
      end -= 1;
    }
  }
  return "";
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
  if (!(providerId in PROVIDER_CANONICAL_RULES)) {
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
  const providerCapabilities = parseProviderCapabilities(value.providerCapabilities);
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
    providerCapabilities,
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
  const progressPercent = parseProgressPercent(
    value.progressPercent,
    typedDownloadState,
    typedProgress,
  );
  const artifactBytes = parseArtifactBytes(
    value.artifactBytes,
    typedDownloadState,
    artifactReady,
  );
  const suggestedFilename = value.suggestedFilename;
  if (!isSafeSuggestedFilename(suggestedFilename, selectedFormat.container)) {
    fail();
  }
  const createdAt = requireIso(value.createdAt);
  const updatedAt = requireIso(value.updatedAt);
  const expiresAt = requireIso(value.expiresAt);
  const completedAt =
    value.completedAt === null ? null : requireIso(value.completedAt);
  const providerCapabilities = parseProviderCapabilities(value.providerCapabilities);
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
    progressPercent,
    artifactBytes,
    suggestedFilename,
    providerCapabilities,
  };
}

function parseProgressPercent(
  value: unknown,
  state: DownloadState,
  stage: ProgressStage,
): number | null {
  const allowed =
    state === "downloading" &&
    (DOWNLOAD_PERCENT_STAGES as readonly string[]).includes(stage);
  if (value === null) {
    return null;
  }
  if (!allowed) {
    fail();
  }
  return requireIntegerInRange(value, 0, 99);
}

function parseArtifactBytes(
  value: unknown,
  state: DownloadState,
  artifactReady: boolean,
): number | null {
  if (state === "ready" && artifactReady) {
    return requireIntegerInRange(value, 1, Number.MAX_SAFE_INTEGER);
  }
  if (value !== null) {
    fail();
  }
  return null;
}

export function parseApiError(value: unknown): ApiErrorBody {
  if (!isRecord(value) || !isRecord(value.error)) {
    throw new FlowError(
      "CONTRACT",
      GENERIC_USER_MESSAGE,
    );
  }
  const code = value.error.code;
  if (typeof code !== "string" || code.length === 0 || code.length > 64) {
    throw new FlowError(
      "CONTRACT",
      GENERIC_USER_MESSAGE,
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
      GENERIC_USER_MESSAGE,
    );
  }
}
