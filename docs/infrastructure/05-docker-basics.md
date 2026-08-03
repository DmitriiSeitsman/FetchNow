# 05. Основы Docker

## Как устроено

Image — неизменяемый шаблон из read-only layers; registry хранит images. Container — запущенный экземпляр image с временным writable layer. Docker daemon управляет ими, CLI отправляет ему команды. Volume хранит данные независимо от контейнера; bind mount подключает конкретный host path. Network соединяет контейнеры и даёт им DNS. Restart policy задаёт реакцию daemon после сбоя/reboot. Healthcheck измеряет внутреннюю исправность.

Build создаёт image из Dockerfile; start запускает уже созданный container. Recreate заменяет container, но сохраняет подключённые volumes. FetchNow запускает backend, web и gateway как non-root UID 10001; официальный PostgreSQL image управляет своим пользователем через entrypoint.

## Безопасная диагностика

```bash
docker version
docker info
docker ps
docker ps -a
docker images
docker volume ls
docker network ls
docker logs --tail 200 CONTAINER
docker inspect CONTAINER
docker stats --no-stream
docker system df
```

Первые две команды проверяют CLI/daemon. `ps` показывает running/all containers, `logs` — stdout/stderr, `inspect` — конфигурацию, `stats` — ресурсы, `system df` — учёт диска. На shared host сначала фильтруйте по Compose project, чтобы не спутать соседей.

## Lifecycle и ошибки

`created → running → exited → removed`. `unhealthy` означает провал healthcheck, но процесс может оставаться running. При restart контейнерный writable layer сохраняется, при recreate — нет; named volumes сохраняются в обоих случаях.

- `Cannot connect to Docker daemon`: проверить service/permissions, не использовать sudo как случайный обход.
- `port is already allocated`: найти listener и исправить bind.
- `no space left on device`: сначала измерить layers, logs, volumes и inodes.

## Опасные операции

**DANGER:** `docker system prune`, `docker compose down -v` и удаление volume способны уничтожить images/cache или данные. Они не входят в обычный поток. Не запускайте privileged containers, не mount-ите `/var/run/docker.sock` и не публикуйте сервисы на `0.0.0.0` без обоснования.

**ROLLBACK:** read-only диагностика не требует отката. Для изменения сохраняйте точную предыдущую image/release reference и Compose config; контейнер восстанавливается recreate из известного image, данные — только из volume/backup.

