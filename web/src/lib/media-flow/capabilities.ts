import type { CapabilityState, ProviderCapabilities } from "./contracts";

export type CapabilityUiProjection = {
  isLegacyContract: boolean;
  hasPublicProfile: boolean;
  canDownloadVideo: boolean;
  canExtractAudio: boolean;
  canSelectQuality: boolean;
  canSelectContainer: boolean;
  canUseVideoContent: boolean;
  canUseClipContent: boolean;
  canUseLiveContent: boolean;
  canUsePlaylistContent: boolean;
  canShowTitle: boolean;
  canShowDuration: boolean;
  canShowThumbnail: boolean;
};

function enabledOrLegacy(
  capabilities: ProviderCapabilities | null | undefined,
  state: CapabilityState | undefined,
): boolean {
  if (capabilities === undefined) {
    return true;
  }
  if (capabilities === null) {
    return false;
  }
  return state === "enabled";
}

/**
 * Translate the optional public contract into UI decisions.
 *
 * `undefined` is an old backend and keeps its legacy format-based flow. `null`
 * explicitly means no public provider profile and therefore fails closed.
 */
export function projectCapabilityUi(
  capabilities: ProviderCapabilities | null | undefined,
): CapabilityUiProjection {
  return {
    isLegacyContract: capabilities === undefined,
    hasPublicProfile: capabilities !== null && capabilities !== undefined,
    canDownloadVideo: enabledOrLegacy(
      capabilities,
      capabilities?.operations.downloadVideo,
    ),
    canExtractAudio: enabledOrLegacy(
      capabilities,
      capabilities?.operations.extractAudio,
    ),
    canSelectQuality: enabledOrLegacy(
      capabilities,
      capabilities?.operations.selectQuality,
    ),
    canSelectContainer: enabledOrLegacy(
      capabilities,
      capabilities?.operations.selectContainer,
    ),
    canUseVideoContent: enabledOrLegacy(
      capabilities,
      capabilities?.contentKinds.video,
    ),
    canUseClipContent: enabledOrLegacy(
      capabilities,
      capabilities?.contentKinds.clip,
    ),
    canUseLiveContent: enabledOrLegacy(
      capabilities,
      capabilities?.contentKinds.live,
    ),
    canUsePlaylistContent: enabledOrLegacy(
      capabilities,
      capabilities?.contentKinds.playlist,
    ),
    canShowTitle: enabledOrLegacy(
      capabilities,
      capabilities?.metadata.title,
    ),
    canShowDuration: enabledOrLegacy(
      capabilities,
      capabilities?.metadata.duration,
    ),
    canShowThumbnail: enabledOrLegacy(
      capabilities,
      capabilities?.metadata.thumbnail,
    ),
  };
}
