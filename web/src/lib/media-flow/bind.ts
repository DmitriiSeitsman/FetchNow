import { isMediaFlowEnabled } from "./flag";
import { MediaFlowController } from "./controller";
import { renderFlow } from "./render";

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
  form?.addEventListener("submit", (event) => {
    event.preventDefault();
    const input = root.querySelector<HTMLInputElement>("[data-flow-url]");
    const url = input?.value.trim() ?? "";
    if (!url) {
      return;
    }
    void controller.submit(url);
  });
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
