import { describe, expect, it } from "vitest";
import type { ProviderCapabilities } from "./contracts";
import { projectCapabilityUi } from "./capabilities";

const capabilities: ProviderCapabilities = {
  providerId: "vk",
  operations: {
    downloadVideo: "enabled",
    extractAudio: "disabled",
    selectQuality: "enabled",
    selectContainer: "disabled",
  },
  contentKinds: {
    video: "enabled",
    clip: "enabled",
    live: "disabled",
    playlist: "disabled",
  },
  metadata: {
    title: "enabled",
    duration: "enabled",
    thumbnail: "planned",
  },
};

describe("capability UI projection", () => {
  it("keeps legacy flow only when the contract is absent", () => {
    const legacy = projectCapabilityUi(undefined);
    expect(legacy.isLegacyContract).toBe(true);
    expect(legacy.canDownloadVideo).toBe(true);
    expect(legacy.canSelectQuality).toBe(true);

    const unknown = projectCapabilityUi(null);
    expect(unknown.isLegacyContract).toBe(false);
    expect(unknown.hasPublicProfile).toBe(false);
    expect(unknown.canDownloadVideo).toBe(false);
    expect(unknown.canSelectQuality).toBe(false);
  });

  it("allows only enabled operations from a known profile", () => {
    const enabled = projectCapabilityUi(capabilities);
    expect(enabled.canDownloadVideo).toBe(true);
    expect(enabled.canSelectQuality).toBe(true);
    expect(enabled.canExtractAudio).toBe(false);
    expect(enabled.canSelectContainer).toBe(false);
    expect(enabled.canShowThumbnail).toBe(false);

    const disabled = projectCapabilityUi({
      ...capabilities,
      operations: { ...capabilities.operations, downloadVideo: "disabled" },
    });
    const planned = projectCapabilityUi({
      ...capabilities,
      operations: { ...capabilities.operations, selectQuality: "planned" },
    });
    expect(disabled.canDownloadVideo).toBe(false);
    expect(planned.canSelectQuality).toBe(false);
  });
});
