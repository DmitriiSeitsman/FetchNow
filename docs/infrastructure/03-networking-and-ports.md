# 03. Сеть и порты

## Основы

IPv4 — 32-битный адрес. Public IP маршрутизируется из Internet; `77.239.107.143` — документированный внешний IP планового staging. Loopback `127.0.0.0/8` остаётся внутри host. Bind address определяет интерфейс: `127.0.0.1` принимает только локальные соединения, `0.0.0.0` — на всех IPv4-интерфейсах.

TCP создаёт надёжное соединение к `IP:port`. Inbound-трафик приходит на сервер, outbound выходит к внешнему ресурсу. Firewall фильтрует его. Reverse proxy принимает HTTP/TLS и передаёт запрос внутреннему upstream. NAT подменяет адрес/порт между сетями; Docker использует его для published ports.

## Почему схема FetchNow безопаснее

Host Nginx — единая публичная точка `80/443`, где управляются TLS и virtual hosts. Gateway должен публиковаться как `127.0.0.1:8091`, чтобы firewall/Docker rules не сделали его публичным. PostgreSQL вообще не публикуется: `api`/`worker` находят `postgres:5432` через Compose DNS. Docker не занимает `80/443`, а также `8000` CryptoBot и `8080` JustTwo.

Фактический staging publish задаётся в `compose.staging.yaml` как `${GATEWAY_PORT:-127.0.0.1:8091}:8080`. Local publish — только через `compose.override.yaml` (`8080:8080`). Base `compose.yaml` host ports не публикует. Перед запуском staging подтвердите loopback-only binding через `docker compose ... config`.

## Безопасность пользовательского URL после PR2

Пользовательский URL — недоверенный ввод. Pipeline сначала нормализует URL и проверяет exact provider registry; неизвестный домен получает `UNSUPPORTED_PROVIDER` **до DNS**, поэтому FetchNow нельзя использовать как произвольный DNS resolver. Затем проверяются все A/AAAA: пустой набор, loopback/private/link-local/reserved и смешанный public/private набор отклоняются.

Safe outbound probe рассматривает каждый redirect как новый URL: снова применяет provider/DNS/IP validation, требует тот же provider и запрещает HTTPS→HTTP downgrade. Даже redirect внутри VK не получает автоматического доверия.

DNS preflight не гарантирует адрес будущего TCP connection. Перед каждым запросом client дважды resolve-ит hostname, требует два полностью публичных непустых набора и их непустое пересечение. Это best-effort защита от DNS TOCTOU/rebinding, но не IP pinning: изменение между последним resolve и handshake остаётся residual risk. Hostname сохраняется ради SNI/certificate validation; TLS verification нельзя отключать.

Raw URL, query/fragment, credentials, полный `Location`, resolved IP и response body не должны попадать в logs. Детали решений: [ADR 0004](../adr/0004-provider-registry-and-dns-validation.md) и [ADR 0005](../adr/0005-safe-outbound-http-and-redirects.md).

## Диагностика

```bash
ip -brief address
ip route
sudo ss -lntup
curl -fsS http://127.0.0.1:8091/api/v1/health/live
getent ahosts staging.fetchnow.online
dig staging.fetchnow.online A +short
sudo ufw status verbose
```

`ss` должен показать `127.0.0.1:8091`, не `0.0.0.0:8091`. `curl` ожидает JSON `{"status":"ok"}` после запуска. `getent` показывает системное разрешение имён, `dig` — DNS. UFW должен разрешать необходимый SSH и `80/443`; точные правила сначала инвентаризируют.

## Типичные ошибки

- `Connection refused`: никто не слушает порт либо контейнер не стартовал.
- Timeout: firewall, routing или зависший upstream.
- `502`: Nginx доступен, upstream нет.
- Порт занят: `sudo ss -lntup '( sport = :8091 )'` и определить владельца; не убивать процесс вслепую.

**ROLLBACK:** изменение bind/Firewall отменяют возвратом сохранённой конфигурации и повторной проверкой. **DANGER:** не отключать UFW, не публиковать `5432`, не использовать `0.0.0.0:8091` и не менять соседние порты.
