import { isUuid } from "./contracts";
import { FlowError, GENERIC_USER_MESSAGE } from "./errors";

const ATTR_CHAR = /^[A-Za-z0-9!#$&+\-.^_`|~]$/;

export function suggestedFilename(downloadJobId: string, container: string): string {
  if (!isUuid(downloadJobId) || !/^[a-z0-9]{2,8}$/.test(container)) {
    throw new FlowError("CONTRACT", GENERIC_USER_MESSAGE);
  }
  return `fetchnow-${downloadJobId}.${container}`;
}

export function encodeRfc8187(value: string): string {
  const bytes = new TextEncoder().encode(value);
  let out = "UTF-8''";
  for (const byte of bytes) {
    const char = String.fromCharCode(byte);
    if (ATTR_CHAR.test(char)) {
      out += char;
    } else {
      out += `%${byte.toString(16).toUpperCase().padStart(2, "0")}`;
    }
  }
  return out;
}

export function decodeRfc8187(value: string): string | null {
  if (!value.startsWith("UTF-8''")) {
    return null;
  }
  const raw = value.slice(7);
  if (!raw) {
    return null;
  }
  const bytes: number[] = [];
  for (let i = 0; i < raw.length; ) {
    const ch = raw[i];
    if (ch === "%") {
      if (i + 2 >= raw.length) {
        return null;
      }
      const hex = raw.slice(i + 1, i + 3);
      if (!/^[0-9A-Fa-f]{2}$/.test(hex)) {
        return null;
      }
      bytes.push(Number.parseInt(hex, 16));
      i += 3;
      continue;
    }
    if (!ATTR_CHAR.test(ch ?? "")) {
      return null;
    }
    bytes.push(ch.charCodeAt(0));
    i += 1;
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(new Uint8Array(bytes));
  } catch {
    return null;
  }
}

export function contentDispositionHeader(
  filename: string,
  asciiFallback: string,
): string {
  const ascii = filename.split("").every((ch) => {
    const code = ch.charCodeAt(0);
    return code >= 32 && code <= 126 && ch !== '"' && ch !== "\\";
  })
    ? filename
    : asciiFallback;
  if (/[\r\n\0]/.test(filename) || /[\r\n\0"\\]/.test(ascii)) {
    throw new FlowError("CONTRACT", GENERIC_USER_MESSAGE);
  }
  return `attachment; filename="${ascii}"; filename*=${encodeRfc8187(filename)}`;
}

function splitHeaderParts(header: string): string[] | null {
  if (/[\r\n\0]/.test(header)) {
    return null;
  }
  const parts: string[] = [];
  let buf = "";
  let inQuotes = false;
  for (let i = 0; i < header.length; i += 1) {
    const ch = header[i];
    if (ch === "\\" && inQuotes) {
      return null;
    }
    if (ch === '"') {
      inQuotes = !inQuotes;
      buf += ch;
      continue;
    }
    if (ch === ";" && !inQuotes) {
      const trimmed = buf.trim();
      if (trimmed) {
        parts.push(trimmed);
      }
      buf = "";
      continue;
    }
    buf += ch;
  }
  if (inQuotes) {
    return null;
  }
  const last = buf.trim();
  if (last) {
    parts.push(last);
  }
  return parts;
}

export function parseContentDispositionFilename(
  header: string | null,
  expected: string,
): string | null {
  if (!header) {
    return null;
  }
  const parts = splitHeaderParts(header.trim());
  if (!parts || parts.length < 3) {
    return null;
  }
  if (parts[0].toLowerCase() !== "attachment") {
    return null;
  }
  let asciiName: string | null = null;
  let encoded: string | null = null;
  for (const part of parts.slice(1)) {
    const eq = part.indexOf("=");
    if (eq <= 0) {
      return null;
    }
    const key = part.slice(0, eq).trim().toLowerCase();
    const rawValue = part.slice(eq + 1).trim();
    if (key === "filename") {
      if (asciiName !== null) {
        return null;
      }
      if (!rawValue.startsWith('"') || !rawValue.endsWith('"') || rawValue.length < 2) {
        return null;
      }
      asciiName = rawValue.slice(1, -1);
      if (
        /[\r\n\0"\\]/.test(asciiName) ||
        ![...asciiName].every((ch) => {
          const code = ch.charCodeAt(0);
          return code >= 32 && code <= 126;
        })
      ) {
        return null;
      }
      continue;
    }
    if (key === "filename*") {
      if (encoded !== null) {
        return null;
      }
      encoded = rawValue;
      continue;
    }
    return null;
  }
  if (asciiName === null || encoded === null) {
    return null;
  }
  const decoded = decodeRfc8187(encoded);
  if (decoded !== expected) {
    return null;
  }
  return decoded;
}

/** True when native cookie-grant delivery is allowed on this origin. */
export function isSecureDeliveryContext(origin?: string): boolean {
  let resolved: string;
  try {
    resolved =
      origin ??
      (typeof globalThis.location?.origin === "string"
        ? globalThis.location.origin
        : "http://localhost");
    const url = new URL(resolved);
    if (url.protocol === "https:") {
      return true;
    }
    const host = url.hostname.toLowerCase();
    return host === "localhost" || host === "127.0.0.1";
  } catch {
    return false;
  }
}

/** Re-arm browser grants this many milliseconds before they expire. */
export const GRANT_REISSUE_BUFFER_MS = 30_000;

/** Bounded positive minimum delay for success-path refresh timers. */
export const MIN_GRANT_REFRESH_DELAY_MS = 5_000;

/** Click / resume safety margin: treat grants this close to expiry as stale. */
export const GRANT_HANDOFF_SAFETY_MS = 2_000;

/** setTimeout delay cap (32-bit signed integer limit in many runtimes). */
export const MAX_GRANT_TIMER_MS = 2_147_483_647;

/**
 * Deterministic delay until the next grant refresh attempt.
 *
 * Returns null when the grant is already expired (caller must re-arm via
 * resume/click, never via setTimeout(0)). Never returns 0 on a still-valid
 * grant, including when remaining lifetime equals the reissue buffer (30s).
 */
export function computeGrantRefreshDelayMs(
  expiresAtMs: number,
  nowMs: number,
  bufferMs: number = GRANT_REISSUE_BUFFER_MS,
  minDelayMs: number = MIN_GRANT_REFRESH_DELAY_MS,
): number | null {
  if (!Number.isFinite(expiresAtMs) || !Number.isFinite(nowMs)) {
    return null;
  }
  const remaining = expiresAtMs - nowMs;
  if (remaining <= 0) {
    return null;
  }
  const ideal = remaining - bufferMs;
  if (ideal >= minDelayMs) {
    return Math.min(ideal, MAX_GRANT_TIMER_MS);
  }
  // remaining <= buffer: fall back to a positive fraction of remaining life.
  const fallback = Math.max(minDelayMs, Math.floor(remaining / 2));
  const delay = Math.min(remaining - 1, fallback);
  return Math.max(1, Math.min(delay, MAX_GRANT_TIMER_MS));
}
