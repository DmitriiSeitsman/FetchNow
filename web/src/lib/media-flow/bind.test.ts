/**
 * @vitest-environment jsdom
 */
import { describe, expect, it, vi } from "vitest";
import { mountMediaFlow } from "./bind";

describe("bind", () => {
  it("does not mount or fetch when the UI flag is off", () => {
    const fetchImpl = vi.fn();
    vi.stubGlobal("fetch", fetchImpl);
    document.body.innerHTML = `<section data-media-flow></section>`;
    expect(mountMediaFlow(document.body, false)).toBeNull();
    expect(fetchImpl).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});
