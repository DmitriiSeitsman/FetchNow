import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const nginxConf = readFileSync(
  join(here, "../../../docker/nginx.conf"),
  "utf8",
);

describe("web nginx SEO routing", () => {
  it("canonicalizes provider directories with 308 and never soft-200s unknowns", () => {
    for (const slug of ["vk", "rutube", "ok", "dzen"]) {
      expect(nginxConf).toContain(`location = /${slug} { return 308 /${slug}/; }`);
    }
    expect(nginxConf).toContain("try_files $uri $uri/ =404;");
    expect(nginxConf).toContain("absolute_redirect off;");
    expect(nginxConf).not.toContain("/index.html;");
  });
});
