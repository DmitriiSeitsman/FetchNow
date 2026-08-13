/**
 * @vitest-environment jsdom
 */
import { describe, expect, it } from "vitest";
import { renderFlow } from "./render";
import type { FlowSnapshot } from "./controller";
import { progressiveFormat } from "./fixtures";

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
      formats: [progressiveFormat],
      muxingRequired: false,
    },
    formats: [progressiveFormat],
    selectedFormatId: progressiveFormat.formatOptionId,
    downloadEligible: true,
    muxingBlocked: false,
    browserUnsupported: false,
    busy: false,
    canSubmit: false,
    canSave: false,
    canStartOver: true,
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
      <button data-flow-save></button>
      <button data-flow-reset></button>
      <p data-flow-mux></p>
      <p data-flow-browser></p>
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
      <button data-flow-save></button>
      <button data-flow-reset></button>
      <p data-flow-mux></p>
      <p data-flow-browser></p>
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
});
