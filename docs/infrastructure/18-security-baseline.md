# 18. Базовая безопасность

## Infrastructure controls

- Least privilege: host-операции через `cryptobot` + точечный `sudo`; backend/web/gateway — non-root UID 10001.
- Firewall включён; gateway bind `127.0.0.1:8091`; PostgreSQL/API не имеют staging public port.
- Secrets вне Git/images/logs, mode 600/700, уникальные production credentials.
- Containers без `privileged` и Docker socket mount; TLS validation включена.
- Backups ограничены по правам, проверяются restore и копируются off-server.

## Реализованная URL validation (PR1)

`POST /api/v1/media/validate` принимает абсолютный HTTP/HTTPS URL, нормализует scheme/hostname через IDNA, удаляет fragment из canonical, запрещает credentials и недопустимые ports. Exact provider registry с enable flags проверяется до DNS. Затем resolver классифицирует все IPv4/IPv6 addresses; empty, loopback/private/link-local/reserved и mixed public/private наборы fail closed. Public errors стабильны, raw URL/query secrets не логируются. См. [ADR 0004](../adr/0004-provider-registry-and-dns-validation.md).

Registry:

- VK (`vk`, `VK`): `vk.com`, `www.vk.com`, `m.vk.com`, `vk.ru`, `www.vk.ru`, `m.vk.ru`, `vkvideo.ru`, `www.vkvideo.ru`, `m.vkvideo.ru`; enabled default `true`. Exact hosts only — no suffix matching; `login.vk.ru` and unknown subdomains are unsupported.
- Rutube (`rutube`, `Rutube`): `rutube.ru`, `www.rutube.ru`; enabled default `true`.

## Реализованный safe outbound probe (PR2)

`POST /api/v1/media/probe` использует async pooled `SafeHTTPClient` внутри API. Automatic redirects отключены; 301/302/303/307/308 обрабатываются вручную, каждый hop заново проходит URL/provider/DNS/IP validation, остаётся в исходном provider и не может сделать HTTPS→HTTP downgrade.

Перед HTTP call hostname дважды resolve-ится: оба набора должны быть непустыми и полностью публичными, их пересечение — непустым. Это best-effort DNS TOCTOU mitigation, не полноценное IP pinning. Hostname URL сохраняется для SNI и certificate verification.

HEAD fallback provider-specific: VK `{418,405,501}`, Rutube `{405,501}`. Такой status не успех, а разрешение на bounded streaming GET. GET требует MIME из `text/html`, `application/json`, `text/plain`, останавливается на soft probe limit и защищён hard response limit/timeouts. Cookies, Authorization, Referer, Range, browser fingerprint и пользовательские headers не используются. Raw URL/query, `Location`, IP и body не логируются. См. [ADR 0005](../adr/0005-safe-outbound-http-and-redirects.md).

## Что защита не означает

Validate/probe не являются downloader или yt-dlp metadata extraction. Residual DNS race между последним resolve и TCP/TLS handshake сохраняется. Нельзя отключать TLS verification, расширять provider suffix match, добавлять cookies или снимать limits ради совместимости.

## Planned/not implemented

Нет Turbo/payments/recovery links, runtime file lifecycle/cleanup worker как отдельный сервис, application-level rate limiting, host Nginx/TLS staging publish automation beyond existing operator tooling, отдельный StorageProvider или S3 implementation. ffmpeg/ffprobe присутствуют в shared backend image (Debian bookworm package); API/delivery не получают paths и не запускают tools. Полный threat model и logging rules находятся в [security docs](../security/threat-model.md), их не следует дублировать здесь целиком.
