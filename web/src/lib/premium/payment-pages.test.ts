import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

function page(name: "success" | "fail"): string {
  return readFileSync(
    fileURLToPath(new URL(`../../pages/payment/${name}/index.astro`, import.meta.url)),
    "utf8",
  );
}

describe("payment return pages", () => {
  it("keeps SuccessURL noindex and delegates truth to backend polling", () => {
    const source = page("success");
    expect(source).toContain('robots="noindex,nofollow"');
    expect(source).toContain("mountPaymentSuccess");
    expect(source).toContain("Переадресация сама по себе не активирует Premium");
    expect(source).not.toMatch(/localStorage|Premium\s*=\s*true/);
  });

  it("keeps FailURL non-mutating with safe TEST copy", () => {
    const source = page("fail");
    expect(source).toContain('robots="noindex,nofollow"');
    expect(source).toContain("Тестовая оплата не завершена");
    expect(source).toContain("Premium не был изменён");
    expect(source).not.toMatch(/fetch\(|localStorage|sessionStorage|revoke/i);
  });
});
