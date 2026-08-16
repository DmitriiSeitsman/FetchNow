import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  homePage,
  indexablePaths,
  legalPageById,
  legalPages,
  providerPages,
  providerPageById,
  robotsContentForLegalPage,
  robotsContentForPage,
  webApplicationJsonLd,
  websiteJsonLd,
} from "./pages";
import { robotsMetaContent } from "./indexing";

const here = dirname(fileURLToPath(import.meta.url));

describe("seo pages", () => {
  it("covers only actually supported providers", () => {
    expect(providerPages.map((page) => page.id)).toEqual([
      "vk",
      "rutube",
      "ok",
      "dzen",
    ]);
    expect(homePage.providerLinks.map((link) => link.href)).toEqual([
      "/vk/",
      "/rutube/",
      "/ok/",
      "/dzen/",
    ]);
  });

  it("keeps trailing-slash paths for directory builds", () => {
    for (const page of providerPages) {
      expect(page.path.endsWith("/")).toBe(true);
      expect(providerPageById(page.id).title).toContain("FetchNow");
    }
  });

  it("keeps unique title/description/h1/canonical per landing", () => {
    const titles = new Set(providerPages.map((page) => page.title));
    const descriptions = new Set(providerPages.map((page) => page.description));
    const h1s = new Set(providerPages.map((page) => page.h1));
    const paths = new Set(providerPages.map((page) => page.path));
    expect(titles.size).toBe(providerPages.length);
    expect(descriptions.size).toBe(providerPages.length);
    expect(h1s.size).toBe(providerPages.length);
    expect(paths.size).toBe(providerPages.length);
  });

  it("builds sitemap paths only from indexable pages", () => {
    expect(indexablePaths()).toEqual([
      "/",
      "/vk/",
      "/rutube/",
      "/ok/",
      "/dzen/",
    ]);
    const withNoindex = [
      { path: "/", indexable: true },
      { path: "/draft/", indexable: false },
      { path: "/vk/", indexable: true },
    ];
    expect(
      withNoindex.filter((page) => page.indexable).map((page) => page.path),
    ).toEqual(["/", "/vk/"]);
  });

  it("forces page-level noindex even when global indexing is on", () => {
    expect(robotsContentForPage(false)).toBe("noindex,nofollow");
    expect(robotsContentForPage(true)).toBeUndefined();
    const globalOn = robotsMetaContent(true);
    expect(globalOn).toBe("index,follow");
    const pageOverride = robotsContentForPage(false) ?? globalOn;
    expect(pageOverride).toBe("noindex,nofollow");
  });

  it("keeps legal pages noindex,follow and out of the sitemap", () => {
    expect(legalPages.map((page) => page.path)).toEqual([
      "/privacy/",
      "/terms/",
      "/copyright/",
    ]);
    for (const page of legalPages) {
      expect(page.indexable).toBe(false);
      expect(robotsContentForLegalPage(page)).toBe("noindex,follow");
      expect(indexablePaths()).not.toContain(page.path);
    }
    expect(legalPageById("privacy").title).toBe(
      "Политика конфиденциальности — FetchNow",
    );
  });

  it("emits factual JSON-LD without offers", () => {
    const website = websiteJsonLd();
    const app = webApplicationJsonLd();
    expect(website["@type"]).toBe("WebSite");
    expect(app["@type"]).toBe("WebApplication");
    expect(app).not.toHaveProperty("offers");
    expect(app).toMatchObject({
      name: "FetchNow",
      applicationCategory: "MultimediaApplication",
      operatingSystem: "Web",
      inLanguage: "ru-RU",
    });
  });
});

describe("provider landing wiring", () => {
  it("uses shared MediaFlow on every provider landing page", () => {
    const landing = readFileSync(
      join(here, "../../components/ProviderLanding.astro"),
      "utf8",
    );
    expect(landing).toContain('import MediaFlow from "./MediaFlow.astro"');
    expect(landing).toContain("<MediaFlow />");
    for (const id of ["vk", "rutube", "ok", "dzen"] as const) {
      const page = readFileSync(
        join(here, `../../pages/${id}/index.astro`),
        "utf8",
      );
      expect(page).toContain("ProviderLanding");
      expect(page).toContain(`providerPageById("${id}")`);
    }
  });

  it("does not mention Yandex Preview as a user-facing platform", () => {
    const mediaFlow = readFileSync(
      join(here, "../../components/MediaFlow.astro"),
      "utf8",
    );
    const providerSupport = readFileSync(
      join(here, "../../components/ProviderSupport.astro"),
      "utf8",
    );
    expect(mediaFlow).toContain("ProviderSupport");
    expect(providerSupport).toContain("Поддерживаем публичные видео:");
    expect(providerSupport).toContain("Регистрация не нужна.");
    expect(providerSupport).toContain("homePage.providerLinks");
    expect(mediaFlow).not.toMatch(/Yandex Preview/i);
    expect(providerSupport).not.toMatch(/Yandex Preview/i);
  });

  it("keeps provider navigation once under the downloader, not under the trust line", () => {
    const index = readFileSync(join(here, "../../pages/index.astro"), "utf8");
    expect(index).toContain("ProviderSupport");
    expect(index).toContain("trust-premium");
    expect(index).toContain("Достаточно хорош, чтобы полюбить.");
    expect(index).toContain("Достаточно полезен, чтобы выбрать Premium.");
    // Provider row must not sit between trust-line and slogan.
    const trustBlock = index.slice(
      index.indexOf('class="trust-premium"'),
      index.indexOf('class="trust-points"'),
    );
    expect(trustBlock).not.toContain("platform-links");
    expect(trustBlock).not.toContain("providerLinks");
  });

  it("ships legal routes, shared footer, and privacy policy copy", () => {
    const layout = readFileSync(
      join(here, "../../layouts/BaseLayout.astro"),
      "utf8",
    );
    const footer = readFileSync(
      join(here, "../../components/SiteFooter.astro"),
      "utf8",
    );
    const privacy = readFileSync(
      join(here, "../../pages/privacy/index.astro"),
      "utf8",
    );
    const terms = readFileSync(
      join(here, "../../pages/terms/index.astro"),
      "utf8",
    );
    const copyright = readFileSync(
      join(here, "../../pages/copyright/index.astro"),
      "utf8",
    );
    expect(layout).toContain("SiteFooter");
    expect(footer).toContain("/privacy/");
    expect(footer).toContain("/terms/");
    expect(footer).toContain("/copyright/");
    expect(footer).toContain("mailto:support@fetchnow.online");
    expect(footer).toContain("mailto:copyright@fetchnow.online");
    expect(privacy).toContain("Редакция от 16 августа 2026 года");
    expect(privacy).toContain("Локальная статистика использования");
    expect(terms).toContain("Раздел готовится");
    expect(copyright).toContain("copyright@fetchnow.online");
    expect(copyright).not.toMatch(/DMCA|информационн(ый|ого) посредник/i);
  });
});
