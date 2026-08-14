/** Locale-independent byte formatting using 1024-based KB/MB/GB. */

const KB = 1024;
const MB = 1024 * 1024;
const GB = 1024 * 1024 * 1024;

export const UNKNOWN_SIZE_LABEL = "Size available after preparation";

export function formatByteSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0 || !Number.isInteger(bytes)) {
    return UNKNOWN_SIZE_LABEL;
  }
  if (bytes < KB) {
    return `${bytes} B`;
  }
  if (bytes < MB) {
    return `${(bytes / KB).toFixed(1)} KB`;
  }
  if (bytes < GB) {
    return `${(bytes / MB).toFixed(1)} MB`;
  }
  return `${(bytes / GB).toFixed(1)} GB`;
}

export function formatApproxBytes(bytes: number | null): string {
  if (bytes === null) {
    return UNKNOWN_SIZE_LABEL;
  }
  return `≈ ${formatByteSize(bytes)}`;
}

export function formatExactBytes(bytes: number): string {
  return formatByteSize(bytes);
}
