# 19. Реакция на инциденты

## Общий порядок

1. Ограничить распространение: прекратить новые опасные операции, не уничтожая evidence.
2. Записать UTC time, симптомы, request/release IDs, affected scope.
3. Сохранить redacted logs, process/container state, connections и disk state.
4. Не удалять containers/volumes и не перезагружать host в панике.
5. Изолировать минимальный компонент: будущий provider/worker, а не весь health API, если это безопасно.
6. Rotate exposed secrets, проверить filesystem ownership/changes и network connections.
7. Восстановить из verified backup или known-good release по отдельному plan.
8. Провести postmortem: timeline, root cause, impact, corrective actions без обвинений.

## Сценарии

| Сценарий | Первое безопасное действие |
|---|---|
| Подозрение на SSRF | прекратить outbound feature/provider, сохранить request ID и destination classification без раскрытия internal IP клиенту |
| Утечка secret | ограничить доступ, rotate/revoke secret, проверить usage; удаление строки не отменяет компрометацию |
| Malware/опасный output | прекратить выдачу, quarantine metadata/artifact с ограниченным доступом; не открывать файл на рабочей машине |
| Заполнение диска | остановить новые writes/jobs, `df/du/docker system df`; не prune |
| Всплеск нагрузки | измерить routes/concurrency/resources, rate/capacity controls; не масштабировать слепо |
| Compromised server | изолировать сеть через согласованный provider/control-plane процесс, сохранить forensic evidence, готовить rebuild на clean host |
| Массовые provider errors | временно установить соответствующий `PROVIDER_*_ENABLED=false` через контролируемый env/recreate; оставить live/ready доступными, если core исправен |
| Жалоба правообладателя | следовать [abuse/copyright process](../product/abuse-and-copyright-process.md), минимизировать retained data, не обещать юридический исход |

Provider enable flags уже реализованы, но это startup configuration, а не динамический control plane: изменение требует проверить rendered config и recreate API. Job-level disable отсутствует. «Остановить worker» — **WARNING modifying** и допустимо только когда scope подтверждён; текущий worker media jobs всё равно не выполняет. API health можно оставить доступным, если он не продолжает опасную работу.

## Evidence commands

```bash
date -u
docker compose --env-file .env.staging --project-name fetchnow-staging \
  -f compose.yaml -f compose.staging.yaml ps -a
docker compose --env-file .env.staging --project-name fetchnow-staging \
  -f compose.yaml -f compose.staging.yaml logs --since 30m --tail 1000
sudo ss -ntup
df -hT
free -h
```

Перед передачей redaction обязателен. **DANGER:** не обещать compliance/SLA, не контактировать с предполагаемым атакующим, не восстанавливать поверх единственной копии и не удалять logs до определения требований хранения.
