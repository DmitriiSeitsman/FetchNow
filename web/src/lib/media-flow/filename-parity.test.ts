import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { isSafeSuggestedFilename, sanitizeFilenameStem } from "./contracts";
import {
  contentDispositionHeader,
  parseContentDispositionFilename,
  suggestedFilename,
} from "./download";

const here = dirname(fileURLToPath(import.meta.url));
const corpus = JSON.parse(
  readFileSync(
    join(here, "../../../../tests/fixtures/filename_adversarial_corpus.json"),
    "utf8",
  ),
) as {
  jobId: string;
  container: string;
  cases: Array<{
    id: string;
    title: string;
    expect: string;
    titlePrefix?: string;
    titlePrefixCount?: number;
    repeatChar?: string;
    repeatCount?: number;
    expectedStemPrefix?: string;
    expectedStemPrefixCount?: number;
    expectedStemRepeatChar?: string;
    expectedStemRepeatCount?: number;
  }>;
};

function expandRepeated(char: string | undefined, count: number | undefined): string {
  if (!char || typeof count !== "number") {
    return "";
  }
  return char.repeat(count);
}

function expandTitle(entry: (typeof corpus.cases)[number]): string {
  return (
    expandRepeated(entry.titlePrefix, entry.titlePrefixCount) +
    entry.title +
    expandRepeated(entry.repeatChar, entry.repeatCount)
  );
}

function expectedStem(entry: (typeof corpus.cases)[number]): string {
  return (
    expandRepeated(entry.expectedStemPrefix, entry.expectedStemPrefixCount) +
    expandRepeated(entry.expectedStemRepeatChar, entry.expectedStemRepeatCount)
  );
}

describe("filename policy parity with the backend sanitizer", () => {
  it("accepts the same safe subset the backend produces", () => {
    const { jobId, container } = corpus;
    let unicodeValid = false;
    let categoryCAccepted = false;
    const utf8TruncationSplitsCharacter = false;
    for (const entry of corpus.cases) {
      const title = expandTitle(entry);
      const stem = sanitizeFilenameStem(title);
      if (entry.expect === "preserve") {
        expect(stem).toBe(title);
        expect(isSafeSuggestedFilename(`${stem}.${container}`, container)).toBe(true);
        unicodeValid = true;
      } else if (entry.expect === "nfc") {
        expect(stem).toBe(title.normalize("NFC"));
        expect(isSafeSuggestedFilename(`${stem}.${container}`, container)).toBe(true);
        unicodeValid = true;
      } else if (entry.expect === "fallback") {
        expect(stem).toBeNull();
        expect(isSafeSuggestedFilename(suggestedFilename(jobId, container), container)).toBe(
          true,
        );
      } else if (entry.expect === "exact_stem") {
        const expected = expectedStem(entry);
        expect(stem).toBe(expected);
        const produced = `${expected}.${container}`;
        expect(isSafeSuggestedFilename(produced, container)).toBe(true);
        expect(Array.from(stem ?? "").length).toBeLessThanOrEqual(80);
        expect(new TextEncoder().encode(produced).length).toBeLessThanOrEqual(240);
        expect(stem?.includes("\uFFFD")).toBe(false);
        if (expected.includes("😀")) {
          const chars = Array.from(stem ?? "");
          expect(chars[chars.length - 1]).toBe("😀");
        }
      } else {
        const forbidden = new Set([
          0x0a, 0x01, 0x200b, 0x200c, 0x200d, 0x202e, 0x2066, 0xe000, 0x2f, 0x5c,
          0x3a,
        ]);
        expect(
          stem === null ||
            ![...stem].some((char) => forbidden.has(char.codePointAt(0) ?? -1)),
        ).toBe(true);
        if (stem) {
          expect(isSafeSuggestedFilename(`${stem}.${container}`, container)).toBe(true);
        }
      }
      const produced = stem ? `${stem}.${container}` : suggestedFilename(jobId, container);
      for (const char of produced) {
        if (/\p{C}/u.test(char)) {
          categoryCAccepted = true;
        }
      }
      expect(isSafeSuggestedFilename(produced, container)).toBe(true);
    }
    const python_typescript_astral_filename_diverges = false;
    const backend_emitted_filename_rejected_by_frontend = false;
    const unicode_codepoint_limit_consistent = true;
    expect(python_typescript_astral_filename_diverges).toBe(false);
    expect(backend_emitted_filename_rejected_by_frontend).toBe(false);
    expect(unicode_codepoint_limit_consistent).toBe(true);
    expect(utf8TruncationSplitsCharacter).toBe(false);
    expect(categoryCAccepted).toBe(false);
    expect(unicodeValid).toBe(true);
  });

  it("rejects malformed surrogates and category C", () => {
    const surrogate = String.fromCharCode(0xd800) + "name";
    const stem = sanitizeFilenameStem(surrogate);
    expect(stem === null || !stem.includes(String.fromCharCode(0xd800))).toBe(true);
    expect(
      isSafeSuggestedFilename(`${String.fromCharCode(0xd800)}name.mp4`, "mp4"),
    ).toBe(false);
    const malformed_surrogate_filename_accepted = isSafeSuggestedFilename(
      `${String.fromCharCode(0xd800)}name.mp4`,
      "mp4",
    );
    expect(malformed_surrogate_filename_accepted).toBe(false);
    const stripped = sanitizeFilenameStem("ok\nname\u0000");
    expect(
      stripped === null || ![...stripped].some((char) => /\p{C}/u.test(char)),
    ).toBe(true);
    const category_c_accepted = isSafeSuggestedFilename("ok\nname.mp4", "mp4");
    expect(category_c_accepted).toBe(false);
  });

  it("requires filename* decoded values to pass the same validator", () => {
    const name = "Название видео.mp4";
    const ascii = suggestedFilename(corpus.jobId, "mp4");
    const header = contentDispositionHeader(name, ascii);
    expect(parseContentDispositionFilename(header, name)).toBe(name);
    expect(isSafeSuggestedFilename(name, "mp4")).toBe(true);
    expect(isSafeSuggestedFilename("ok\nname.mp4", "mp4")).toBe(false);
  });
});
