/** FetchNow — скачать видео по ссылке. Без рекламы. Вообще. */

export class FlowError extends Error {
  readonly code: string;
  readonly userMessage: string;
  readonly retryable: boolean;
  readonly retryAfterMs: number | null;

  constructor(
    code: string,
    userMessage: string,
    retryable = false,
    retryAfterMs: number | null = null,
  ) {
    super(userMessage);
    this.name = "FlowError";
    this.code = code;
    this.userMessage = userMessage;
    this.retryable = retryable;
    this.retryAfterMs = retryAfterMs;
  }
}

export function isAbortError(err: unknown): boolean {
  return (
    (err instanceof DOMException && err.name === "AbortError") ||
    (err instanceof Error && err.name === "AbortError")
  );
}

export const GENERIC_USER_MESSAGE =
  "Что-то пошло не так. Попробуйте ещё раз чуть позже или начните сначала.";

const GENERIC = GENERIC_USER_MESSAGE;

const MESSAGES: Record<string, { text: string; retryable: boolean }> = {
  INVALID_URL: { text: "Эту ссылку не удалось принять.", retryable: false },
  UNSUPPORTED_SCHEME: {
    text: "Разрешены только ссылки http и https.",
    retryable: false,
  },
  UNSUPPORTED_PROVIDER: {
    text: "Этот сайт пока не поддерживается. Попробуйте публичную ссылку VK, RUTUBE, Одноклассников или Дзена.",
    retryable: false,
  },
  BLOCKED_DESTINATION: {
    text: "Этот адрес недоступен для скачивания.",
    retryable: false,
  },
  WRAPPER_UNSUPPORTED: {
    text: "Такой тип обёрнутой ссылки не поддерживается.",
    retryable: false,
  },
  WRAPPER_UNRESOLVED: {
    text: "Не удалось найти поддерживаемое видео на странице превью.",
    retryable: false,
  },
  RESOLVED_PROVIDER_UNSUPPORTED: {
    text: "Видео ведёт на неподдерживаемый сайт.",
    retryable: false,
  },
  RESOLUTION_LOOP: {
    text: "Не удалось разобрать страницу превью.",
    retryable: false,
  },
  RESOLUTION_LIMIT_EXCEEDED: {
    text: "Не удалось разобрать страницу превью.",
    retryable: false,
  },
  UNSAFE_RESOLUTION_TARGET: {
    text: "Этот адрес недоступен для скачивания.",
    retryable: false,
  },
  MEDIA_INSPECTION_FAILED: {
    text: "Не удалось прочитать информацию о медиа по этой ссылке.",
    retryable: false,
  },
  CAPACITY_UNAVAILABLE: {
    text: "Сервис сейчас перегружен. Попробуйте позже.",
    retryable: true,
  },
  RATE_LIMITED: {
    text: "Слишком много запросов. Подождите немного и попробуйте снова.",
    retryable: true,
  },
  JOB_NOT_FOUND: { text: GENERIC, retryable: false },
  JOB_EXPIRED: {
    text: "Проверка устарела. Начните сначала с той же ссылкой.",
    retryable: false,
  },
  INVALID_ACCESS_TOKEN: { text: GENERIC, retryable: false },
  IDEMPOTENCY_CONFLICT: { text: GENERIC, retryable: false },
  DOWNLOADS_DISABLED: {
    text: "Скачивание сейчас недоступно.",
    retryable: true,
  },
  DELIVERY_DISABLED: {
    text: "Сохранение файлов сейчас недоступно.",
    retryable: true,
  },
  DOWNLOAD_JOB_NOT_FOUND: { text: GENERIC, retryable: false },
  PARENT_JOB_NOT_READY: {
    text: "Информация о медиа ещё не готова. Подождите немного.",
    retryable: true,
  },
  FORMAT_NOT_FOUND: {
    text: "Это качество больше недоступно. Начните сначала, чтобы обновить варианты.",
    retryable: false,
  },
  FORMAT_NOT_ELIGIBLE: {
    text: "Это качество недоступно в текущем бесплатном режиме.",
    retryable: false,
  },
  FORMAT_UNAVAILABLE: {
    text: "Это качество больше недоступно. Начните сначала, чтобы обновить варианты.",
    retryable: false,
  },
  MUXING_UNAVAILABLE: {
    text: "Это медиа недоступно как единый файл с видео и звуком.",
    retryable: false,
  },
  MUXING_FAILED: {
    text: "Не удалось подготовить файл. Попробуйте позже.",
    retryable: true,
  },
  MUXING_TIMEOUT: {
    text: "Подготовка файла заняла слишком много времени. Попробуйте позже.",
    retryable: true,
  },
  MUXED_OUTPUT_INVALID: {
    text: "Подготовленный файл нельзя использовать. Начните сначала.",
    retryable: false,
  },
  DOWNLOAD_TIMEOUT: {
    text: "Подготовка файла заняла слишком много времени. Попробуйте позже.",
    retryable: true,
  },
  DOWNLOAD_TOO_LARGE: {
    text: "Файл больше лимита бесплатного скачивания.",
    retryable: false,
  },
  DOWNLOAD_TOOL_FAILED: {
    text: "Не удалось подготовить файл. Попробуйте позже.",
    retryable: true,
  },
  DOWNLOAD_INVALID_OUTPUT: {
    text: "Подготовленный файл нельзя использовать. Начните сначала.",
    retryable: false,
  },
  DOWNLOAD_STORAGE_UNAVAILABLE: {
    text: "Хранилище временно недоступно. Попробуйте позже.",
    retryable: true,
  },
  DOWNLOAD_EXPIRED: {
    text: "Подготовленный файл устарел. Начните сначала, чтобы подготовить его снова.",
    retryable: false,
  },
  DOWNLOAD_CANCELLED: {
    text: "Скачивание отменено",
    retryable: false,
  },
  DOWNLOAD_NOT_READY: {
    text: "Файл ещё не готов. Подождите немного.",
    retryable: true,
  },
  FILE_TOO_LARGE: {
    text: "Файл больше лимита бесплатного скачивания.",
    retryable: false,
  },
  // Keep the stated maximum in sync with MAX_SOURCE_DURATION_SECONDS.
  DURATION_TOO_LONG: {
    text: "Это видео длиннее максимума в 2 часа. Попробуйте более короткое.",
    retryable: false,
  },
  INTERNAL_ERROR: { text: GENERIC, retryable: true },
  SOURCE_UNAVAILABLE: {
    text: "Источник недоступен. Попробуйте позже.",
    retryable: true,
  },
  SOURCE_TIMEOUT: {
    text: "Источник не ответил вовремя. Попробуйте позже.",
    retryable: true,
  },
  NETWORK_ERROR: {
    text: "Сетевой запрос не удался. Проверьте соединение и попробуйте снова.",
    retryable: true,
  },
  BROWSER_UNSUPPORTED: {
    text: "«Сохранить как…» доступно в браузерах с File System Access API (актуальный Chromium на компьютере). Используйте «Скачать файл» или откройте страницу в поддерживаемом браузере.",
    retryable: false,
  },
  HTTPS_REQUIRED: {
    text: "Откройте эту страницу по HTTPS, чтобы скачать подготовленный файл.",
    retryable: false,
  },
  SAVE_FAILED: {
    text: "Файл не удалось сохранить полностью. Он не отмечен как завершённый.",
    retryable: false,
  },
  CONTRACT: { text: GENERIC, retryable: false },
};

export function userMessageForCode(code: string | null | undefined): {
  text: string;
  retryable: boolean;
} {
  if (!code || typeof code !== "string") {
    return { text: GENERIC, retryable: false };
  }
  return MESSAGES[code] ?? { text: GENERIC, retryable: false };
}

export function flowErrorFromCode(
  code: string | null | undefined,
  fallback = "CONTRACT",
  retryAfterMs: number | null = null,
): FlowError {
  const resolved = code && MESSAGES[code] ? code : fallback;
  const mapped = userMessageForCode(resolved);
  return new FlowError(resolved, mapped.text, mapped.retryable, retryAfterMs);
}
