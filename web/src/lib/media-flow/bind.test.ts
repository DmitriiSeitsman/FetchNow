/**
 * @vitest-environment jsdom
 */
import { describe, expect, it, vi } from "vitest";
import { mountMediaFlow, pasteClipboardIntoInput } from "./bind";

describe("bind", () => {
  it("does not mount or fetch when the UI flag is off", () => {
    const fetchImpl = vi.fn();
    vi.stubGlobal("fetch", fetchImpl);
    document.body.innerHTML = `<section data-media-flow></section>`;
    expect(mountMediaFlow(document.body, false)).toBeNull();
    expect(fetchImpl).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("pastes trimmed clipboard text and emits an input event", async () => {
    const input = document.createElement("input");
    document.body.replaceChildren(input);
    const onInput = vi.fn();
    input.addEventListener("input", onInput);

    await expect(
      pasteClipboardIntoInput(input, {
        readText: vi.fn().mockResolvedValue("  https://example.test/video  "),
      }),
    ).resolves.toBe(true);

    expect(input.value).toBe("https://example.test/video");
    expect(onInput).toHaveBeenCalledOnce();
    expect(document.activeElement).toBe(input);
  });

  it("fails soft when clipboard access is unavailable or denied", async () => {
    const input = document.createElement("input");
    document.body.replaceChildren(input);
    input.value = "keep me";

    await expect(pasteClipboardIntoInput(input, undefined)).resolves.toBe(false);
    await expect(
      pasteClipboardIntoInput(input, {
        readText: vi.fn().mockRejectedValue(new DOMException("denied")),
      }),
    ).resolves.toBe(false);

    expect(input.value).toBe("keep me");
    expect(document.activeElement).toBe(input);
  });

  it("uses a pre-started clipboard read without calling readText again", async () => {
    const input = document.createElement("input");
    document.body.replaceChildren(input);
    const readText = vi.fn().mockResolvedValue("https://from-pending.test");
    const pending = Promise.resolve("  https://prestarted.test/v  ");

    await expect(
      pasteClipboardIntoInput(input, { readText }, pending),
    ).resolves.toBe(true);

    expect(readText).not.toHaveBeenCalled();
    expect(input.value).toBe("https://prestarted.test/v");
  });

  it("pastes from clipboard when the paste button is clicked", async () => {
    const readText = vi.fn().mockResolvedValue("https://click-paste.test/video");
    vi.stubGlobal("navigator", {
      ...navigator,
      clipboard: { readText },
    });
    document.body.innerHTML = `
      <section data-media-flow>
        <form data-flow-form>
          <input data-flow-url type="url" value="" />
          <button type="button" data-flow-paste>Paste</button>
          <button type="submit" data-flow-submit>Fetch</button>
        </form>
      </section>
    `;
    const controller = mountMediaFlow(document.body, true);
    const input = document.querySelector<HTMLInputElement>("[data-flow-url]");
    const paste = document.querySelector<HTMLButtonElement>("[data-flow-paste]");
    paste?.click();
    await vi.waitFor(() => {
      expect(input?.value).toBe("https://click-paste.test/video");
    });
    expect(readText).toHaveBeenCalledOnce();
    controller?.disconnect();
    vi.unstubAllGlobals();
  });

  it("clears the url field when Start over is clicked", () => {
    document.body.innerHTML = `
      <section data-media-flow>
        <form data-flow-form>
          <input data-flow-url type="url" value="https://example.test/video" />
          <button type="submit" data-flow-submit>Fetch</button>
          <button type="button" data-flow-reset>Start over</button>
        </form>
      </section>
    `;
    const controller = mountMediaFlow(document.body, true);
    expect(controller).not.toBeNull();
    const input = document.querySelector<HTMLInputElement>("[data-flow-url]");
    expect(input?.value).toBe("https://example.test/video");
    document.querySelector<HTMLButtonElement>("[data-flow-reset]")?.click();
    expect(input?.value).toBe("");
    controller?.disconnect();
  });
});
