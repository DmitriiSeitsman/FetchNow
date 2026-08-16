# 12. Обычное развёртывание

**INFO:** Для актуального staging release path используйте главы
[24](24-release-preflight-health.md)–[29](29-verified-migration-transaction.md)
(`preflight → prepare → verify → deploy-plan → migrate-if-required →
rollout → health`). Плановый production path и blockers — в
[главе 30](30-production-release-runbook.md). Ниже сохранён исторический
checklist; не выдумывать отсутствующие script names.

## Термины

`checkout` выбирает Git revision; `pull` получает и интегрирует remote branch. `build` создаёт image, `rebuild` повторяет это для нового кода. `restart` перезапускает тот же container без нового env/image. `recreate` заменяет container согласно актуальному config/image. `rollback` возвращает проверенную release reference, но не автоматически БД.

## Runbook

1. Выполнить server preflight и сохранить текущие `git rev-parse HEAD`, `docker compose ... images/ps`, Alembic revision.
2. Получить reviewed release в новом каталоге, не изменяя active checkout на месте.
3. Создать timestamped DB backup и подтвердить файл/permissions.
4. Выполнить `docker compose ... config` и проверить published ports/secrets redaction.
5. Build images; старые working images пока не удалять.
6. `alembic current`; прочитать migrations; применить `alembic upgrade head` только при готовом rollback decision.
7. `up -d` применяет recreate только нужных services.
8. Проверить `ps`, localhost live/ready/root, worker logs, затем public HTTPS.
9. Наблюдать error rate, resources и логи хотя бы согласованное окно.
10. Принять решение оставить release или откатить по [главе 16](16-rollback.md).

### Existing automation

Единственный существующий deployment script — `deploy/scripts/migrate.sh`. `make migrate` — существующий shortcut для Alembic migration, но он не вызывает этот script напрямую. На staging необходимо сохранить явные project/files, например через `COMPOSE` только после проверки итоговой команды.

### Planned PRD1

Scripts deploy staging, rollback, healthcheck и backup отсутствуют и только планируются. Не выдумывать имена и вызовы; до их появления workflow остаётся ручным checklist с verification gates.

## Verification и rollback

Release успешен, когда live/ready, web, worker, PostgreSQL, public TLS и соседние services прошли gates, а ресурсные метрики стабильны. External provider failure сам по себе не доказывает дефект release.

**ROLLBACK:** переключить active release на прежний commit/image, `config`, `up -d`, smoke. Не выполнять Alembic downgrade автоматически. **DANGER:** не использовать `git pull` внутри изменённого working tree, не rebuild без сохранения прежней reference и не удалять build cache/images до окончания окна rollback.
