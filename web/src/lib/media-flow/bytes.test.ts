import { describe, expect, it } from "vitest";
import { formatApproxBytes, formatByteSize, formatExactBytes, UNKNOWN_SIZE_LABEL } from "./bytes";

describe("byte size formatting", () => {
  it("uses locale-independent KB/MB/GB boundaries", () => {
    expect(formatByteSize(0)).toBe("0 Б");
    expect(formatByteSize(1023)).toBe("1023 Б");
    expect(formatByteSize(1024)).toBe("1.0 КБ");
    expect(formatByteSize(12_000_000)).toBe("11.4 МБ");
    expect(formatByteSize(1024 * 1024)).toBe("1.0 МБ");
    expect(formatByteSize(1024 * 1024 * 1024)).toBe("1.0 ГБ");
    expect(formatApproxBytes(12_000_000)).toBe("≈ 11.4 МБ");
    expect(formatExactBytes(12_000_000)).toBe("11.4 МБ");
    expect(formatApproxBytes(null)).toBe(UNKNOWN_SIZE_LABEL);
    expect(formatByteSize(12_000_000)).not.toMatch(/,/);
  });
});
