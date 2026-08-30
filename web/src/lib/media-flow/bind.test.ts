/**
 * @vitest-environment jsdom
 */
import { describe, expect, it, vi } from "vitest";
import { mountMediaFlow, pasteClipboardIntoInput } from "./bind";
import type { MediaFlowController } from "./controller";

const SAMPLE_QUOTA = {
  tier: "free" as const,
  downloadLimit: 3,
  downloadsUsed: 1,
  downloadsReserved: 0,
  downloadsRemaining: 2,
  resetAt: null,
};

async function drainQuotaAndDisconnect(controller: MediaFlowController | null) {
  controller?.disconnect();
  await controller?.initializeQuota();
}

function quotaJsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

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
    await drainQuotaAndDisconnect(controller);
    vi.unstubAllGlobals();
  });

  it("does not cancel the paste click so native paste UI can proceed", async () => {
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
    await drainQuotaAndDisconnect(controller);
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
    expect(hint?.textContent).toMatch(/Не удалось вставить автоматически/);
    expect(hint?.textContent).toMatch(/⌘V|Ctrl\+V/);
    expect(document.activeElement).toBe(input);

    input?.dispatchEvent(new Event("paste", { bubbles: true }));
    expect(hint?.hidden).toBe(true);
    await drainQuotaAndDisconnect(controller);
    vi.unstubAllGlobals();
  });

  it("hints immediately when the Clipboard API is missing", async () => {
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
    await drainQuotaAndDisconnect(controller);
    vi.unstubAllGlobals();
  });

  it("keeps paste hint hidden until the paste button is used", async () => {
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
    expect(hint?.hidden).toBe(true);
    expect(hint?.textContent).toBe("");
    await drainQuotaAndDisconnect(controller);
  });

  it("clears the url field when Start over is clicked", async () => {
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
    await drainQuotaAndDisconnect(controller);
  });

  it("renders quota while the flow remains mounted", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => quotaJsonResponse(SAMPLE_QUOTA)),
    );
    document.body.innerHTML = `
      <section data-media-flow>
        <form data-flow-form>
          <input data-flow-url type="url" value="" />
          <button type="submit" data-flow-submit>Fetch</button>
        </form>
        <p data-flow-status></p>
        <p data-flow-quota hidden></p>
        <p data-flow-quota-reset hidden></p>
      </section>
    `;
    const controller = mountMediaFlow(document.body, true);
    await controller?.initializeQuota();
    const quota = document.querySelector<HTMLElement>("[data-flow-quota]");
    expect(quota?.hidden).toBe(false);
    expect(quota?.textContent).toContain("2 из 3");
    await drainQuotaAndDisconnect(controller);
    vi.unstubAllGlobals();
  });

  it("does not render quota after disconnect even if the fetch later settles", async () => {
    let release!: (value: Response) => void;
    const pending = new Promise<Response>((resolve) => {
      release = resolve;
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(() => pending),
    );
    document.body.innerHTML = `
      <section data-media-flow>
        <form data-flow-form>
          <input data-flow-url type="url" value="" />
          <button type="submit" data-flow-submit>Fetch</button>
        </form>
        <p data-flow-status></p>
        <p data-flow-quota hidden></p>
      </section>
    `;
    const controller = mountMediaFlow(document.body, true);
    const quota = document.querySelector<HTMLElement>("[data-flow-quota]");
    expect(quota?.hidden).toBe(true);
    expect(quota?.textContent).toBe("");
    controller?.disconnect();
    release(quotaJsonResponse(SAMPLE_QUOTA));
    await controller?.initializeQuota();
    expect(quota?.hidden).toBe(true);
    expect(quota?.textContent).toBe("");
    expect(controller?.snapshot().freeQuota).toBeNull();
    vi.unstubAllGlobals();
  });
});
