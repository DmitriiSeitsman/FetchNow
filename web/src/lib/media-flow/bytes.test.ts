import { describe, expect, it } from "vitest";
import { formatApproxBytes, formatByteSize, formatExactBytes, UNKNOWN_SIZE_LABEL } from "./bytes";

describe("byte size formatting", () => {
  it("uses locale-independent KB/MB/GB boundaries", () => {
    expect(formatByteSize(0)).toBe("0 B");
    expect(formatByteSize(1023)).toBe("1023 B");
    expect(formatByteSize(1024)).toBe("1.0 KB");
    expect(formatByteSize(12_000_000)).toBe("11.4 MB");
    expect(formatByteSize(1024 * 1024)).toBe("1.0 MB");
    expect(formatByteSize(1024 * 1024 * 1024)).toBe("1.0 GB");
    expect(formatApproxBytes(12_000_000)).toBe("≈ 11.4 MB");
    expect(formatExactBytes(12_000_000)).toBe("11.4 MB");
    expect(formatApproxBytes(null)).toBe(UNKNOWN_SIZE_LABEL);
    expect(formatByteSize(12_000_000)).not.toMatch(/,/);
  });
});
