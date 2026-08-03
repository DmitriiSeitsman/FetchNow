# 06. Docker Compose

## Понятия

Compose описывает project из services, networks и volumes. Service — шаблон одного или нескольких containers. Profile включает опциональную группу (в FetchNow profiles нет). Override объединяется с основным YAML. `environment` задаёт значения прямо, `env_file` загружает файл в контейнер (в текущем Compose `env_file` не используется). `depends_on` задаёт порядок и может ждать healthy dependency, но не заменяет runtime retry. Resource limits ограничивают CPU/RAM.

## FetchNow на shared server

Явный `-p fetchnow-staging` изолирует имена containers/networks и команды управления от других Compose projects. Однако named volumes в текущем YAML имеют явные глобальные `name: fetchnow_pgdata` и `name: fetchnow_tmp`: project name их **не изолирует и не переименует**. Local и staging на одном Docker host способны подключить одни и те же database/temp volumes.

**STOP:** до staging deployment Compose нужно отдельно исправить или параметризовать так, чтобы rendered config показывал staging-specific volume names. Эта документационная задача Compose не меняет. Deployment запрещён, если `docker compose config` для local и staging показывает одинаковые volume names либо уже существующий volume не имеет подтверждённого владельца/environment.

Для production-shaped конфигурации указывайте оба файла явно, чтобы локальный `compose.override.yaml` не публиковал API `8000`:

```bash
docker compose -p fetchnow-staging \
  -f compose.yaml -f deploy/compose/compose.prod.yaml config
docker compose -p fetchnow-staging \
  -f compose.yaml -f deploy/compose/compose.prod.yaml build
docker compose -p fetchnow-staging \
  -f compose.yaml -f deploy/compose/compose.prod.yaml up -d
docker compose -p fetchnow-staging \
  -f compose.yaml -f deploy/compose/compose.prod.yaml ps
docker compose -p fetchnow-staging \
  -f compose.yaml -f deploy/compose/compose.prod.yaml logs --tail 200
docker compose -p fetchnow-staging \
  -f compose.yaml -f deploy/compose/compose.prod.yaml restart SERVICE
```

`config` — **CHECK** (но может читать env); ожидается пять services, единственная host-публикация gateway `127.0.0.1:8091→8080`, без port у postgres/api, а до deployment — staging-specific volume names. В текущем коде последнее условие не выполнено. `build`, `up` и `restart` — **WARNING modifying**. `ps` и `logs` — проверки.

**CHECK:** rendered config может содержать подставленный пароль БД; не прикладывайте полный вывод к публичному тикету. Проверяйте секреты локально и редактируйте вывод.

## Ошибки и откат

- Compose автоматически читает `.env` для interpolation, но это не то же самое, что `env_file`.
- Запуск без `-f` подхватит local override и конфликтующий `8000`.
- Запуск без `-p` может создать/изменить не тот project.
- `restart` не применяет новый image/env; для этого нужен recreate через `up -d`.

**ROLLBACK:** вернуться к сохранённому Git commit/image и выполнить `up -d` после compatibility check. `down` останавливает весь project и обычно не нужен для deploy. **DANGER:** никогда не добавлять `-v` к `down` в штатной операции.
