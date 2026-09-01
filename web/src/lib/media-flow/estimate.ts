/**
 * Delivery rate formatting and estimated download duration for the READY state.
 */

const MB = 1024 * 1024;

export function pluralizeRussian(
  value: number,
  one: string,
  few: string,
  many: string,
): string {
  const abs = Math.abs(Math.round(value)) % 100;
  const rem = abs % 10;
  if (abs > 10 && abs < 20) {
    return many;
  }
  if (rem > 1 && rem < 5) {
    return few;
  }
  if (rem === 1) {
    return one;
  }
  return many;
}

/**
 * Calculate expected download duration in whole seconds (ceiling).
 * Returns null if size or rate is invalid or non-positive.
 */
export function estimateDownloadSeconds(
  artifactBytes: number | null | undefined,
  deliveryRateBytesPerSecond: number | null | undefined,
): number | null {
  if (
    artifactBytes == null ||
    deliveryRateBytesPerSecond == null ||
    !Number.isFinite(artifactBytes) ||
    !Number.isFinite(deliveryRateBytesPerSecond) ||
    artifactBytes <= 0 ||
    deliveryRateBytesPerSecond <= 0
  ) {
    return null;
  }
  return Math.ceil(artifactBytes / deliveryRateBytesPerSecond);
}

/**
 * Format download duration into human Russian copy with whole-number rounding.
 * < 60s: "Примерное время скачивания: ≈ 45 секунд"
 * 1–59m: "Примерное время скачивания: ≈ 18 минут"
 * >= 60m: "Примерное время скачивания: ≈ 1 ч 10 мин"
 */
export function formatEstimatedDownloadTime(
  seconds: number | null | undefined,
): string | null {
  if (seconds == null || !Number.isFinite(seconds) || seconds <= 0) {
    return null;
  }
  const roundedSec = Math.round(seconds);
  if (roundedSec < 60) {
    const word = pluralizeRussian(roundedSec, "секунда", "секунды", "секунд");
    return `Примерное время скачивания: ≈ ${roundedSec} ${word}`;
  }
  const totalMinutes = Math.round(roundedSec / 60);
  if (totalMinutes < 60) {
    const word = pluralizeRussian(totalMinutes, "минута", "минуты", "минут");
    return `Примерное время скачивания: ≈ ${totalMinutes} ${word}`;
  }
  const hours = Math.floor(totalMinutes / 60);
  const remainingMinutes = totalMinutes % 60;
  if (remainingMinutes === 0) {
    return `Примерное время скачивания: ≈ ${hours} ч`;
  }
  return `Примерное время скачивания: ≈ ${hours} ч ${remainingMinutes} мин`;
}

/**
 * Format effective delivery shaping rate to user-facing Russian copy.
 * E.g. 0.5 MB/s -> "Скорость до 0,5 МБ/с"
 * E.g. 1 MB/s -> "Скорость до 1 МБ/с"
 * null / <=0 -> null
 */
export function formatDeliverySpeed(
  bytesPerSecond: number | null | undefined,
): string | null {
  if (
    bytesPerSecond == null ||
    !Number.isFinite(bytesPerSecond) ||
    bytesPerSecond <= 0
  ) {
    return null;
  }
  const mbPerSec = bytesPerSecond / MB;
  const formatted = new Intl.NumberFormat("ru-RU", {
    maximumFractionDigits: 2,
  }).format(mbPerSec);
  return `Скорость до ${formatted} МБ/с`;
}
