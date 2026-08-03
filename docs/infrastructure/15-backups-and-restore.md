# 15. Backup и restore

Backup — отдельная восстанавливаемая копия данных; snapshot фиксирует состояние volume/VM на момент времени, но может зависеть от того же provider/account и без DB coordination быть неконсистентным. Backup, для которого никогда не проверяли restore, — разновидность оптимизма, а не стратегия.

## PostgreSQL logical dump

В репозитории backup script отсутствует. Ниже шаблон ручной будущей операции; имя файла содержит UTC timestamp, а credentials берутся из защищённого env, не из command line.

```bash
umask 077
install -d -m 0700 /srv/fetchnow-staging/backups
docker compose --env-file .env.staging --project-name fetchnow-staging \
  -f compose.yaml -f compose.staging.yaml exec -T postgres \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > /srv/fetchnow-staging/backups/fetchnow-YYYYMMDDTHHMMSSZ.dump
stat -c '%a %s %n' /srv/fetchnow-staging/backups/fetchnow-YYYYMMDDTHHMMSSZ.dump
pg_restore --list /srv/fetchnow-staging/backups/fetchnow-YYYYMMDDTHHMMSSZ.dump | head
```

**WARNING modifying:** создаёт файл; shell redirection выполняется с правами текущего пользователя. Ожидаются mode 600 (при umask 077), ненулевой размер и читаемый catalog. Не использовать literal timestamp: подставить заранее проверенное UTC-значение без секретов.

## Restore test

Restore выполняют только в изолированную временную database/project, никогда поверх active DB. Согласовать имя, создать пустую test DB, применить `pg_restore`, сверить schema/Alembic и выборочные counts, затем удалить test DB отдельной утверждённой операцией. Здесь destructive-команды намеренно не приведены.

## Retention и off-server copy

Политика должна задать, например, daily/weekly generations по объёму и RPO, но репозиторий её пока не фиксирует. До автоматизации оператор хранит inventory: timestamp, commit, DB revision, checksum, size, restore-test result. Хотя бы одна encrypted/authenticated copy должна находиться вне сервера и отдельного failure domain. Encryption — обязательное будущее усиление при появлении пользовательских данных; ключ нельзя хранить рядом с backup.

`fetchnow_tmp` не является backup и содержит временное состояние. Для будущих media artifacts действуют TTL и [file lifecycle policy](../product/file-lifecycle-policy.md), а не бессрочная архивация.

## Ошибки и rollback

Нулевой/truncated dump, world-readable mode или failed catalog check → backup недействителен. Не удалять предыдущую good copy. Restore rollback — прекратить работу с test target; изменение active DB требует отдельного recovery plan. **DANGER:** не писать пароль в filename/command, не хранить единственную копию на том же disk и не восстанавливать поверх production без подтверждённого downtime/backup.
