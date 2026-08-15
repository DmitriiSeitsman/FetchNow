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
    const other = document.createElement("button");
    document.body.append(other);
    other.focus();

    await expect(
      pasteClipboardIntoInput(input, {
        readText: vi.fn().mockResolvedValue("  https://example.test/video  "),
      }),
    ).resolves.toBe(true);

    expect(input.value).toBe("https://example.test/video");
    expect(onInput).toHaveBeenCalledOnce();
    expect(document.activeElement).toBe(input);
  });

  it("does not focus the input before clipboard read settles", async () => {
    const input = document.createElement("input");
    document.body.replaceChildren(input);
    const other = document.createElement("button");
    document.body.append(other);
    other.focus();

    let release!: (value: string) => void;
    const pending = new Promise<string>((resolve) => {
      release = resolve;
    });
    const done = pasteClipboardIntoInput(input, undefined, pending);
    expect(document.activeElement).toBe(other);
    release("https://example.test/deferred");
    await expect(done).resolves.toBe(true);
    expect(input.value).toBe("https://example.test/deferred");
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
          <p data-flow-paste-hint hidden></p>
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
    expect(
      document.querySelector<HTMLElement>("[data-flow-paste-hint]")?.hidden,
    ).toBe(true);
    controller?.disconnect();
    vi.unstubAllGlobals();
  });

  it("does not cancel the paste click so native paste UI can proceed", () => {
    const readText = vi.fn().mockReturnValue(new Promise<string>(() => {}));
    vi.stubGlobal("navigator", { ...navigator, clipboard: { readText } });
    document.body.innerHTML = `
      <section data-media-flow>
        <form data-flow-form>
          <input data-flow-url type="url" value="" />
          <button type="button" data-flow-paste>Paste</button>
        </form>
      </section>
    `;
    const controller = mountMediaFlow(document.body, true);
    const paste = document.querySelector<HTMLButtonElement>("[data-flow-paste]");
    const event = new MouseEvent("click", { bubbles: true, cancelable: true });
    paste?.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
    expect(readText).toHaveBeenCalledOnce();
    controller?.disconnect();
    vi.unstubAllGlobals();
  });

  it("offers a manual paste hint when the clipboard is unreadable", async () => {
    const readText = vi.fn().mockRejectedValue(new DOMException("denied"));
    vi.stubGlobal("navigator", { ...navigator, clipboard: { readText } });
    document.body.innerHTML = `
      <section data-media-flow>
        <form data-flow-form>
          <input data-flow-url type="url" value="" />
          <button type="button" data-flow-paste>Paste</button>
          <p data-flow-paste-hint hidden></p>
        </form>
      </section>
    `;
    const controller = mountMediaFlow(document.body, true);
    const input = document.querySelector<HTMLInputElement>("[data-flow-url]");
    const hint = document.querySelector<HTMLElement>("[data-flow-paste-hint]");
    document.querySelector<HTMLButtonElement>("[data-flow-paste]")?.click();

    await vi.waitFor(() => {
      expect(hint?.hidden).toBe(false);
    });
    expect(hint?.textContent).toMatch(/V to paste/);
    expect(document.activeElement).toBe(input);

    input?.dispatchEvent(new Event("paste", { bubbles: true }));
    expect(hint?.hidden).toBe(true);
    controller?.disconnect();
    vi.unstubAllGlobals();
  });

  it("hints immediately when the Clipboard API is missing", () => {
    vi.stubGlobal("navigator", { ...navigator, clipboard: undefined });
    document.body.innerHTML = `
      <section data-media-flow>
        <form data-flow-form>
          <input data-flow-url type="url" value="" />
          <button type="button" data-flow-paste>Paste</button>
          <p data-flow-paste-hint hidden></p>
        </form>
      </section>
    `;
    const controller = mountMediaFlow(document.body, true);
    const hint = document.querySelector<HTMLElement>("[data-flow-paste-hint]");
    document.querySelector<HTMLButtonElement>("[data-flow-paste]")?.click();
    expect(hint?.hidden).toBe(false);
    expect(document.activeElement).toBe(
      document.querySelector("[data-flow-url]"),
    );
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
