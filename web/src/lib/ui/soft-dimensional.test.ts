/**
 * @vitest-environment node
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const WEB_ROOT = resolve(__dirname, "../../..");
const indexSource = readFileSync(resolve(WEB_ROOT, "src/pages/index.astro"), "utf-8");
const mediaFlowSource = readFileSync(
  resolve(WEB_ROOT, "src/components/MediaFlow.astro"),
  "utf-8",
);
const baseLayoutSource = readFileSync(
  resolve(WEB_ROOT, "src/layouts/BaseLayout.astro"),
  "utf-8",
);
const cssSource = readFileSync(resolve(WEB_ROOT, "src/styles/global.css"), "utf-8");
const legalSource = readFileSync(
  resolve(WEB_ROOT, "src/components/LegalDocument.astro"),
  "utf-8",
);

describe("soft dimensional UI", () => {
  it("A. homepage brand lockup references the merchant mark asset", () => {
    expect(indexSource).toContain('src="/robokassa-merchant.png"');
    expect(indexSource).toContain('class="brand-lockup"');
  });

  it("B. brand mark declares intrinsic width and height", () => {
    expect(indexSource).toMatch(/width="52"/);
    expect(indexSource).toMatch(/height="52"/);
  });

  it("C. brand mark is decorative without duplicating the accessible name", () => {
    expect(indexSource).toContain('alt=""');
    expect(indexSource).toContain('aria-hidden="true"');
    expect(indexSource).toContain('<p class="brand">FetchNow</p>');
  });

  it("D. media-flow data hooks are unchanged", () => {
    for (const hook of [
      "data-flow-form",
      "data-flow-url",
      "data-flow-submit",
      "data-flow-ready-free",
      "data-flow-native-download",
    ]) {
      expect(mediaFlowSource).toContain(hook);
    }
  });

  it("E. free ready-card markup remains present", () => {
    expect(mediaFlowSource).toContain("ready-card--free");
    expect(mediaFlowSource).toContain("data-flow-ready-free");
  });

  it("F. no premium card or public payment CTA was introduced", () => {
    expect(indexSource).not.toContain("ready-card--premium");
    expect(indexSource).not.toContain("data-payment");
    expect(mediaFlowSource).not.toContain("ready-card--premium");
    expect(mediaFlowSource).not.toContain("data-payment");
    expect(mediaFlowSource).not.toContain("robokassa");
  });

  it("G. reduced-motion cancels brand lockup entrance animation", () => {
    const start = cssSource.indexOf("@media (prefers-reduced-motion: reduce)");
    expect(start).toBeGreaterThan(-1);
    const reduced = cssSource.slice(start);
    expect(reduced).toContain(".brand-lockup");
    expect(reduced).toMatch(/animation:\s*none/u);
  });

  it("H. SEO metadata and og image contract stay unchanged", () => {
    expect(baseLayoutSource).toContain('href="/favicon.svg"');
    expect(baseLayoutSource).toContain("/og.png");
    expect(baseLayoutSource).not.toContain("robokassa-merchant");
  });

  it("I. legal pages do not depend on the payment merchant asset", () => {
    expect(legalSource).not.toContain("robokassa-merchant");
  });

  it("defines shared soft depth tokens on :root", () => {
    for (const token of [
      "--elev-surface:",
      "--elev-raised:",
      "--elev-primary:",
      "--elev-inset-highlight:",
      "--elev-inset-pressed:",
      "--glow-green-soft:",
    ]) {
      expect(cssSource).toContain(token);
    }
  });
});
