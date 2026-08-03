# 02. Файловая система и пользователи

## Как устроено

- `/etc` — системная конфигурация; `/var` — изменяемые данные.
- `/var/log` — журналы, `/var/lib` — состояние сервисов.
- `/srv` — данные развёрнутых сервисов; `/opt` — дополнительное ПО.
- `/home` — личные каталоги; `/tmp` — краткоживущие временные файлы.

У файла есть owner, group и права read/write/execute для владельца, группы и остальных. `umask` убирает права из режима новых объектов. `chown` меняет владельца, `chmod` — разрешения; это не взаимозаменяемые операции. World-writable каталог доступен на запись любому локальному пользователю и создаёт риск подмены файлов и symlink-атак.

## FetchNow

`/srv/fetchnow-staging` отделяет сервис от домашнего каталога пользователя и соответствует назначению `/srv`:

```text
/srv/fetchnow-staging/
├── app/       # Git checkout или ссылка на release
├── env/       # environment/secrets вне Git
├── backups/   # локальные dumps с ограниченными правами
├── logs/      # только если нужны host-side exports
└── releases/  # версионированные checkout
```

Secrets отделены от checkout: `git pull`, rollback и случайный `git status` не должны раскрывать или перезаписывать их.

## Команды будущего deployment

**WARNING — modifying:** выполнять только после preflight и проверки пользователя `cryptobot`.

```bash
sudo install -d -o cryptobot -g cryptobot -m 0750 /srv/fetchnow-staging
sudo install -d -o cryptobot -g cryptobot -m 0750 /srv/fetchnow-staging/app
sudo install -d -o cryptobot -g cryptobot -m 0700 /srv/fetchnow-staging/env
sudo install -d -o cryptobot -g cryptobot -m 0700 /srv/fetchnow-staging/backups
sudo install -d -o cryptobot -g cryptobot -m 0750 /srv/fetchnow-staging/logs
sudo install -d -o cryptobot -g cryptobot -m 0750 /srv/fetchnow-staging/releases
namei -l /srv/fetchnow-staging/env
stat -c '%U:%G %a %n' /srv/fetchnow-staging/{app,env,backups,logs,releases}
umask
```

Ожидается owner/group `cryptobot:cryptobot`; `env` и `backups` — `700`, остальные — не writable для `other`. `namei` показывает права каждого компонента пути.

## Ошибки, откат и запреты

- `Permission denied`: сначала `namei -l` и `id`, не расширяйте права вслепую.
- Неверный owner после копирования через `sudo`: исправляйте только точный проверенный путь.

**ROLLBACK:** для ошибочно созданного пустого каталога сначала убедитесь, что он пуст и не является symlink; удаление — отдельная опасная операция и не часть обычного runbook. Права возвращают известным прежним `chown`/`chmod`, записанным до изменения.

**DANGER:** никогда не использовать `chmod 777`, рекурсивный `chown` от `/srv` и не хранить `.env` в Git.

