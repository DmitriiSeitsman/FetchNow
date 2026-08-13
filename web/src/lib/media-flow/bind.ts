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
  root.querySelector("[data-flow-save]")?.addEventListener("click", () => {
    void controller.saveFile();
  });
  root.querySelector("[data-flow-reset]")?.addEventListener("click", () => {
    controller.startOver();
  });
  window.addEventListener("pagehide", () => {
    controller.onPageHide();
  });
  window.addEventListener("pageshow", (event) => {
    controller.onPageShow(event.persisted);
  });
  controller.restore();
  renderFlow(root, controller.snapshot());
  return controller;
}
