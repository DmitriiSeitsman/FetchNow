import { describe, expect, it } from "vitest";
import { parseMediaFlowFlag } from "./flag";

describe("feature flag", () => {
  it("enables only the exact true value", () => {
    expect(parseMediaFlowFlag("true")).toBe(true);
    expect(parseMediaFlowFlag(true)).toBe(true);
    expect(parseMediaFlowFlag("false")).toBe(false);
    expect(parseMediaFlowFlag(undefined)).toBe(false);
    expect(parseMediaFlowFlag("TRUE")).toBe(false);
    expect(parseMediaFlowFlag("1")).toBe(false);
    expect(parseMediaFlowFlag("yes")).toBe(false);
  });
});
