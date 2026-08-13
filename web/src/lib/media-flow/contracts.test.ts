import { describe, expect, it } from "vitest";
import { FlowError } from "./errors";
import {
  RUTUBE_CANONICAL_HOSTS,
  VK_CANONICAL_HOSTS,
  isDownloadEligible,
  parseDownloadJob,
  parseInspectionJob,
  parseMediaFormat,
} from "./contracts";
import {
  DOWNLOAD_ID,
  OPTION_ID,
  STAMP,
  downloadPayload,
  inspectedPayload,
  inspectionPayload,
  progressiveFormat,
} from "./fixtures";

describe("contracts", () => {
  it("accepts valid create/status inspection payloads", () => {
    const queued = parseInspectionJob(inspectionPayload());
    expect(queued.state).toBe("queued");
    const inspected = parseInspectionJob(inspectedPayload());
    expect(inspected.result?.formats[0]?.formatOptionId).toBe(OPTION_ID);
  });

  it("accepts valid download payloads", () => {
    const job = parseDownloadJob(
      downloadPayload({
        state: "ready",
        artifactReady: true,
        completedAt: "2026-08-13T04:22:27Z",
      }),
    );
    expect(job.id).toBe(DOWNLOAD_ID);
    expect(job.artifactReady).toBe(true);
  });

  it("rejects missing and unknown fields", () => {
    expect(() => parseInspectionJob({ ...inspectionPayload(), extra: true })).toThrow(
      FlowError,
    );
    const missing = inspectionPayload();
    delete missing.statusPath;
    expect(() => parseInspectionJob(missing)).toThrow(FlowError);
  });

  it("rejects unknown states and wrong types", () => {
    expect(() => parseInspectionJob(inspectionPayload({ state: "running" }))).toThrow(
      FlowError,
    );
    expect(() => parseInspectionJob(inspectionPayload({ id: 1 }))).toThrow(FlowError);
  });

  it("rejects malformed formatOptionId and inconsistent A/V flags", () => {
    expect(() =>
      parseMediaFormat({ ...progressiveFormat, formatOptionId: "fmt_short" }),
    ).toThrow(FlowError);
    expect(() =>
      parseMediaFormat({
        ...progressiveFormat,
        category: "progressive",
        hasAudio: false,
      }),
    ).toThrow(FlowError);
  });

  it("rejects provider token / direct URL fields", () => {
    expect(() =>
      parseInspectionJob(inspectionPayload({ accessToken: "nope" })),
    ).toThrow(FlowError);
    expect(() =>
      parseMediaFormat({ ...progressiveFormat, providerFormatToken: "x" }),
    ).toThrow(FlowError);
    expect(() =>
      parseDownloadJob(downloadPayload({ downloadUrl: "https://cdn.example/file" })),
    ).toThrow(FlowError);
  });

  it("accepts a null durationSeconds", () => {
    const inspected = parseInspectionJob(inspectedPayload({ durationSeconds: null }));
    expect(inspected.result?.durationSeconds).toBeNull();
  });

  it("rejects invalid durationSeconds values", () => {
    expect(() => parseInspectionJob(inspectedPayload({ durationSeconds: -1 }))).toThrow(
      FlowError,
    );
    expect(() =>
      parseInspectionJob(inspectedPayload({ durationSeconds: Number.NaN })),
    ).toThrow(FlowError);
    expect(() =>
      parseInspectionJob(
        inspectedPayload({ durationSeconds: Number.POSITIVE_INFINITY }),
      ),
    ).toThrow(FlowError);
    expect(() =>
      parseInspectionJob(inspectedPayload({ durationSeconds: "90" })),
    ).toThrow(FlowError);
    expect(() =>
      parseInspectionJob(inspectedPayload({ durationSeconds: true })),
    ).toThrow(FlowError);
    const missing = inspectedPayload();
    const result = missing.result as Record<string, unknown>;
    delete result.durationSeconds;
    expect(() => parseInspectionJob(missing)).toThrow(FlowError);
  });

  it("accepts every backend VK and Rutube public host alias", () => {
    expect([...VK_CANONICAL_HOSTS]).toEqual([
      "vk.com",
      "www.vk.com",
      "m.vk.com",
      "vkvideo.ru",
      "www.vkvideo.ru",
      "m.vkvideo.ru",
    ]);
    expect([...RUTUBE_CANONICAL_HOSTS]).toEqual(["rutube.ru", "www.rutube.ru"]);
    for (const host of VK_CANONICAL_HOSTS) {
      expect(
        parseInspectionJob(
          inspectedPayload({
            providerId: "vk",
            canonicalProviderUrl: `https://${host}/video-1_2`,
            mediaId: "-1_2",
          }),
        ).result?.canonicalProviderUrl,
      ).toBe(`https://${host}/video-1_2`);
    }
    for (const host of RUTUBE_CANONICAL_HOSTS) {
      expect(
        parseInspectionJob(
          inspectedPayload({
            providerId: "rutube",
            canonicalProviderUrl: `https://${host}/video/abc_1-2`,
            mediaId: "abc_1-2",
          }),
        ).result?.canonicalProviderUrl,
      ).toBe(`https://${host}/video/abc_1-2`);
    }
  });

  it("rejects provider host lookalikes and suffix matches", () => {
    const lookalikes = [
      "https://vk.com.evil/video-1_2",
      "https://evilvk.com/video-1_2",
      "https://notvk.com/video-1_2",
      "https://vkvideo.ru.evil.com/video-1_2",
      "https://evil-vkvideo.ru/video-1_2",
      "https://m.vkvideo.ru.attacker/video-1_2",
      "https://rutube.ru.evil.com/video/abc_1-2",
      "https://www.rutube.ru.attacker/video/abc_1-2",
      "https://not-rutube.ru/video/abc_1-2",
      "https://youtube.com/video-1_2",
    ];
    for (const canonicalProviderUrl of lookalikes) {
      const providerId = canonicalProviderUrl.includes("rutube") ? "rutube" : "vk";
      const mediaId = providerId === "rutube" ? "abc_1-2" : "-1_2";
      expect(() =>
        parseInspectionJob(inspectedPayload({ canonicalProviderUrl, providerId, mediaId })),
      ).toThrow(FlowError);
    }
  });

  it("rejects http, explicit ports, credentials, query, fragment, and IPv6-like authority", () => {
    const rejected = [
      "http://vk.com/video-1_2",
      "HTTP://vk.com/video-1_2",
      "https://vk.com:443/video-1_2",
      "HTTPS://vk.com:443/video-1_2",
      "https://vk.com:8443/video-1_2",
      "https://m.vkvideo.ru:443/video-1_2",
      "https://user:pass@vk.com/video-1_2",
      "https://user@vk.com/video-1_2",
      "https://vk.com/video-1_2?x=1",
      "https://vk.com/video-1_2#clip",
      "https://[::1]/video-1_2",
      "https://[::1]:443/video-1_2",
      "https://vk.com@[::1]/video-1_2",
    ];
    for (const canonicalProviderUrl of rejected) {
      expect(() => parseInspectionJob(inspectedPayload({ canonicalProviderUrl }))).toThrow(
        FlowError,
      );
    }
  });

  it("validates inspection state/timestamp coherence for every state", () => {
    const cases: Array<{
      name: string;
      payload: Record<string, unknown>;
      ok: boolean;
    }> = [
      { name: "queued", payload: inspectionPayload({ state: "queued" }), ok: true },
      {
        name: "inspecting",
        payload: inspectionPayload({ state: "inspecting" }),
        ok: true,
      },
      { name: "inspected", payload: inspectedPayload(), ok: true },
      {
        name: "failed",
        payload: inspectionPayload({
          state: "failed",
          completedAt: STAMP,
          errorCode: "MEDIA_INSPECTION_FAILED",
        }),
        ok: true,
      },
      {
        name: "expired from queued",
        payload: inspectionPayload({
          state: "expired",
          completedAt: "2099-01-01T00:00:01Z",
          updatedAt: "2099-01-01T00:00:01Z",
        }),
        ok: true,
      },
      {
        name: "queued with completedAt",
        payload: inspectionPayload({ state: "queued", completedAt: STAMP }),
        ok: false,
      },
      {
        name: "inspecting with completedAt",
        payload: inspectionPayload({ state: "inspecting", completedAt: STAMP }),
        ok: false,
      },
      {
        name: "inspected without completedAt",
        payload: { ...inspectedPayload(), completedAt: null },
        ok: false,
      },
      {
        name: "failed without completedAt",
        payload: inspectionPayload({
          state: "failed",
          errorCode: "MEDIA_INSPECTION_FAILED",
        }),
        ok: false,
      },
      {
        name: "expired without completedAt",
        payload: inspectionPayload({ state: "expired" }),
        ok: false,
      },
      {
        name: "inspected completedAt after expiresAt",
        payload: inspectionPayload({
          state: "inspected",
          completedAt: "2099-01-02T00:00:00Z",
          result: inspectedPayload().result,
        }),
        ok: false,
      },
      {
        name: "createdAt after updatedAt",
        payload: inspectionPayload({ updatedAt: "2020-01-01T00:00:00Z" }),
        ok: false,
      },
    ];
    for (const row of cases) {
      if (row.ok) {
        expect(parseInspectionJob(row.payload).state, row.name).toBeTruthy();
      } else {
        expect(() => parseInspectionJob(row.payload), row.name).toThrow(FlowError);
      }
    }
  });

  it("validates download state/timestamp coherence for every state", () => {
    const cases: Array<{
      name: string;
      payload: Record<string, unknown>;
      ok: boolean;
    }> = [
      { name: "queued", payload: downloadPayload({ state: "queued" }), ok: true },
      {
        name: "downloading",
        payload: downloadPayload({ state: "downloading" }),
        ok: true,
      },
      {
        name: "ready",
        payload: downloadPayload({
          state: "ready",
          artifactReady: true,
          completedAt: STAMP,
        }),
        ok: true,
      },
      {
        name: "failed",
        payload: downloadPayload({
          state: "failed",
          completedAt: STAMP,
          errorCode: "DOWNLOAD_TOOL_FAILED",
        }),
        ok: true,
      },
      {
        name: "expired from queued",
        payload: downloadPayload({
          state: "expired",
          completedAt: "2099-01-01T00:00:01Z",
          updatedAt: "2099-01-01T00:00:01Z",
        }),
        ok: true,
      },
      {
        name: "queued with completedAt",
        payload: downloadPayload({ state: "queued", completedAt: STAMP }),
        ok: false,
      },
      {
        name: "downloading with completedAt",
        payload: downloadPayload({ state: "downloading", completedAt: STAMP }),
        ok: false,
      },
      {
        name: "ready without completedAt",
        payload: downloadPayload({ state: "ready", artifactReady: true }),
        ok: false,
      },
      {
        name: "failed without completedAt",
        payload: downloadPayload({
          state: "failed",
          errorCode: "DOWNLOAD_TOOL_FAILED",
        }),
        ok: false,
      },
      {
        name: "expired without completedAt",
        payload: downloadPayload({ state: "expired" }),
        ok: false,
      },
      {
        name: "ready completedAt after expiresAt",
        payload: downloadPayload({
          state: "ready",
          artifactReady: true,
          completedAt: "2099-01-02T00:00:00Z",
        }),
        ok: false,
      },
    ];
    for (const row of cases) {
      if (row.ok) {
        expect(parseDownloadJob(row.payload).state, row.name).toBeTruthy();
      } else {
        expect(() => parseDownloadJob(row.payload), row.name).toThrow(FlowError);
      }
    }
  });

  it("rejects missing extraction metadata", () => {
    const missingTool = inspectedPayload();
    delete (missingTool.result as Record<string, unknown>).extractionTool;
    expect(() => parseInspectionJob(missingTool)).toThrow(FlowError);
    expect(() =>
      parseInspectionJob(inspectedPayload({ extractionToolVersion: undefined })),
    ).toThrow(FlowError);
  });

  it("rejects ready download without artifactReady", () => {
    expect(() => parseDownloadJob(downloadPayload({ state: "ready" }))).toThrow(
      FlowError,
    );
  });

  it("marks only progressive free-tier A/V as downloadable", () => {
    expect(isDownloadEligible(progressiveFormat)).toBe(true);
    expect(
      isDownloadEligible({
        ...progressiveFormat,
        category: "video_only",
        hasAudio: false,
        freeTierEligible: true,
      }),
    ).toBe(false);
  });
});
