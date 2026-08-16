import { isDownloadEligible, type MediaFormat } from "./contracts";

/**
 * Quality grouping for the public inspection response.
 *
 * The response can expose several formats that describe the same visible
 * quality (for example MP4 and WebM at 720p). The UI shows one representative
 * per normalized quality. Grouping and ranking are pure and independent of the
 * input order; representative ids are always the opaque ids returned by the
 * API, never constructed here.
 */
export type QualityOption = {
  /** Normalized group key: `h:<height>` when a numeric height exists, else `l:<label>`. */
  key: string;
  /** The single format shown for this quality. */
  representative: MediaFormat;
  /** All grouped formats, ordered by the representative ranking. */
  members: readonly MediaFormat[];
  /** Human-first row text, for example `High (720p)`. */
  label: string;
  /** True when the representative can actually be enqueued. */
  eligible: boolean;
};

export type QualityGroupingOptions = {
  muxingBlocked?: boolean;
  selectedFormatId?: string | null;
};

const CONTAINER_RANK: Readonly<Record<string, number>> = Object.freeze({
  mp4: 0,
  webm: 1,
});
const UNKNOWN_CONTAINER_RANK = 2;

function containerRank(container: string): number {
  return CONTAINER_RANK[container] ?? UNKNOWN_CONTAINER_RANK;
}

function finiteHeight(format: MediaFormat): number | null {
  const height = format.height;
  if (typeof height !== "number" || !Number.isFinite(height) || height <= 0) {
    return null;
  }
  return Math.trunc(height);
}

/** Strict label normalization: only identical labels may share a group. */
export function normalizeQualityLabel(label: string): string {
  return label.trim().toLowerCase().replace(/\s+/g, " ");
}

export function qualityGroupKey(format: MediaFormat): string {
  const height = finiteHeight(format);
  if (height !== null) {
    return `h:${height}`;
  }
  return `l:${normalizeQualityLabel(format.qualityLabel)}`;
}

function shorthandHeight(label: string): number | null {
  const shorthand = /^p(\d+)$/i.exec(label.trim());
  if (!shorthand) {
    return null;
  }
  const parsed = Number.parseInt(shorthand[1], 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

export function qualityTierLabel(format: MediaFormat): string {
  const height = finiteHeight(format) ?? shorthandHeight(format.qualityLabel);
  if (height === null) {
    return "Качество";
  }
  if (height >= 720) {
    return "Высокое";
  }
  if (height >= 480) {
    return "Среднее";
  }
  return "Низкое";
}

function qualityTechnicalLabel(format: MediaFormat): string {
  const height = finiteHeight(format);
  if (height !== null) {
    return `${height}p`;
  }
  const label = format.qualityLabel.trim();
  const inferredHeight = shorthandHeight(label);
  if (inferredHeight !== null) {
    return `${inferredHeight}p`;
  }
  return label.length > 0 ? label : "Неизвестное качество";
}

export function qualityDisplayLabel(format: MediaFormat): string {
  return `${qualityTierLabel(format)} (${qualityTechnicalLabel(format)})`;
}

export function isDisplayableFormat(format: MediaFormat): boolean {
  return format.category === "progressive" && format.hasVideo && format.hasAudio;
}

export function isSelectableFormat(
  format: MediaFormat,
  muxingBlocked = false,
): boolean {
  return !muxingBlocked && isDownloadEligible(format);
}

/**
 * Stable representative ranking inside one quality group:
 * 1. eligible before ineligible;
 * 2. the already selected id (restored sessions keep their exact format);
 * 3. MP4 before WebM before other containers, then container name;
 * 4. higher finite FPS, unknown FPS last;
 * 5. smaller known approximate size, unknown size last;
 * 6. `formatOptionId` ascending as the final tie-breaker.
 */
export function compareRepresentatives(
  left: MediaFormat,
  right: MediaFormat,
  options: QualityGroupingOptions = {},
): number {
  const muxingBlocked = options.muxingBlocked === true;
  const selectedFormatId = options.selectedFormatId ?? null;

  const eligibility =
    Number(!isSelectableFormat(left, muxingBlocked)) -
    Number(!isSelectableFormat(right, muxingBlocked));
  if (eligibility !== 0) {
    return eligibility;
  }

  const selected =
    Number(left.formatOptionId !== selectedFormatId) -
    Number(right.formatOptionId !== selectedFormatId);
  if (selected !== 0) {
    return selected;
  }

  const container = containerRank(left.container) - containerRank(right.container);
  if (container !== 0) {
    return container;
  }
  if (left.container !== right.container) {
    return left.container < right.container ? -1 : 1;
  }

  const leftFps =
    typeof left.fps === "number" && Number.isFinite(left.fps) ? left.fps : null;
  const rightFps =
    typeof right.fps === "number" && Number.isFinite(right.fps) ? right.fps : null;
  if (leftFps !== rightFps) {
    if (leftFps === null) {
      return 1;
    }
    if (rightFps === null) {
      return -1;
    }
    return rightFps - leftFps;
  }

  const leftBytes = left.approxBytes;
  const rightBytes = right.approxBytes;
  if (leftBytes !== rightBytes) {
    if (leftBytes === null) {
      return 1;
    }
    if (rightBytes === null) {
      return -1;
    }
    return leftBytes - rightBytes;
  }

  return left.formatOptionId.localeCompare(right.formatOptionId);
}

function groupHeight(key: string): number | null {
  if (!key.startsWith("h:")) {
    return null;
  }
  const height = Number.parseInt(key.slice(2), 10);
  return Number.isFinite(height) ? height : null;
}

function compareOptions(left: QualityOption, right: QualityOption): number {
  const leftHeight = groupHeight(left.key);
  const rightHeight = groupHeight(right.key);
  if (leftHeight !== null && rightHeight !== null) {
    if (leftHeight !== rightHeight) {
      return rightHeight - leftHeight;
    }
  } else if (leftHeight !== null) {
    return -1;
  } else if (rightHeight !== null) {
    return 1;
  }
  if (left.key !== right.key) {
    return left.key < right.key ? -1 : 1;
  }
  return left.representative.formatOptionId.localeCompare(
    right.representative.formatOptionId,
  );
}

/** One representative per normalized quality, highest quality first. */
export function groupQualityOptions(
  formats: readonly MediaFormat[],
  options: QualityGroupingOptions = {},
): QualityOption[] {
  const muxingBlocked = options.muxingBlocked === true;
  const groups = new Map<string, MediaFormat[]>();
  for (const format of formats) {
    if (!isDisplayableFormat(format)) {
      continue;
    }
    const key = qualityGroupKey(format);
    const bucket = groups.get(key);
    if (bucket) {
      bucket.push(format);
    } else {
      groups.set(key, [format]);
    }
  }
  const grouped = [...groups.entries()].map(([key, members]) => {
    const ordered = [...members].sort((left, right) =>
      compareRepresentatives(left, right, options),
    );
    const representative = ordered[0];
    return {
      key,
      representative,
      members: ordered,
      label: qualityDisplayLabel(representative),
      eligible: isSelectableFormat(representative, muxingBlocked),
    } satisfies QualityOption;
  });
  grouped.sort(compareOptions);
  return grouped;
}

/**
 * Highest eligible representative at or below the free 720p limit.
 * Shares the ranking with {@link groupQualityOptions}, so the default selection
 * is always one of the rows the user can see.
 */
export function pickHighestEligibleFormat(
  formats: readonly MediaFormat[],
  options: QualityGroupingOptions = {},
): string | null {
  const eligible = groupQualityOptions(formats, options).filter(
    (option) => option.eligible && (option.representative.height ?? 0) <= 720,
  );
  if (eligible.length === 0) {
    return null;
  }
  return eligible[0].representative.formatOptionId;
}

/**
 * Keeps a restored `formatOptionId` when the response still offers it as a
 * usable option. Returns null when it is gone or no longer selectable so the
 * caller can fall back to the documented default instead of sending an id the
 * backend would reject.
 */
export function reconcileSelectedFormatId(
  formats: readonly MediaFormat[],
  selectedFormatId: string | null,
  options: QualityGroupingOptions = {},
): string | null {
  if (!selectedFormatId) {
    return null;
  }
  const match = formats.find(
    (format) =>
      format.formatOptionId === selectedFormatId &&
      isDisplayableFormat(format) &&
      isSelectableFormat(format, options.muxingBlocked === true),
  );
  return match ? match.formatOptionId : null;
}
