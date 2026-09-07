import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const nginx = readFileSync(join(here, "../../../../deploy/nginx/nginx.conf"), "utf8");

describe("payment navigation CSP", () => {
  it("allows only the exact Robokassa form origin without broadening fetches", () => {
    expect(nginx).toContain("form-action 'self' https://auth.robokassa.ru");
    expect(nginx).toContain("connect-src 'self'");
    expect(nginx).not.toContain("form-action *");
    expect(nginx).not.toContain("form-action https:");
    expect(nginx).not.toContain("https://*.robokassa.ru");
  });
});
