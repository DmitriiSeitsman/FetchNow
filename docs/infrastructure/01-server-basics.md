# 01. Основы сервера

## Зачем это нужно

VPS (virtual private server) — изолированная виртуальная машина у провайдера. Host OS — её основная ОС, для целевого staging это Ubuntu 22.04 LTS. В отличие от домашнего компьютера сервер постоянно доступен из сети, обслуживает других пользователей и требует контролируемых изменений, журналирования и восстановления.

Процесс — запущенная программа с PID. Service — управляемая функция системы; daemon — её долговременно работающий фоновый процесс. `systemd` запускает и контролирует services, а journal хранит их события. Port — числовая точка TCP/UDP; socket — конкретный сетевой endpoint. `localhost`/`127.0.0.1` доступен лишь с этого host. Package manager `apt` устанавливает подписанные пакеты. `root` имеет полные права, `sudo` временно запускает одну команду с повышенными правами.

## Безопасный preflight

Все команды ниже — **CHECK**, они не меняют систему (проверка Nginx тоже только валидирует конфигурацию).

| Команда | Назначение | Нормальный результат |
|---|---|---|
| `date -u` | UTC-время | корректная дата; важна для TLS и логов |
| `hostnamectl` | hostname и ОС | ожидаемый host, Ubuntu 22.04 |
| `uname -a` | kernel и архитектура | Linux, ожидаемая архитектура |
| `cat /etc/os-release` | версия ОС | `Ubuntu 22.04` |
| `uptime` | время работы/load average | нет неожиданно высокой длительной нагрузки |
| `free -h` | RAM и swap | есть доступная память, swap не растёт постоянно |
| `df -hT` | диски и filesystem | достаточно места, ни один важный mount не заполнен |
| `sudo ss -lntup` | слушающие sockets и процессы | 80/443 принадлежат host Nginx; 8000/8080 соседям; 8091 свободен до deployment либо FetchNow на loopback |
| `systemctl --failed` | упавшие units | `0 loaded units listed` либо объяснённый список |
| `sudo nginx -t` | синтаксис и ссылки Nginx | `syntax is ok`, `test is successful` |
| `systemctl is-active nginx` | статус Nginx | `active` |
| `systemctl is-active crypto-bot` | сосед CryptoBot | `active` |
| `systemctl is-active justtwo-api` | сосед JustTwo | `active` |

```bash
date -u
hostnamectl
uname -a
cat /etc/os-release
uptime
free -h
df -hT
sudo ss -lntup
systemctl --failed
sudo nginx -t
systemctl is-active nginx
systemctl is-active crypto-bot
systemctl is-active justtwo-api
```

**WARNING:** имена `crypto-bot` и `justtwo-api` даны подтверждённым staging-контекстом, но существование units проверяется на сервере. `Unit ... could not be found` — STOP и повод уточнить фактическое имя, не создавать unit наугад.

## Типичные ошибки и откат

- Высокий `load average` не всегда означает CPU: проверьте CPU, I/O и память отдельно.
- `active` не гарантирует корректный HTTP-ответ; выполните smoke test.
- `df` показывает место, `df -i` — inodes; исчерпаться может одно из двух.

**ROLLBACK:** preflight ничего не меняет. Если проверка не прошла, остановитесь и сохраните вывод. Не перезагружайте сервер и не убивайте процессы до установления причины.

## Что нельзя делать

Не работать постоянно под `root`, не обновлять все пакеты перед deployment без окна изменений, не отключать firewall и не считать reboot универсальным лечением.

