/** Page-level SEO and marketing copy for the Russian-only MVP. */

import { isSearchIndexingEnabled } from "./indexing";
import { resolveSiteUrl } from "./site-url";

export type ProviderPage = {
  id: "vk" | "rutube" | "ok" | "dzen";
  path: string;
  title: string;
  description: string;
  h1: string;
  lede: string;
  navLabel: string;
  brand: string;
  /** Page may stay noindex even when global indexing is enabled. */
  indexable: boolean;
};

export type HomePage = {
  path: string;
  title: string;
  description: string;
  h1: string;
  lede: string;
  indexable: boolean;
  providerLinks: ReadonlyArray<{ href: string; label: string }>;
};

/** Legal / policy pages — always noindex; never SEO landings. */
export type LegalPage = {
  id: "privacy" | "terms" | "copyright";
  path: string;
  title: string;
  description: string;
  h1: string;
  /** Always false: legal pages stay out of search even when global indexing is on. */
  indexable: false;
  /** Prefer follow so crawlers may still discover linked public pages. */
  robotsFollow: true;
};

export const homePage: HomePage = Object.freeze({
  path: "/",
  title: "FetchNow — скачать видео по ссылке",
  description:
    "Честный сервис для скачивания видео по ссылке. Без рекламы, без всплывающих окон и без фальшивых кнопок «Скачать».",
  h1: "Скачать видео по ссылке",
  lede: "Вставьте публичную ссылку на видео, выберите нужный вариант и скачайте файл. Без регистрации и лишних действий.",
  indexable: true,
  providerLinks: Object.freeze([
    { href: "/vk/", label: "VK" },
    { href: "/rutube/", label: "RUTUBE" },
    { href: "/ok/", label: "Одноклассники" },
    { href: "/dzen/", label: "Дзен" },
  ]),
});

export const providerPages: ReadonlyArray<ProviderPage> = Object.freeze([
  Object.freeze({
    id: "vk",
    path: "/vk/",
    title: "Скачать видео из ВКонтакте — FetchNow",
    description:
      "Скачайте публичное видео из VK по ссылке. Без рекламы, без аккаунта и без фальшивых кнопок.",
    h1: "Скачать видео из ВКонтакте",
    lede: "Вставьте публичную ссылку VK или VK Video — FetchNow подготовит файл для скачивания.",
    navLabel: "VK",
    brand: "VK",
    indexable: true,
  }),
  Object.freeze({
    id: "rutube",
    path: "/rutube/",
    title: "Скачать видео с Rutube — FetchNow",
    description:
      "Скачайте публичное видео с Rutube по ссылке. Без рекламы, без аккаунта и без лишних шагов.",
    h1: "Скачать видео с Rutube",
    lede: "Вставьте публичную ссылку Rutube — включая Shorts — и получите файл для скачивания.",
    navLabel: "RUTUBE",
    brand: "RUTUBE",
    indexable: true,
  }),
  Object.freeze({
    id: "ok",
    path: "/ok/",
    title: "Скачать видео с OK.ru — FetchNow",
    description:
      "Скачайте публичное видео с Одноклассников по ссылке. Без рекламы и без фальшивых кнопок «Скачать».",
    h1: "Скачать видео с OK.ru",
    lede: "Вставьте публичную ссылку OK.ru — FetchNow подготовит файл, когда доступен единый видео+аудио поток.",
    navLabel: "Одноклассники",
    brand: "OK",
    indexable: true,
  }),
  Object.freeze({
    id: "dzen",
    path: "/dzen/",
    title: "Скачать видео с Дзен — FetchNow",
    description:
      "Скачайте публичное видео или Shorts с Дзен по ссылке. Без рекламы и без аккаунта.",
    h1: "Скачать видео с Дзен",
    lede: "Вставьте публичную ссылку Дзен Video или Shorts — FetchNow подготовит файл для скачивания.",
    navLabel: "Дзен",
    brand: "Дзен",
    indexable: true,
  }),
]);

export const legalPages: ReadonlyArray<LegalPage> = Object.freeze([
  Object.freeze({
    id: "privacy",
    path: "/privacy/",
    title: "Политика конфиденциальности — FetchNow",
    description:
      "Политика конфиденциальности FetchNow: какие технические данные используются сервисом и как устроена агрегированная статистика.",
    h1: "Политика конфиденциальности",
    indexable: false,
    robotsFollow: true,
  }),
  Object.freeze({
    id: "terms",
    path: "/terms/",
    title: "Условия использования — FetchNow",
    description:
      "Условия использования FetchNow: законное использование сервиса, ответственность пользователя, ограничения и порядок обращений.",
    h1: "Условия использования",
    indexable: false,
    robotsFollow: true,
  }),
  Object.freeze({
    id: "copyright",
    path: "/copyright/",
    title: "Правообладателям — FetchNow",
    description:
      "Информация для правообладателей FetchNow. Порядок взаимодействия готовится к публикации.",
    h1: "Правообладателям",
    indexable: false,
    robotsFollow: true,
  }),
]);

export function providerPageById(id: ProviderPage["id"]): ProviderPage {
  const page = providerPages.find((entry) => entry.id === id);
  if (!page) {
    throw new Error(`Unknown provider page: ${id}`);
  }
  return page;
}

export function legalPageById(id: LegalPage["id"]): LegalPage {
  const page = legalPages.find((entry) => entry.id === id);
  if (!page) {
    throw new Error(`Unknown legal page: ${id}`);
  }
  return page;
}

/** Validated site origin without trailing slash. Evaluated at build time. */
export function siteUrl(): string {
  return resolveSiteUrl({
    indexingEnabled: isSearchIndexingEnabled(),
  });
}

export function absoluteUrl(path: string): string {
  const normalized = path.startsWith("/") ? path : `/${path}`;
  const withSlash =
    normalized === "/" ? "/" : normalized.replace(/\/?$/, "/");
  return `${siteUrl()}${withSlash}`;
}

export function websiteJsonLd(): Record<string, unknown> {
  return {
    "@context": "https://schema.org",
    "@type": "WebSite",
    name: "FetchNow",
    url: absoluteUrl("/"),
    inLanguage: "ru-RU",
    description: homePage.description,
  };
}

export function webApplicationJsonLd(): Record<string, unknown> {
  return {
    "@context": "https://schema.org",
    "@type": "WebApplication",
    name: "FetchNow",
    url: absoluteUrl("/"),
    applicationCategory: "MultimediaApplication",
    operatingSystem: "Web",
    inLanguage: "ru-RU",
    description: homePage.description,
  };
}

/** Paths eligible for sitemap when global indexing is enabled. */
export function indexablePaths(): string[] {
  // Legal pages are intentionally omitted: they stay noindex and are not landings.
  const pages: Array<{ path: string; indexable: boolean }> = [
    homePage,
    ...providerPages,
  ];
  return pages.filter((page) => page.indexable).map((page) => page.path);
}

export type RobotsPageOptions = {
  /**
   * When indexable is false, emit `noindex,follow` instead of `noindex,nofollow`.
   * Used for legal pages so linked public URLs remain discoverable.
   */
  follow?: boolean;
};

export function robotsContentForPage(
  indexable: boolean,
  options?: RobotsPageOptions,
): string | undefined {
  // When a page opts out, force noindex even if the global switch is on.
  if (!indexable) {
    return options?.follow ? "noindex,follow" : "noindex,nofollow";
  }
  return undefined;
}

/** Robots override for legal documents (`noindex,follow`). */
export function robotsContentForLegalPage(page: LegalPage): string {
  return robotsContentForPage(page.indexable, { follow: page.robotsFollow })!;
}
