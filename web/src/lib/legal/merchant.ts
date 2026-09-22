/** Public merchant identity for footer and readiness pages (owner-approved). */

export const merchant = Object.freeze({
  statusLabel: "Самозанятый",
  fullName: "Сейцман Дмитрий Александрович",
  inn: "471701994485",
  /** Locality only — not a complete postal correspondence address. */
  location: "188410, Ленинградская область, г. Волосово",
  phoneDisplay: "+7 (965) 007-56-07",
  phoneTel: "+79650075607",
  supportEmail: "support@fetchnow.online",
  copyrightEmail: "copyright@fetchnow.online",
});

export const commercialPremium = Object.freeze({
  priceRub: 100,
  durationHours: 24,
  /** Honest interim notice while LIVE payments are not enabled. */
  interimNotice:
    "Коммерческий тариф — 100 ₽ / 24 часа. Приём реальных платежей пока не открыт.",
});
