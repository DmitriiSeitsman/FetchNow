import { isCanonicalAccessToken } from "./credentials";
import { isFormatOptionId, isIsoTimestamp, isUuid } from "./contracts";
import { FLOW_PHASES, isRestorablePhase, type FlowPhase } from "./state-machine";

export const SESSION_KEY = "fetchnow.media-flow.v2";
export const LEGACY_SESSION_KEY = "fetchnow.media-flow.v1";
export const SESSION_MAX_BYTES = 2048;
export const SESSION_VERSION = 2 as const;

const SESSION_KEYS = new Set([
  "v",
  "token",
  "mediaJobId",
  "downloadJobId",
  "formatOptionId",
  "phase",
  "expiresAt",
]);

export type RecoveryRecord = {
  v: typeof SESSION_VERSION;
  token: string;
  mediaJobId: string | null;
  downloadJobId: string | null;
  formatOptionId: string | null;
  phase: FlowPhase;
  expiresAt: string;
};

export type SessionStore = {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
};

function memoryStore(): SessionStore {
  const map = new Map<string, string>();
  return {
    getItem: (key) => map.get(key) ?? null,
    setItem: (key, value) => {
      map.set(key, value);
    },
    removeItem: (key) => {
      map.delete(key);
    },
  };
}

function activeStore(): SessionStore {
  try {
    if (typeof sessionStorage !== "undefined") {
      return sessionStorage;
    }
  } catch {
    /* private mode */
  }
  return memoryStore();
}

function isPhase(value: unknown): value is FlowPhase {
  return (
    typeof value === "string" && (FLOW_PHASES as readonly string[]).includes(value)
  );
}

function isCoherent(record: RecoveryRecord): boolean {
  if (!isRestorablePhase(record.phase)) {
    return false;
  }
  if (record.downloadJobId !== null) {
    if (record.mediaJobId === null || record.formatOptionId === null) {
      return false;
    }
  }
  if (record.formatOptionId !== null && record.mediaJobId === null) {
    return false;
  }
  switch (record.phase) {
    case "submitting":
    case "inspecting":
      return record.mediaJobId !== null && record.downloadJobId === null;
    case "inspected":
      return record.mediaJobId !== null && record.downloadJobId === null;
    case "enqueueing_download":
      return (
        record.mediaJobId !== null &&
        record.formatOptionId !== null &&
        record.downloadJobId === null
      );
    case "downloading":
    case "ready":
    case "saving":
      return (
        record.mediaJobId !== null &&
        record.downloadJobId !== null &&
        record.formatOptionId !== null
      );
    default:
      return false;
  }
}

export function parseRecoveryRecord(value: unknown): RecoveryRecord | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return null;
  }
  const record = value as Record<string, unknown>;
  for (const key of Object.keys(record)) {
    if (!SESSION_KEYS.has(key)) {
      return null;
    }
  }
  if (record.v !== SESSION_VERSION) {
    return null;
  }
  if (!isCanonicalAccessToken(record.token)) {
    return null;
  }
  if (record.mediaJobId !== null && !isUuid(record.mediaJobId)) {
    return null;
  }
  if (record.downloadJobId !== null && !isUuid(record.downloadJobId)) {
    return null;
  }
  if (record.formatOptionId !== null && !isFormatOptionId(record.formatOptionId)) {
    return null;
  }
  if (!isPhase(record.phase) || !isIsoTimestamp(record.expiresAt)) {
    return null;
  }
  const parsed: RecoveryRecord = {
    v: SESSION_VERSION,
    token: record.token,
    mediaJobId: record.mediaJobId,
    downloadJobId: record.downloadJobId,
    formatOptionId: record.formatOptionId,
    phase: record.phase,
    expiresAt: record.expiresAt,
  };
  if (!isCoherent(parsed)) {
    return null;
  }
  return parsed;
}

export class FlowSession {
  constructor(private readonly store: SessionStore = activeStore()) {}

  read(now = Date.now()): RecoveryRecord | null {
    let raw: string | null;
    try {
      this.store.removeItem(LEGACY_SESSION_KEY);
    } catch {
      /* ignore */
    }
    try {
      raw = this.store.getItem(SESSION_KEY);
    } catch {
      return null;
    }
    if (!raw) {
      return null;
    }
    if (raw.length > SESSION_MAX_BYTES) {
      this.clear();
      return null;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw) as unknown;
    } catch {
      this.clear();
      return null;
    }
    const record = parseRecoveryRecord(parsed);
    if (!record) {
      this.clear();
      return null;
    }
    const expires = Date.parse(record.expiresAt);
    if (!Number.isFinite(expires) || expires <= now) {
      this.clear();
      return null;
    }
    return record;
  }

  write(record: RecoveryRecord): void {
    const valid = parseRecoveryRecord(record);
    if (!valid) {
      this.clear();
      return;
    }
    const serialized = JSON.stringify(valid);
    if (serialized.length > SESSION_MAX_BYTES) {
      this.clear();
      return;
    }
    try {
      this.store.setItem(SESSION_KEY, serialized);
    } catch {
      return;
    }
  }

  clear(): void {
    try {
      this.store.removeItem(SESSION_KEY);
    } catch {
      return;
    }
  }
}
