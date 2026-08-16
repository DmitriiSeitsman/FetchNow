import { isMediaFlowEnabled } from "./flag";
import { MediaFlowController } from "./controller";
import { renderFlow } from "./render";

type ClipboardReader = Pick<Clipboard, "readText">;

export function manualPasteHint(
  userAgent: string = globalThis.navigator?.userAgent ?? "",
): string {
  const apple = /Mac|iPhone|iPad|iPod/.test(userAgent);
  return apple
    ? "Не удалось вставить автоматически. Нажмите ⌘V."
    : "Не удалось вставить автоматически. Нажмите Ctrl+V.";
}

export async function pasteClipboardIntoInput(
  input: HTMLInputElement,
  clipboard: ClipboardReader | undefined = globalThis.navigator?.clipboard,
  /** Optional pre-started read so callers can kick off readText in the gesture turn. */
  pendingRead?: Promise<string>,
): Promise<boolean> {
  try {
    const raw =
      pendingRead ??
      (clipboard && typeof clipboard.readText === "function"
        ? clipboard.readText()
        : null);
    if (!raw) {
      return false;
    }
    const value = (await raw).trim();
    if (!value) {
      return false;
    }
    input.value = value;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.focus();
    return true;
  } catch {
    return false;
  }
}

export function mountMediaFlow(
  root: ParentNode,
  enabled = isMediaFlowEnabled(),
): MediaFlowController | null {
  if (!enabled) {
    return null;
  }
  const controller = new MediaFlowController({
    onChange: (snapshot) => renderFlow(root, snapshot),
  });
  const form = root.querySelector<HTMLFormElement>("[data-flow-form]");
  const input = root.querySelector<HTMLInputElement>("[data-flow-url]");
  form?.addEventListener("submit", (event) => {
    event.preventDefault();
    const url = input?.value.trim() ?? "";
    if (!url) {
      return;
    }
    void controller.submit(url);
  });
  const paste = root.querySelector<HTMLButtonElement>("[data-flow-paste]");
  const pasteHint = root.querySelector<HTMLElement>("[data-flow-paste-hint]");
  const showPasteHint = (visible: boolean) => {
    if (!pasteHint) {
      return;
    }
    pasteHint.textContent = visible ? manualPasteHint() : "";
    pasteHint.hidden = !visible;
  };
  let pasteInFlight = false;
  paste?.addEventListener("click", () => {
    if (!input || paste.disabled || pasteInFlight) {
      return;
    }
    showPasteHint(false);
    const clipboard = globalThis.navigator?.clipboard;
    if (!clipboard || typeof clipboard.readText !== "function") {
      // Insecure origin or unsupported browser: leave the caret ready for ⌘V.
      input.focus();
      input.select();
      showPasteHint(true);
      return;
    }
    // readText must start in the same task as the click to keep user activation.
    // The click event is deliberately not cancelled: Safari anchors its native
    // paste confirmation to this gesture.
    const pendingRead = clipboard.readText();
    pasteInFlight = true;
    void pasteClipboardIntoInput(input, clipboard, pendingRead)
      .then((pasted) => {
        if (!pasted) {
          input.focus();
          input.select();
          showPasteHint(true);
        }
      })
      .finally(() => {
        pasteInFlight = false;
      });
  });
  input?.addEventListener("paste", () => showPasteHint(false));
  root.querySelector("[data-flow-formats]")?.addEventListener("change", (event) => {
    const target = event.target;
    if (target instanceof HTMLInputElement && target.type === "radio") {
      controller.selectFormat(target.value);
    }
  });
  root.querySelector("[data-flow-download]")?.addEventListener("click", () => {
    void controller.enqueueDownload();
  });

  const onNativeClick = (event: Event) => {
    const allow = controller.onNativeDownloadClick();
    if (!allow) {
      event.preventDefault();
    }
  };
  root
    .querySelector("[data-flow-native-download]")
    ?.addEventListener("click", onNativeClick);

  root.querySelector("[data-flow-grant-retry]")?.addEventListener("click", () => {
    controller.retryGrantAccess();
  });
  root.querySelector("[data-flow-save-as]")?.addEventListener("click", () => {
    void controller.saveFile();
  });
  root.querySelector("[data-flow-reset]")?.addEventListener("click", () => {
    const urlInput = root.querySelector<HTMLInputElement>("[data-flow-url]");
    if (urlInput) {
      urlInput.value = "";
      urlInput.dispatchEvent(new Event("input", { bubbles: true }));
    }
    controller.startOver();
  });
  root.querySelector("[data-flow-cancel]")?.addEventListener("click", () => {
    void controller.cancelTask();
  });

  const onPageHide = () => {
    controller.onPageHide();
  };
  const onPageShow = (event: PageTransitionEvent) => {
    void controller.onPageShow(event.persisted);
  };
  const onVisibility = () => {
    if (globalThis.document?.visibilityState === "visible") {
      controller.onForegroundResume();
    }
  };
  const onFocus = () => {
    controller.onForegroundResume();
  };

  window.addEventListener("pagehide", onPageHide);
  window.addEventListener("pageshow", onPageShow);
  document.addEventListener("visibilitychange", onVisibility);
  window.addEventListener("focus", onFocus);

  const originalDisconnect = controller.disconnect.bind(controller);
  controller.disconnect = () => {
    window.removeEventListener("pagehide", onPageHide);
    window.removeEventListener("pageshow", onPageShow);
    document.removeEventListener("visibilitychange", onVisibility);
    window.removeEventListener("focus", onFocus);
    originalDisconnect();
  };

  void controller.restore();
  renderFlow(root, controller.snapshot());
  return controller;
}
