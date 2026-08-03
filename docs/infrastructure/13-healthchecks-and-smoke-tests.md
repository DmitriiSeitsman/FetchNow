# 13. Healthchecks и smoke tests

Проверки разделены по назначению. Внешний provider никогда не должен определять liveness процесса: его временный сбой иначе вызовет бессмысленные restart и усилит инцидент.

## Уровень 1: liveness

```bash
curl -fsS http://127.0.0.1:8091/api/v1/health/live
```

`GET /api/v1/health/live` проверяет только, что API process жив; ожидается 200 и `{"status":"ok"}`. Он не проверяет PostgreSQL или Internet.

## Уровень 2: readiness

```bash
curl -fsS http://127.0.0.1:8091/api/v1/health/ready
```

`GET /api/v1/health/ready` проверяет API и PostgreSQL. Ожидается 200; при недоступной БД — 503 с `not_ready`. Public варианты через `https://staging.fetchnow.online` дополнительно проверяют DNS, TLS и оба Nginx.

## Уровень 3: URL validation smoke

`POST /api/v1/media/validate` не открывает HTTP-соединение к media page, но выполняет parse/provider lookup/DNS/IP classification. Замените placeholder локально; реальные media URLs в Git не сохранять.

```bash
curl -sS -X POST http://127.0.0.1:8091/api/v1/media/validate \
  -H 'Content-Type: application/json' \
  --data '{"url":"<PUBLIC_VK_URL>"}'
curl -sS -X POST http://127.0.0.1:8091/api/v1/media/validate \
  -H 'Content-Type: application/json' \
  --data '{"url":"<PUBLIC_RUTUBE_URL>"}'
curl -sS -X POST http://127.0.0.1:8091/api/v1/media/validate \
  -H 'Content-Type: application/json' \
  --data '{"url":"https://unsupported.example/item"}'
curl -sS -X POST http://127.0.0.1:8091/api/v1/media/validate \
  -H 'Content-Type: application/json' \
  --data '{"url":"http://127.0.0.1/item"}'
curl -sS -X POST http://127.0.0.1:8091/api/v1/media/validate \
  -H 'Content-Type: application/json' \
  --data '{"url":"https://user:pass@vk.com/item"}'
curl -sS -X POST http://127.0.0.1:8091/api/v1/media/validate \
  -H 'Content-Type: application/json' \
  --data '{"url":"<SECRET_QUERY_TEST_URL>"}'
```

Ожидаются: VK/Rutube — 200; unknown — `UNSUPPORTED_PROVIDER`; localhost/private — `BLOCKED_DESTINATION`; credentials — `INVALID_URL`. Secret query не должна появиться в response canonical или logs. Placeholder должен быть целой JSON string; если подставляете URL программно, используйте JSON encoder, а не shell concatenation.

## Уровень 4: safe network probe

```bash
curl -sS -X POST http://127.0.0.1:8091/api/v1/media/probe \
  -H 'Content-Type: application/json' \
  --data '{"url":"<PUBLIC_VK_URL>"}'
curl -sS -X POST http://127.0.0.1:8091/api/v1/media/probe \
  -H 'Content-Type: application/json' \
  --data '{"url":"<PUBLIC_RUTUBE_URL>"}'
```

Это diagnostic HEAD/limited GET, не liveness/readiness, downloader или metadata extraction. VK HEAD `418/405/501` разрешает bounded GET fallback; Rutube fallback — только `405/501`, а обычный HEAD 200 завершается без GET. Fallback status сам по себе не успех: успешным должен быть итоговый 2xx response с допустимым MIME.

Probe зависит от provider, DNS, TLS и Internet. Provider может временно блокировать IP дата-центра; failure не обязательно означает неисправность FetchNow process. Не запускать probe каждую минуту обычным health monitor и не лечить его cookies, Authorization, TLS bypass или browser fingerprint imitation.

## Диагностика и neighbour checks

```bash
systemctl is-active crypto-bot
systemctl is-active justtwo-api
sudo ss -lntp | grep -E ':(8000|8080|8091)\b'
```

Live fail → process/gateway logs. Live OK + ready fail → PostgreSQL. Local OK + public fail → host Nginx/TLS/DNS. Validate fail → input/provider/DNS policy. Probe fail при health OK → outbound policy или provider. HTTP endpoints соседей репозиторий не документирует: использовать их собственный runbook.

Проверки read-only с точки зрения FetchNow state, но probe создаёт внешний запрос. **DANGER:** не использовать реальные secret URLs, destructive payloads и не отключать TLS validation.
