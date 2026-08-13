import { describe, expect, it } from "vitest";
import { FlowMachine, IllegalTransitionError, canTransition } from "./state-machine";

describe("state machine", () => {
  it("walks the happy path", () => {
    const m = new FlowMachine();
    expect(m.transition("submitting")).toBe(true);
    const gen = m.generationId;
    m.transition("inspecting", gen);
    m.transition("inspected", gen);
    m.transition("enqueueing_download", gen);
    m.transition("downloading", gen);
    m.transition("ready", gen);
    m.transition("saving", gen);
    m.transition("completed", gen);
    expect(m.current).toBe("completed");
  });

  it("allows saving to return to ready after picker cancel", () => {
    expect(canTransition("saving", "ready")).toBe(true);
  });

  it("maps inspection and download failures", () => {
    const a = new FlowMachine();
    a.transition("submitting");
    a.transition("inspecting");
    a.transition("inspection_failed");
    expect(a.current).toBe("inspection_failed");
    const b = new FlowMachine();
    b.transition("submitting");
    b.transition("inspecting");
    b.transition("inspected");
    b.transition("enqueueing_download");
    b.transition("download_failed");
    expect(b.current).toBe("download_failed");
  });

  it("supports expiration and start-over", () => {
    const m = new FlowMachine();
    m.transition("submitting");
    m.transition("expired");
    m.transition("idle");
    m.resetToIdle();
    expect(m.current).toBe("idle");
  });

  it("ignores stale generation transitions", () => {
    const m = new FlowMachine();
    m.transition("submitting");
    const old = m.generationId;
    m.resetToIdle();
    m.transition("submitting");
    expect(m.transition("inspecting", old)).toBe(false);
    expect(m.current).toBe("submitting");
  });

  it("rejects illegal transitions", () => {
    const m = new FlowMachine();
    expect(() => m.transition("ready")).toThrow(IllegalTransitionError);
    expect(canTransition("idle", "completed")).toBe(false);
  });

  it("suppresses duplicate actions while busy", () => {
    const m = new FlowMachine();
    expect(m.beginAction()).toBe(true);
    expect(m.beginAction()).toBe(false);
    m.endAction();
    expect(m.beginAction()).toBe(true);
  });
});
