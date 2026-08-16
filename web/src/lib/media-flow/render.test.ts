/**
 * @vitest-environment jsdom
 */
import { describe, expect, it } from "vitest";
import { renderFlow } from "./render";
import type { FlowSnapshot } from "./controller";
import { progressiveFormat } from "./fixtures";

const alternateFormat = {
  ...progressiveFormat,
  formatOptionId: "fmt_bbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  height: 480,
  qualityLabel: "p480",
};

function snapshot(partial: Partial<FlowSnapshot>): FlowSnapshot {
  return {
    phase: "inspected",
    statusText: "Choose a quality to download.",
    errorText: null,
    result: {
      providerId: "vk",
      canonicalProviderUrl: "https://vk.com/video-1_2",
      mediaId: "-1_2",
      title: "<img src=x onerror=alert(1)>",
      durationSeconds: 90,
      formats: [progressiveFormat, alternateFormat],
      muxingRequired: false,
    },
    formats: [progressiveFormat, alternateFormat],
    selectedFormatId: progressiveFormat.formatOptionId,
    downloadEligible: true,
    canSelectQuality: true,
    muxingBlocked: false,
    httpsRequired: false,
    grantArming: false,
    canNativeDownload: false,
    canRetryGrant: false,
    canSaveAs: false,
    downloadHref: null,
    nativeDownloadHandoff: false,
    browserUnsupported: false,
    busy: false,
    canSubmit: false,
    canStartOver: true,
    canCancelTask: false,
    restored: false,
    progressStage: null,
    progressPercent: null,
    artifactBytes: null,
    ...partial,
  };
}

describe("render", () => {
  it("renders provider title as text and uses opaque formatOptionId", () => {
    document.body.innerHTML = `
      <p data-flow-status></p>
      <article data-flow-meta><h2 data-flow-title></h2><span data-flow-provider></span><span data-flow-duration></span></article>
      <div data-flow-formats></div>
      <button data-flow-submit></button>
      <button data-flow-download></button>
      <a data-flow-native-download hidden download></a>
      <button data-flow-save-as></button>
      <button data-flow-reset></button>
      <p data-flow-mux></p>
      <p data-flow-https hidden></p>
      <p data-flow-grant-pending hidden></p>
      <p data-flow-handoff hidden></p>
    `;
    renderFlow(document, snapshot({}));
    const title = document.querySelector("[data-flow-title]");
    expect(title?.textContent).toBe("<img src=x onerror=alert(1)>");
    expect(title?.innerHTML).toBe("&lt;img src=x onerror=alert(1)&gt;");
    const radio = document.querySelector<HTMLInputElement>("input[type=radio]");
    expect(radio?.value).toBe(progressiveFormat.formatOptionId);
    expect(document.body.textContent ?? "").not.toMatch(/%|progress/i);
  });

  it("renders a null duration without NaN", () => {
    document.body.innerHTML = `
      <p data-flow-status></p>
      <article data-flow-meta><h2 data-flow-title></h2><span data-flow-provider></span><span data-flow-duration></span></article>
      <div data-flow-formats></div>
      <button data-flow-submit></button>
      <button data-flow-download></button>
      <a data-flow-native-download hidden download></a>
      <button data-flow-save-as></button>
      <button data-flow-reset></button>
      <p data-flow-mux></p>
      <p data-flow-https hidden></p>
      <p data-flow-grant-pending hidden></p>
      <p data-flow-handoff hidden></p>
    `;
    renderFlow(
      document,
      snapshot({
        result: {
          providerId: "vk",
          canonicalProviderUrl: "https://vk.com/video-1_2",
          mediaId: "-1_2",
          title: "Example",
          durationSeconds: null,
          formats: [progressiveFormat],
          muxingRequired: false,
        },
      }),
    );
    const duration = document.querySelector("[data-flow-duration]");
    expect(duration?.textContent).toBe("Duration unavailable");
    expect(duration?.textContent).not.toMatch(/NaN/);
  });

  it("enables derived progressive options and hides the unavailable hint", () => {
    document.body.innerHTML = `
      <div data-flow-formats></div>
      <p data-flow-mux>placeholder</p>
    `;
    renderFlow(document, snapshot({ muxingBlocked: false }));
    const radio = document.querySelector<HTMLInputElement>("input[type=radio]");
    expect(radio?.disabled).toBe(false);
    const mux = document.querySelector<HTMLElement>("[data-flow-mux]");
    expect(mux?.hidden).toBe(true);
  });

  it("shows quality choices only when policy allows and media has 2+ options", () => {
    document.body.innerHTML = `
      <fieldset data-flow-quality><div data-flow-formats></div></fieldset>
      <button data-flow-download></button>
    `;

    renderFlow(
      document,
      snapshot({
        canSelectQuality: true,
        formats: [progressiveFormat],
        result: {
          providerId: "vk",
          canonicalProviderUrl: "https://vk.com/video-1_2",
          mediaId: "-1_2",
          title: "Example",
          durationSeconds: 90,
          formats: [progressiveFormat],
          muxingRequired: false,
        },
        selectedFormatId: progressiveFormat.formatOptionId,
        downloadEligible: true,
      }),
    );
    expect(document.querySelector<HTMLElement>("[data-flow-quality]")?.hidden).toBe(true);
    expect(document.querySelector<HTMLButtonElement>("[data-flow-download]")?.disabled).toBe(
      false,
    );
    expect(document.querySelector("input[type=radio]")).toBeNull();

    renderFlow(document, snapshot({ canSelectQuality: true }));
    expect(document.querySelector<HTMLElement>("[data-flow-quality]")?.hidden).toBe(false);
    expect(document.querySelectorAll("input[type=radio]")).toHaveLength(2);

    renderFlow(document, snapshot({ canSelectQuality: false }));
    expect(document.querySelector<HTMLElement>("[data-flow-quality]")?.hidden).toBe(true);

    renderFlow(
      document,
      snapshot({
        canSelectQuality: true,
        formats: [],
        result: {
          providerId: "vk",
          canonicalProviderUrl: "https://vk.com/video-1_2",
          mediaId: "-1_2",
          title: "Example",
          durationSeconds: 90,
          formats: [],
          muxingRequired: false,
        },
        selectedFormatId: null,
        downloadEligible: false,
      }),
    );
    expect(document.querySelector<HTMLElement>("[data-flow-quality]")?.hidden).toBe(true);

    // Legacy snapshots omit canSelectQuality; treat as allowed when 2+ options exist.
    const legacySnapshot = snapshot({});
    delete legacySnapshot.canSelectQuality;
    renderFlow(document, legacySnapshot);
    expect(document.querySelector<HTMLElement>("[data-flow-quality]")?.hidden).toBe(false);
  });

  it("shows the unavailable hint only when no executable option exists", () => {
    document.body.innerHTML = `
      <div data-flow-formats></div>
      <p data-flow-mux>This media is not available in the current free download mode.</p>
    `;
    renderFlow(document, snapshot({ muxingBlocked: true, downloadEligible: false }));
    const radio = document.querySelector<HTMLInputElement>("input[type=radio]");
    expect(radio?.disabled).toBe(true);
    const mux = document.querySelector<HTMLElement>("[data-flow-mux]");
    expect(mux?.hidden).toBe(false);
    expect(mux?.textContent ?? "").not.toMatch(/muxing is not offered/i);
  });

  it("renders a native download anchor with a same-origin href", () => {
    document.body.innerHTML = `
      <p data-flow-status></p>
      <a data-flow-native-download hidden download></a>
      <p data-flow-grant-pending hidden></p>
      <p data-flow-handoff hidden></p>
      <p data-flow-https hidden></p>
      <section data-flow-progress hidden><p data-flow-progress-label></p><span data-flow-loader hidden></span></section>
    `;
    renderFlow(
      document,
      snapshot({
        phase: "ready",
        canNativeDownload: true,
        downloadHref: "/api/v1/media/browser-grants/bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee/content",
      }),
    );
    const link = document.querySelector<HTMLAnchorElement>("[data-flow-native-download]");
    expect(link?.tagName).toBe("A");
    expect(link?.getAttribute("href")).toBe(
      "/api/v1/media/browser-grants/bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee/content",
    );
    expect(link?.hidden).toBe(false);
  });
});
