/** @vitest-environment jsdom */
import { beforeEach, describe, expect, it } from "vitest";
import { MediaFlowController, type FlowSnapshot } from "./controller";
import type { MediaFormat } from "./contracts";
import { progressiveFormat } from "./fixtures";
import { groupStandaloneOptions } from "./quality";
import { renderFlow } from "./render";

const video: MediaFormat = { ...progressiveFormat, formatOptionId: "fmt_bbbbbbbbbbbbbbbbbbbbbbbbbbbb", mediaKind: "video_only", category: "video_only", hasAudio: false, audioCodec: "none", requiresPremium: true, freeTierEligible: false };
const audio: MediaFormat = { ...progressiveFormat, formatOptionId: "fmt_cccccccccccccccccccccccccccc", mediaKind: "audio_only", category: "audio_only", hasVideo: false, videoCodec: "none", height: null, width: null, fps: null, container: "m4a", bitrateKbps: 128, requiresPremium: true, freeTierEligible: false };
function snapshot(overrides: Partial<FlowSnapshot> = {}): FlowSnapshot {
  const formats = [progressiveFormat, video, audio];
  return { ...new MediaFlowController().snapshot(), phase: "inspected", premiumState: "free", formats, result: { formats, providerId: "vk", canonicalProviderUrl: "https://vk.com/video-1_2", mediaId: "-1_2", title: "Example", durationSeconds: 90, muxingRequired: false }, selectedFormatId: progressiveFormat.formatOptionId, downloadEligible: true, canSelectQuality: true, busy: false, ...overrides };
}
function tab(kind: string): HTMLButtonElement {
  return document.querySelector(`[data-category-tab="${kind}"]`)!;
}
function panel(kind: string): HTMLElement {
  return document.querySelector(`[data-category-panel="${kind}"]`)!;
}
beforeEach(() => {
  document.body.innerHTML = '<fieldset data-flow-quality><div data-flow-formats></div></fieldset><p data-flow-selection-summary></p><button data-flow-download></button>';
});

describe("A4 capability selector", () => {
  it("keeps category identity, one global radio selection and compact real metadata", () => {
    renderFlow(document, snapshot());
    expect([...document.querySelectorAll<HTMLElement>("[data-category-panel]")].map((p) => p.dataset.categoryPanel)).toEqual(["normal_video", "video_only", "audio_only"]);
    expect(document.querySelectorAll('input[name="formatOption"]')).toHaveLength(3);
    expect(document.querySelectorAll('input:checked')).toHaveLength(1);
    expect(panel("audio_only").querySelector(".format-label")?.textContent).toBe("128 кбит/с");
    expect(panel("audio_only").querySelector(".format-detail")?.textContent).toContain("M4A · ≈");
    expect(panel("audio_only").querySelector("input")?.getAttribute("aria-label")).toContain("Без изображения");
    expect(document.querySelectorAll(".format .premium-badge")).toHaveLength(0);
    expect(document.querySelectorAll(".category-card .premium-badge")).toHaveLength(2);
  });

  it("switches tabs with arrows/Home/End without changing selected format, including after rerender", () => {
    const state = snapshot();
    renderFlow(document, state);
    tab("normal_video").focus();
    tab("normal_video").dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }));
    expect(document.activeElement).toBe(tab("audio_only"));
    expect(panel("audio_only").dataset.active).toBe("true");
    expect(document.querySelector<HTMLInputElement>("input:checked")?.value).toBe(progressiveFormat.formatOptionId);
    renderFlow(document, state);
    expect(document.activeElement).toBe(tab("audio_only"));
    expect(tab("audio_only").getAttribute("aria-selected")).toBe("true");
    tab("audio_only").dispatchEvent(new KeyboardEvent("keydown", { key: "Home", bubbles: true }));
    expect(document.activeElement).toBe(tab("normal_video"));
    tab("normal_video").dispatchEvent(new KeyboardEvent("keydown", { key: "End", bubbles: true }));
    expect(document.activeElement).toBe(tab("audio_only"));
  });

  it("preserves radio focus through selection rendering and exposes just one selected option", () => {
    const state = snapshot({ premiumState: "active" });
    renderFlow(document, state);
    tab("audio_only").click();
    panel("audio_only").querySelector("input")!.focus();
    renderFlow(document, { ...state, selectedFormatId: audio.formatOptionId });
    expect((document.activeElement as HTMLInputElement).value).toBe(audio.formatOptionId);
    expect(document.querySelectorAll("input:checked")).toHaveLength(1);
    expect(document.querySelector("[data-flow-selection-summary]")?.textContent).toContain("Только аудио · 128 кбит/с · M4A");
  });

  it("starts on the valid selected capability and resets presentation for a new inspection", () => {
    renderFlow(document, snapshot({ premiumState: "active", selectedFormatId: video.formatOptionId }));
    expect(panel("video_only").dataset.active).toBe("true");
    renderFlow(document, snapshot());
    expect(panel("normal_video").dataset.active).toBe("true");
  });

  it.each([
    ["free", "Доступно с Premium", true],
    ["loading", "Проверяем Premium…", true],
    ["error", "Не удалось проверить статус Premium", true],
    ["active", "", false],
  ] as const)("renders %s without misrepresenting entitlement", (premiumState, help, disabled) => {
    renderFlow(document, snapshot({ premiumState }));
    expect(panel("audio_only").querySelector<HTMLInputElement>("input")?.disabled).toBe(disabled);
    expect(panel("audio_only").querySelector(".category-help")?.textContent).toBe(help);
    expect(panel("normal_video").querySelector<HTMLInputElement>("input")?.disabled).toBe(false);
  });

  it("handles unknown audio bitrate/size without fake resolution, size or MP3", () => {
    renderFlow(document, snapshot({ formats: [{ ...audio, bitrateKbps: null, approxBytes: null }] }));
    expect(panel("audio_only").querySelector(".format-label")?.textContent).toBe("Аудиодорожка");
    expect(panel("audio_only").querySelector(".format-detail")?.textContent).toBe("M4A");
    expect(panel("audio_only").textContent).not.toMatch(/MP3|720p|0 МБ|неизвестно/);
  });

  it("shows a single auto-selected normal option and technical empty states without paywalls", () => {
    renderFlow(document, snapshot({ formats: [progressiveFormat] }));
    expect(document.querySelector<HTMLElement>("[data-flow-quality]")?.hidden).toBe(false);
    expect(document.querySelector<HTMLInputElement>("input")?.checked).toBe(true);
    expect(panel("audio_only").textContent).toContain("Для этой ссылки нет доступных вариантов аудио");
    expect(panel("audio_only").textContent).not.toContain("Premium");
  });

  it("locks the common action while busy and removes selector during preparation", () => {
    renderFlow(document, snapshot({ busy: true }));
    expect(document.querySelector<HTMLButtonElement>("[data-flow-download]")?.disabled).toBe(true);
    renderFlow(document, snapshot({ phase: "enqueueing_download", busy: true }));
    expect(document.querySelector<HTMLElement>("[data-flow-quality]")?.hidden).toBe(true);
    expect(document.querySelector<HTMLElement>("[data-flow-selection-summary]")?.hidden).toBe(true);
  });
});

describe("A4 standalone display ordering", () => {
  it("orders known audio bitrate before unknown and preserves a selected duplicate deterministically", () => {
    const high = { ...audio, formatOptionId: "fmt_dddddddddddddddddddddddddddd", bitrateKbps: 192 };
    const unknown = { ...audio, formatOptionId: "fmt_eeeeeeeeeeeeeeeeeeeeeeeeeeee", bitrateKbps: null };
    const duplicate = { ...audio, formatOptionId: "fmt_ffffffffffffffffffffffffffff", container: "webm" };
    const formats = [unknown, duplicate, high, audio];
    const grouped = groupStandaloneOptions(formats, true, duplicate.formatOptionId);
    expect(grouped.map((o) => o.representative.bitrateKbps)).toEqual([192, 128, null]);
    expect(grouped[1].representative.formatOptionId).toBe(duplicate.formatOptionId);
    expect(groupStandaloneOptions([...formats].reverse(), true, duplicate.formatOptionId)).toEqual(grouped);
  });
  it("does not use video bitrate as resolution ordering and has stable ties", () => {
    const unknown = { ...video, formatOptionId: "fmt_dddddddddddddddddddddddddddd", height: null, bitrateKbps: 9000 };
    const high = { ...video, formatOptionId: "fmt_eeeeeeeeeeeeeeeeeeeeeeeeeeee", height: 1080 };
    const formats = [unknown, video, high];
    const grouped = groupStandaloneOptions(formats, false, null);
    expect(grouped.map((o) => o.representative.height)).toEqual([1080, 720, null]);
    expect(groupStandaloneOptions([...formats].reverse(), false, null)).toEqual(grouped);
  });
});
