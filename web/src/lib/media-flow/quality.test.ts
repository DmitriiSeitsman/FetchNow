import { describe, expect, it } from "vitest";
import {
  groupQualityOptions,
  normalizeQualityLabel,
  pickHighestEligibleFormat,
  qualityGroupKey,
  reconcileSelectedFormatId,
} from "./quality";
import type { MediaFormat } from "./contracts";
import { progressiveFormat } from "./fixtures";

function id(suffix: string): string {
  return `fmt_${suffix.padStart(28, "0")}`;
}

const MP4_720 = id("a1");
const WEBM_720 = id("b2");
const MP4_720_HIGH_FPS = id("c3");
const MP4_720_BLOCKED = id("d4");
const MP4_1080 = id("e5");
const MP4_480 = id("f6");
const LABEL_ONLY_A = id("1a");
const LABEL_ONLY_B = id("2b");
const ODD_LABEL = id("3c");

function format(overrides: Partial<MediaFormat>): MediaFormat {
  return { ...progressiveFormat, ...overrides };
}

function permutations<T>(items: readonly T[]): T[][] {
  if (items.length <= 1) {
    return [[...items]];
  }
  const out: T[][] = [];
  for (let index = 0; index < items.length; index += 1) {
    const rest = [...items.slice(0, index), ...items.slice(index + 1)];
    for (const tail of permutations(rest)) {
      out.push([items[index], ...tail]);
    }
  }
  return out;
}

function representativeIds(formats: readonly MediaFormat[], selectedFormatId?: string) {
  return groupQualityOptions(formats, { selectedFormatId }).map(
    (option) => option.representative.formatOptionId,
  );
}

describe("quality grouping", () => {
  it("keeps one representative for two eligible 720p formats and prefers MP4", () => {
    const formats = [
      format({ formatOptionId: WEBM_720, container: "webm" }),
      format({ formatOptionId: MP4_720, container: "mp4" }),
    ];
    const options = groupQualityOptions(formats);
    expect(options).toHaveLength(1);
    expect(options[0].representative.formatOptionId).toBe(MP4_720);
    expect(options[0].members).toHaveLength(2);
    expect(options[0].label).toBe("720p");
    expect(options[0].eligible).toBe(true);
  });

  it("prefers MP4 over a WebM duplicate even when WebM reports a higher FPS", () => {
    const formats = [
      format({ formatOptionId: WEBM_720, container: "webm", fps: 60 }),
      format({ formatOptionId: MP4_720, container: "mp4", fps: 30 }),
    ];
    expect(representativeIds(formats)).toEqual([MP4_720]);
  });

  it("never hides an eligible format behind an ineligible duplicate", () => {
    const formats = [
      format({ formatOptionId: MP4_720_BLOCKED, freeTierEligible: false }),
      format({ formatOptionId: MP4_720 }),
    ];
    const options = groupQualityOptions(formats);
    expect(options).toHaveLength(1);
    expect(options[0].representative.formatOptionId).toBe(MP4_720);
    expect(options[0].eligible).toBe(true);
  });

  it("ranks higher finite FPS, then smaller known size, then the format id", () => {
    const byFps = [
      format({ formatOptionId: MP4_720, fps: 30 }),
      format({ formatOptionId: MP4_720_HIGH_FPS, fps: 60 }),
      format({ formatOptionId: WEBM_720, container: "mp4", fps: null }),
    ];
    expect(representativeIds(byFps)).toEqual([MP4_720_HIGH_FPS]);

    const bySize = [
      format({ formatOptionId: MP4_720, approxBytes: 20_000_000 }),
      format({ formatOptionId: MP4_720_HIGH_FPS, approxBytes: 9_000_000 }),
      format({ formatOptionId: WEBM_720, container: "mp4", approxBytes: null }),
    ];
    expect(representativeIds(bySize)).toEqual([MP4_720_HIGH_FPS]);

    const byId = [
      format({ formatOptionId: MP4_720_HIGH_FPS }),
      format({ formatOptionId: MP4_720 }),
    ];
    expect(representativeIds(byId)).toEqual([MP4_720]);
  });

  it("groups formats without a height by their strictly normalized label", () => {
    const formats = [
      format({
        formatOptionId: LABEL_ONLY_A,
        height: null,
        qualityLabel: " HD  Ready ",
      }),
      format({ formatOptionId: LABEL_ONLY_B, height: null, qualityLabel: "hd ready" }),
      format({ formatOptionId: ODD_LABEL, height: null, qualityLabel: "source ??" }),
    ];
    const options = groupQualityOptions(formats);
    expect(options).toHaveLength(2);
    expect(options.map((option) => option.key)).toEqual(["l:hd ready", "l:source ??"]);
    expect(options[1].label).toBe("source ??");
    expect(normalizeQualityLabel(" HD  Ready ")).toBe("hd ready");
    expect(qualityGroupKey(format({ height: 720 }))).toBe("h:720");
  });

  it("does not merge a malformed label group with a numeric height group", () => {
    const formats = [
      format({ formatOptionId: MP4_720 }),
      format({ formatOptionId: ODD_LABEL, height: null, qualityLabel: "p720" }),
    ];
    const options = groupQualityOptions(formats);
    expect(options).toHaveLength(2);
    expect(options[0].key).toBe("h:720");
    expect(options[1].key).toBe("l:p720");
    expect(options[1].label).toBe("720p");
  });

  it("orders several heights from highest to lowest with label groups last", () => {
    const formats = [
      format({ formatOptionId: MP4_480, height: 480, qualityLabel: "p480" }),
      format({ formatOptionId: ODD_LABEL, height: null, qualityLabel: "source" }),
      format({ formatOptionId: MP4_1080, height: 1080, freeTierEligible: false }),
      format({ formatOptionId: MP4_720 }),
    ];
    expect(representativeIds(formats)).toEqual([MP4_1080, MP4_720, MP4_480, ODD_LABEL]);
  });

  it("produces the same rows for every input order", () => {
    const formats = [
      format({ formatOptionId: WEBM_720, container: "webm" }),
      format({ formatOptionId: MP4_720 }),
      format({ formatOptionId: MP4_1080, height: 1080, freeTierEligible: false }),
      format({ formatOptionId: MP4_480, height: 480, qualityLabel: "p480" }),
    ];
    const expected = representativeIds(formats);
    expect(expected).toEqual([MP4_1080, MP4_720, MP4_480]);
    for (const permutation of permutations(formats)) {
      expect(representativeIds(permutation)).toEqual(expected);
      expect(pickHighestEligibleFormat(permutation)).toBe(MP4_720);
    }
  });

  it("keeps a restored duplicate selection as its own group representative", () => {
    const formats = [
      format({ formatOptionId: MP4_720 }),
      format({ formatOptionId: WEBM_720, container: "webm" }),
    ];
    const options = groupQualityOptions(formats, { selectedFormatId: WEBM_720 });
    expect(options).toHaveLength(1);
    expect(options[0].representative.formatOptionId).toBe(WEBM_720);
    expect(reconcileSelectedFormatId(formats, WEBM_720)).toBe(WEBM_720);
  });

  it("drops a restored selection that the response no longer offers", () => {
    const formats = [format({ formatOptionId: MP4_720 })];
    expect(reconcileSelectedFormatId(formats, WEBM_720)).toBeNull();
    expect(reconcileSelectedFormatId(formats, null)).toBeNull();
    expect(
      reconcileSelectedFormatId(
        [format({ formatOptionId: MP4_720_BLOCKED, freeTierEligible: false })],
        MP4_720_BLOCKED,
      ),
    ).toBeNull();
    expect(
      reconcileSelectedFormatId(formats, MP4_720, { muxingBlocked: true }),
    ).toBeNull();
  });

  it("reports no eligible representative when nothing can be downloaded", () => {
    const formats = [
      format({ formatOptionId: MP4_1080, height: 1080, freeTierEligible: false }),
      format({ formatOptionId: MP4_720_BLOCKED, freeTierEligible: false }),
    ];
    const options = groupQualityOptions(formats);
    expect(options.every((option) => !option.eligible)).toBe(true);
    expect(pickHighestEligibleFormat(formats)).toBeNull();
    expect(
      pickHighestEligibleFormat([format({ formatOptionId: MP4_720 })], {
        muxingBlocked: true,
      }),
    ).toBeNull();
    expect(pickHighestEligibleFormat([])).toBeNull();
  });

  it("skips formats that are not a single progressive video+audio file", () => {
    const formats = [
      format({
        formatOptionId: LABEL_ONLY_A,
        category: "video_only",
        hasAudio: false,
        height: 720,
      }),
      format({ formatOptionId: MP4_720 }),
    ];
    expect(representativeIds(formats)).toEqual([MP4_720]);
  });

  it("only ever returns opaque ids that came from the response", () => {
    const formats = [
      format({ formatOptionId: WEBM_720, container: "webm" }),
      format({ formatOptionId: MP4_720 }),
      format({ formatOptionId: MP4_1080, height: 1080, freeTierEligible: false }),
    ];
    const known = new Set(formats.map((item) => item.formatOptionId));
    for (const option of groupQualityOptions(formats)) {
      expect(known.has(option.representative.formatOptionId)).toBe(true);
    }
    const picked = pickHighestEligibleFormat(formats);
    expect(picked !== null && known.has(picked)).toBe(true);
  });

  it("agrees with the default selection shown in the rows", () => {
    const formats = [
      format({ formatOptionId: WEBM_720, container: "webm" }),
      format({ formatOptionId: MP4_720 }),
      format({ formatOptionId: MP4_480, height: 480, qualityLabel: "p480" }),
      format({ formatOptionId: MP4_1080, height: 1080, freeTierEligible: false }),
    ];
    const picked = pickHighestEligibleFormat(formats);
    expect(representativeIds(formats)).toContain(picked);
    expect(picked).toBe(MP4_720);
  });
});
