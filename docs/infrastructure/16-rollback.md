# 16. Rollback

Application rollback возвращает прежние code/images/config. Database остаётся persistent и может уже иметь новую schema. Поэтому migrations проектируют backward-compatible: сначала добавление, затем переход кода, и только в будущем удаление старого. Автоматический Alembic downgrade запрещён.

## Подготовка release

Для каждого deploy сохранять commit SHA, image IDs, rendered config hash без secrets, Alembic revision, backup path/checksum и smoke results. В репозитории rollback script отсутствует; не выдумывать его имя.

## Decision table

| Событие | Решение |
|---|---|
| Frontend error, API healthy | вернуть `web`/gateway image, проверить cache и root smoke |
| API crash | сохранить logs, проверить schema compatibility, вернуть API/worker image |
| Migration failed до apply | rollout остановить; DB не менять, вернуть app |
| Migration частично applied/неясно | STOP; DBA-style analysis, backup, `alembic_version`; не downgrade вслепую |
| Readiness fails | проверить PostgreSQL; rollback app только если regression подтверждён |
| External provider unavailable | не rollback автоматически; health может быть исправен, временно disable provider после реализации control |
| Disk low | остановить новые writes/jobs, измерить; targeted cleanup/capacity action, не prune |

## Runbook

1. Объявить rollback decision и freeze новых изменений.
2. Сохранить evidence и текущие references.
3. Проверить, совместима ли прежняя app со schema.
4. Направить active release на прежний reviewed commit/image.
5. `docker compose ... config`, затем `up -d` без `down -v`.
6. Проверить DB, live/ready, web, worker, public TLS и neighbours.
7. Наблюдать; записать причину и результат.

Health gate тот же, что при deploy. Если старая версия несовместима, rollback заблокирован: предпочтительнее forward fix либо restore в отдельное окружение с контролируемым cutover.

**DANGER:** не удалять новый release/images до анализа, не делать `git reset --hard` в shared checkout, не восстанавливать DB без подтверждённого RPO и не считать restart rollback.

