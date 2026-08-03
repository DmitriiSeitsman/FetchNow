# 22. Учебные упражнения

Все задания выполнять локально или на учебном выводе. Production не ломать. Разрешены только указанные read-only команды, кроме явно локального Compose restart.

## 1. Процесс по порту

**Цель:** найти владельца `8091`. **Дано:** строка `LISTEN ... 127.0.0.1:8091 ... users:(("docker-proxy",pid=3141))`. **Можно:** `ss -lntup`. **Критерий:** назвать bind, port, process/PID. **Подсказка:** смотрите local address.

<details><summary>Разбор</summary>`docker-proxy`, PID 3141 слушает TCP только на loopback `127.0.0.1:8091`; извне напрямую порт недоступен.</details>

## 2. Bind address

**Цель:** отличить безопасную публикацию. **Дано:** `0.0.0.0:8091` и `127.0.0.1:8091`. **Можно:** `ss`. **Критерий:** выбрать loopback. **Подсказка:** `0.0.0.0` — все IPv4 interfaces.

<details><summary>Разбор</summary>Для gateway за host Nginx нужен `127.0.0.1:8091`; `0.0.0.0` расширяет поверхность.</details>

## 3. DNS

**Цель:** сверить staging A. **Дано:** ожидаемый IP `77.239.107.143`. **Можно:** `dig ... A +short`, `getent ahosts`. **Критерий:** оба resolver paths согласованы.

<details><summary>Разбор</summary>Если `dig` и `getent` различаются, исследуйте cache/resolver; к Certbot не переходите.</details>

## 4. Loopback против wildcard

**Цель:** объяснить термины. **Дано:** два адреса выше. **Можно:** handbook. **Критерий:** упомянуть interfaces и remote reachability.

<details><summary>Разбор</summary>`127.0.0.1` принимает host-local traffic, `0.0.0.0` bind-ит все IPv4 interfaces; firewall всё равно проверяется отдельно.</details>

## 5. Найти Compose service

**Цель:** определить HTTP entrypoint. **Дано:** `compose.yaml`. **Можно:** `docker compose config`, поиск по файлу. **Критерий:** `gateway`, container port 8080.

<details><summary>Разбор</summary>`gateway` — единственный base service с `ports`; он проксирует к `web` и `api`.</details>

## 6. Прочитать healthcheck

**Цель:** понять, что проверяет API. **Дано:** Compose healthcheck. **Можно:** чтение YAML. **Критерий:** liveness, не readiness/DB.

<details><summary>Разбор</summary>API healthcheck вызывает `/api/v1/health/live`; PostgreSQL проверяется отдельным healthcheck и readiness endpoint.</details>

## 7. Найти volume

**Цель:** разделить DB и temp state. **Дано:** `fetchnow_pgdata`, `fetchnow_tmp`. **Можно:** `docker volume ls/inspect`. **Критерий:** mapping к PostgreSQL и API/worker.

<details><summary>Разбор</summary>DB mount — `/var/lib/postgresql/data`; temp mount — `/var/lib/fetchnow/tmp`.</details>

## 8. Проверить Nginx config

**Цель:** безопасный gate. **Дано:** новый site file уже подготовлен учебно. **Можно:** `sudo nginx -t`. **Критерий:** не reload при failed test.

<details><summary>Разбор</summary>Только `syntax is ok` и `test is successful` разрешают перейти к контролируемому reload.</details>

## 9. Request ID в logs

**Цель:** связать запрос. **Дано:** ID `exercise-42` и redacted logs. **Можно:** `docker compose ... logs`, `rg 'exercise-42'`. **Критерий:** найти gateway/API события без source URL.

<details><summary>Разбор</summary>Gateway передаёт `X-Request-ID`; correlation выполняется по ID, status и route, не по секретной URL.</details>

## 10. Database readiness

**Цель:** отличить live от ready. **Дано:** live 200, ready 503 `not_ready`. **Можно:** curl, `pg_isready`, DB logs. **Критерий:** указать PostgreSQL dependency.

<details><summary>Разбор</summary>API process жив, но DB check failed; перезапуск API без анализа БД не является решением.</details>

## 11. Local Compose restart

**Цель:** увидеть lifecycle. **Дано:** только локальный project с тестовыми данными. **Можно:** `docker compose ps`, `docker compose restart api`, `docker compose logs`. **Критерий:** API снова healthy. **Подсказка:** restart не rebuild.

<details><summary>Разбор</summary>Тот же container/image/env перезапускается; после healthcheck gateway продолжает работу.</details>

## 12. Почему PostgreSQL сохранился

**Цель:** понять persistence. **Дано:** локальный container перезапущен, запись осталась. **Можно:** `docker volume inspect`, Compose YAML. **Критерий:** назвать `fetchnow_pgdata`.

<details><summary>Разбор</summary>Данные находятся в named volume, lifecycle которого независим от restart/recreate container.</details>

## 13. План rollback

**Цель:** написать план без выполнения. **Дано:** API regression, migration только добавила nullable column. **Можно:** Git/Compose read-only inventory. **Критерий:** references, compatibility, config, recreate, smoke, observation; без downgrade.

<details><summary>Разбор</summary>Сохранить evidence, подтвердить backward compatibility, вернуть прежний image, `up -d`, пройти gates; schema оставить.</details>

## 14. Mock 502

**Цель:** диагностировать proxy failure. **Дано:** public 502, localhost gateway тоже 502, `api` exited. **Можно:** curl, `ps -a`, logs. **Критерий:** начать с API exit/logs, не DNS/TLS.

<details><summary>Разбор</summary>Host и gateway доступны; upstream API отсутствует. Сохраните exit/logs, выясните причину, затем recreate/recover.</details>

## 15. Low disk

**Цель:** выбрать безопасное действие. **Дано:** `/var` 96%, `docker system df` 8 GB images, PostgreSQL 20 GB, temp 6 GB. **Можно:** `df`, `du`, `docker system df`. **Критерий:** остановить новые writes, определить ownership/TTL; не prune/удалять DB.

<details><summary>Разбор</summary>Сначала containment и inventory. Проверить expired temp/log rotation и add capacity; reclaimable Docker size не является разрешением на prune.</details>

## 16. Deceptive provider hostname

**Цель:** понять exact allowlist. **Дано:** `https://vk.com.attacker.example/video`. **Можно:** чтение provider registry/ADR 0004, без DNS/Internet. **Критерий:** объяснить `UNSUPPORTED_PROVIDER` и отсутствие resolver call. **Подсказка:** сравнивается всё normalized hostname, не suffix.

<details><summary>Разбор</summary>Hostname равен `vk.com.attacker.example`, а не `vk.com`. Exact registry отклоняет его до DNS, поэтому attacker-controlled domain не получает доверия из-за текста слева.</details>

## 17. Redirect на private IP

**Цель:** найти security gate. **Дано:** validated VK URL отвечает `302 Location: http://127.0.0.1/internal`. **Можно:** чтение client/ADR 0005, fake test data; реальный запрос запрещён. **Критерий:** назвать manual redirect, повторную validation и `BLOCKED_DESTINATION` до следующего HTTP call.

<details><summary>Разбор</summary>Automatic redirects выключены. Client строит новый URL и передаёт его validator; literal loopback блокируется. Если исходный URL HTTPS, downgrade также недопустим, но private destination уже не должен быть contacted.</details>

## 18. HEAD fallback

**Цель:** отличить compatibility signal от success. **Дано:** VK: `HEAD → 418 → GET 200 text/html`; Rutube: `HEAD → 404`. **Можно:** provider registry и unit-test output, без Internet. **Критерий:** VK выполняет bounded GET и возвращает `methodUsed=GET`; Rutube 404 даёт `SOURCE_UNAVAILABLE` без GET.

<details><summary>Разбор</summary>VK allowlist fallback содержит 418/405/501, Rutube — 405/501. Fallback status только разрешает повтор GET; успех определяется итоговым 2xx с допустимым MIME и соблюдением body limits.</details>
