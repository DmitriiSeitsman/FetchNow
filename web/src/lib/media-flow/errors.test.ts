import { describe, expect, it } from "vitest";
import { generateAccessToken } from "./credentials";
import { flowErrorFromCode, userMessageForCode } from "./errors";

describe("error copy", () => {
  it("never interpolates the access token into user-facing text", () => {
    const token = generateAccessToken();
    const unknown = flowErrorFromCode("not-a-real-code");
    expect(unknown.message).not.toContain(token);
    expect(unknown.userMessage).not.toContain(token);
    expect(userMessageForCode("INVALID_ACCESS_TOKEN").text).not.toContain(token);
    expect(userMessageForCode("INVALID_ACCESS_TOKEN").text).not.toMatch(/Bearer/i);
  });
});
