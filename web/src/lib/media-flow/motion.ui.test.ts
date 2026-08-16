/**
 * @vitest-environment jsdom
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const cssSource = readFileSync(join(here, "../../styles/global.css"), "utf8");

/**
 * Declarations of the first rule whose selector list matches exactly. The
 * boundary also accepts a preceding comment so documented rules still match.
 */
function ruleBody(selector: string): string {
  const escaped = selector.replaceAll(/[.*+?^${}()|[\]\\]/gu, "\\$&");
  const match = new RegExp(`(?:^|\\}|\\*/)\\s*${escaped}\\s*\\{([^}]*)\\}`, "u").exec(
    cssSource,
  );
  if (!match) {
    throw new Error(`missing rule for ${selector}`);
  }
  return match[1];
}

function keyframeBody(name: string): string {
  const match = new RegExp(`@keyframes\\s+${name}\\s*\\{([\\s\\S]*?)\\n\\}`, "u").exec(
    cssSource,
  );
  if (!match) {
    throw new Error(`missing keyframes ${name}`);
  }
  return match[1];
}

function reducedMotionBlock(): string {
  const start = cssSource.indexOf("@media (prefers-reduced-motion: reduce)");
  expect(start).toBeGreaterThan(-1);
  return cssSource.slice(start);
}

function remValue(declaration: string): number {
  const match = /([\d.]+)rem/u.exec(declaration);
  if (!match) {
    throw new Error(`no rem length in ${declaration}`);
  }
  return Number.parseFloat(match[1]);
}

describe("UI motion polish", () => {
  it("defines the shared motion primitives once on the root", () => {
    const root = ruleBody(":root");
    for (const token of [
      "--motion-fast:",
      "--motion-base:",
      "--motion-entrance:",
      "--ease-out:",
      "--lift:",
    ]) {
      expect(root).toContain(token);
    }
  });

  it("separates the cancel button from the progress divider on both layouts", () => {
    const desktop = ruleBody(".progress-actions");
    expect(desktop).toContain("border-top:");
    expect(remValue(/padding-top:[^;]+;/u.exec(desktop)?.[0] ?? "")).toBeGreaterThanOrEqual(
      0.6,
    );

    const mobileStart = cssSource.indexOf("@media (max-width: 640px)");
    expect(mobileStart).toBeGreaterThan(-1);
    const mobile = /\.progress-actions\s*\{([^}]*)\}/u.exec(cssSource.slice(mobileStart));
    expect(mobile).not.toBeNull();
    expect(remValue(/padding-top:[^;]+;/u.exec(mobile?.[1] ?? "")?.[0] ?? "")).toBeGreaterThanOrEqual(
      0.6,
    );
  });

  it("drives every button state from one hover/focus pattern", () => {
    const base = ruleBody(".btn");
    expect(base).toContain("transition:");
    expect(base).toContain("var(--motion-base)");
    expect(base).toContain("var(--ease-out)");

    // A single shared lift, not a bespoke transform per button.
    expect(cssSource.match(/translateY\(var\(--lift\)\)/gu)).toHaveLength(1);

    for (const selector of [
      '.btn:not(:disabled):not([aria-disabled="true"]):hover',
      '.btn:not(:disabled):not([aria-disabled="true"]):active',
      '.btn-primary:not(:disabled):not([aria-disabled="true"]):hover',
      '.btn-ghost:not(:disabled):not([aria-disabled="true"]):hover',
      '.btn:not(:disabled):not([aria-disabled="true"]):focus-visible',
    ]) {
      expect(() => ruleBody(selector)).not.toThrow();
    }
    expect(ruleBody('.btn-primary:not(:disabled):not([aria-disabled="true"]):hover')).toContain(
      "var(--ring-primary)",
    );
    expect(ruleBody('.btn-ghost:not(:disabled):not([aria-disabled="true"]):hover')).toContain(
      "var(--ring-quiet)",
    );
    expect(cssSource).toContain(".btn:focus-visible");
    expect(cssSource).toContain(".btn-paste:focus-visible:enabled");
  });

  it("keeps disabled buttons visually inert", () => {
    const disabled = ruleBody('.btn:disabled,\n.btn[aria-disabled="true"]');
    expect(disabled).toContain("cursor: not-allowed");
    expect(disabled).toContain("transform: none");
    expect(disabled).toContain("box-shadow: none");
    expect(disabled).not.toMatch(/opacity:\s*0\.\d+/u);
    // Hover/active lift is guarded, so it can never reach a disabled control.
    expect(cssSource).not.toMatch(/^\.btn:hover\s*\{/mu);
  });

  it("animates entrances only with opacity and transform", () => {
    for (const name of ["fade-rise", "fade-in", "progress-sheen"]) {
      const body = keyframeBody(name);
      const properties = [...body.matchAll(/^\s*([a-z-]+):/gmu)].map((match) => match[1]);
      expect(properties.length).toBeGreaterThan(0);
      for (const property of properties) {
        expect(["opacity", "transform"]).toContain(property);
      }
    }
    expect(cssSource).toMatch(/\.meta-card:not\(\[hidden\]\)[\s\S]{0,160}animation: fade-rise/u);
    expect(cssSource).toMatch(/\.progress-actions:not\(\[hidden\]\)\s*\{[^}]*fade-in/u);
  });

  it("keeps the progress sheen out of idle and finished states", () => {
    expect(cssSource).toContain('.progress-card[data-tone="active"] .progress-fill::after');
    expect(ruleBody('.progress-card[data-tone="done"] .progress-fill::after')).toContain(
      "content: none",
    );
    expect(ruleBody(".progress-fill")).toContain("transition:");
  });

  it("drops motion but keeps state feedback under reduced motion", () => {
    const reduced = reducedMotionBlock();
    expect(reduced).toMatch(/--motion-base:\s*1ms/u);
    expect(reduced).toMatch(/--motion-entrance:\s*1ms/u);
    for (const selector of [
      ".spinner",
      ".meta-card:not([hidden])",
      ".quality-card:not([hidden])",
      ".progress-card:not([hidden])",
      ".progress-actions:not([hidden])",
      ".flow-actions .btn:not([hidden])",
    ]) {
      expect(reduced).toContain(selector);
    }
    expect(reduced).toMatch(/animation:\s*none/u);
    expect(reduced).toMatch(/transform:\s*none/u);
    // The paste button keeps its centering translate even without motion.
    expect(reduced).toMatch(/\.btn-paste:hover:enabled[\s\S]{0,120}translateY\(-50%\)/u);
    expect(reduced).not.toMatch(/box-shadow:\s*none/u);
  });

  it("applies the polished spacing through the real stylesheet", () => {
    document.head.innerHTML = "";
    const style = document.createElement("style");
    style.textContent = cssSource;
    document.head.append(style);
    document.body.innerHTML = `
      <section class="progress-card" data-tone="active">
        <div class="progress-track"><span class="progress-fill"></span></div>
        <div class="progress-actions"><button class="btn btn-ghost">Cancel task</button></div>
      </section>
    `;
    const actions = document.querySelector<HTMLElement>(".progress-actions");
    expect(actions).not.toBeNull();
    // jsdom does not resolve var() colors, so only the spacing is asserted here.
    const computed = getComputedStyle(actions as HTMLElement);
    expect(computed.paddingTop).toMatch(/rem$/u);
    expect(Number.parseFloat(computed.paddingTop)).toBeGreaterThanOrEqual(0.6);
    expect(getComputedStyle(actions as HTMLElement).display).toBe("flex");
  });
});
